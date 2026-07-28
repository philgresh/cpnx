# ADR 0006 — Incremental Enablement (Per-Place Dirty Set Scheduling)

- **Status:** Accepted — implemented in [#44](https://github.com/philgresh/cpnx/pull/44)
- **Date:** 2026-07-27
- **Deciders:** cpnx maintainers
- **Related:** `docs/adr/0005-consume-all-count-only-fast-path.md` (the depth-side companion to
  this breadth-side fix); `docs/adr/0001-combinatorial-binding-search.md` (binding policies,
  RNG determinism); `benchmarks/bench_transition_scan.py` (the micro/macro evidence);
  `benchmarks/consensus/` (the BFT workload that motivates it)

---

## Context

The engine's scheduler re-derives enablement from scratch every step. `PetriNet.step()` calls
`_select_transition_to_fire()`, which calls `_enabled_transition_bindings()` — a loop over
**every** transition in the net (`self.transitions.values()`) that resolves a fresh binding for
each, then fires exactly one. `is_quiescent()` and `is_dead()` perform the same full scan to
answer a yes/no question. A single step is therefore `O(T)` in the transition count `T`, and a
run that fires `K` times pays `O(K * T)` binding resolutions — even though any one firing
changes only the places it touched, and can therefore only flip the enablement of the
transitions reading *those* places. The scan runs under the single global engine lock, so its
cost is not just CPU: it is lock-hold time, and every worker thread waiting to fire pays for it.

ADR 0005 removed the `O(N²)` **depth** cost of `consume_all` — the per-probe full-pool copy that
compounded across the steps a deep place sat enabled-but-undrained. This ADR removes the
complementary **breadth** cost: the per-step scan across all `T` transitions, most of which are
disabled and unaffected by the marking change that just occurred. A wide fan-out net — many
transitions, few of them touched by any given firing — starves the `ThreadPoolExecutor`, because
every worker's turn to select a binding pays the full `O(T)` scan under the lock. This is exactly
the shape of the BFT consensus benchmark (`benchmarks/consensus/`): many validator-facing
transitions coexist, and only a handful become newly enabled per step.

`benchmarks/bench_transition_scan.py` isolates the cost with two views. **`scan us`** times one
`_select_transition_to_fire` probe against a net of `T` independent transitions with exactly one
enabled — the per-probe cost climbing linearly with `T` is the scan resolving `T - 1` disabled
transitions for nothing. **`us/step`** drives `T` independent one-shot transitions to quiescence
(`K = T` firings, each preceded by an `O(T)` scan) and reports wall time per step — a rising
per-step cost as `T` grows is the same `O(T)` scan, now compounding across `K = T` steps into
`O(T²)` total drive time. Both climb linearly with `T` today.

## Decision

Replace the `O(T)` global scan with an incremental scheduler built on a static reverse-routing
table plus a per-place dirty set, maintaining an enabled set so that selection is `O(1)` and
`is_quiescent()` is `O(1)` for untimed nets.

1. **Static reverse-routing tables.** `_input_routing` maps place → the transitions holding an
   `InputArc` on it. `_output_routing` maps place → the transitions holding an *unconditional*
   `OutputArc` on it — used only for back-pressure release. Conditional output arcs are already
   skipped by `_check_output_capacity`, so they cannot cause a transition to block on output
   capacity and are correctly omitted from this table.

2. **Dirty set** `_dirty_places`. A place is marked dirty whenever its token pool is mutated: on
   **deposit** (external `deposit()`, a transition's output deposit, a rollback return, a
   leftover-resource return, a schema-violation error deposit) and on **consumption** (token
   removal), because removing tokens can free output capacity for a transition whose *output*
   arc targets that place (back-pressure release).

3. **Maintained enabled set.** `_enabled_bindings` maps selection-eligible transitions (capacity-
   and timing-aware) to their cached binding, indexed with priority buckets for `O(1)` selection.
   `_potentially_enabled` tracks bindings ignoring timing/capacity, giving `O(1)` quiescence.

4. **Reconcile.** On the next step, expand the dirty places through the routing tables to the set
   of affected transitions (size `K`), and re-evaluate only that subset, updating the enabled
   set. Transitions untouched by the last firing are never re-resolved.

5. **Always-re-evaluated sets** `_volatile_transitions`, `_capacity_blocked`. Two shapes of
   transition can change enablement with *no input-place mutation* to route from, so reconcile
   re-checks them every pass in addition to the dirty-routed subset:
   - **Volatile** (`_is_volatile`): input-less *source* transitions (no place routes to them, yet
     they are always enabled), and any transition with a `guard` or an input arc with a
     `key`/`filter`. The last is the subtle one: certification's documented *late-binding* rule
     (ADR 0003 / `cpnx.certification`) admits a callable that reads a rebindable global or free
     variable — e.g. `guard=lambda toks: gate_open` with a `bool` `gate_open` reassigned mid-run.
     The pre-0006 engine re-evaluated every guard on every scan, so such rebinding worked;
     caching a certified-callable result would silently break it. Rather than statically prove
     which callables are token-only, we re-evaluate any of them each pass. This costs exactly the
     old scan for guarded transitions (never wrong), while the full incremental win still applies
     to the guard/selection-free fan-out that `bench_transition_scan` and typical wide nets are
     made of.
   - **Capacity-blocked** (`_capacity_blocked`): a transition with a satisfiable binding held back
     *only* by output back-pressure. Re-checking it each pass lets it unblock the instant its
     output place drops below bound — including an out-of-band `place.retrieve()` the dirty set
     never observes (in-band consumption is already covered by dirty-on-consume + `_output_routing`).

6. **Reactivation heap** `_reactivation`. A retry-delayed rollback token (`retry_delay`) becomes
   available purely as time passes, with no marking mutation to trigger a dirty flag. A min-heap
   of `(available_at, place)` re-dirties the place once its delay elapses, polled by `run()`'s
   existing `cooldown_interval` loop — so retries stay on the fast path without falling back to a
   full scan.

7. **Seeding.** The first time the scheduler is used on a net, every non-empty place is seeded
   dirty, so the initial enabled set reflects the starting marking exactly. Adding a transition
   after the fact invalidates the routing tables and re-seeds on next use.

### Eligibility boundary (why it is safe)

The incremental fast path engages only when a static gate, `_incremental_eligible`, holds for
the whole net. When it does not, the engine falls back to today's full-scan methods,
**unchanged** — byte-identical behavior to the pre-ADR-0006 engine. The gate is the conjunction
of two conditions:

1. **`not _has_timed_features`** — the net has no `PacedResourcePlace` and no arc with
   `settle_secs > 0`. These constructs re-enable purely as the logical or wall clock advances,
   with no marking mutation at all — invisible to a dirty set by construction. (The reactivation
   heap above handles the one timed case that *is* marking-adjacent — retry delay — but a
   `PacedResourcePlace`'s pacing and a `settle_secs` window are clock-driven independent of any
   token move, so they are excluded from the fast path entirely rather than special-cased.)

2. **No transition uses effective `BindingPolicy.RANDOM` or `PRIORITY`.** Per ADR 0001, those
   policies draw from the seeded RNG *during binding resolution*, for every enabled transition,
   on every step (`_reservoir_pick` for `RANDOM`, `_reduce_min_key` for `PRIORITY`). An
   incremental scheduler resolves each transition a different number of times than a full scan
   would — exactly the point of the optimization — which would shift the seeded RNG stream and
   break the pinned golden sequence in `tests/test_seeded_determinism.py`. `LEGACY` (the default)
   and guard-free `FIRST` draw RNG only once, for the scheduler's own tie-break among enabled
   candidates (`_rng.choice(candidates)`), which advances the stream by candidate-count only —
   a quantity the maintained enabled set reproduces exactly. Those two policies are therefore
   safe under the fast path; `RANDOM`/`PRIORITY` are not.

A naive dirty set — mark a place dirty on deposit, re-scan only its consumers — misses four
re-enablement hazards. Each is handled explicitly above rather than assumed away:

- **(a) Output-capacity / back-pressure release.** A transition can go from blocked-on-output to
  enabled purely because a *downstream* place drained, freeing capacity — no input place changed.
  In-band this is covered by dirty-on-consume plus `_output_routing` (consuming from a place
  dirties it, reaching the transitions whose output arc targets it). An *out-of-band* drain —
  `net.places[name].retrieve(...)` straight on the `Place`, bypassing the engine — emits no
  dirty flag, so `_capacity_blocked` re-checks every currently back-pressured transition each
  reconcile to catch it. This safety net covers draining an *output* place to release
  back-pressure, the only supported out-of-band pattern. Draining an *input* place out-of-band is
  **not** supported on the fast path: a cached selection binding names specific token ids, so if
  those tokens are pulled from under it the subsequent `retrieve_specific` raises rather than
  silently re-resolving as the full scan would. Mutate live input markings through the engine
  (`deposit`/firing), not by reaching into `Place` directly.
- **(b) Time-gated re-arm.** A place or arc that re-enables purely from clock advance emits no
  mutation event for a dirty set to observe. The eligibility gate excludes such nets from the
  fast path outright (condition 1); the one clock-driven case close enough to the marking to be
  worth handling on the fast path — retry delay — gets the reactivation heap instead of a full
  fallback.
- **(c) External-state callables and source transitions.** A `guard`/`key`/`filter` may read
  mutable state that changes with no token move (certification's late-binding rule permits it),
  and an input-less source transition has no place to route from at all. Both are folded into the
  always-re-evaluated `_volatile_transitions` set (Decision §5), so their enablement is never
  stale — at the cost, for those transitions only, of the same per-step re-evaluation the old
  full scan did.
- **(d) RNG-stream determinism.** Resolving a different number of transitions than a full scan
  would changes how many RNG draws occur when a policy samples during resolution.
  `RANDOM`/`PRIORITY` are excluded from the fast path (condition 2); `LEGACY`/`FIRST` draw RNG
  only at the scheduler's tie-break, which the maintained enabled set reproduces identically, so
  the same-seed-twice guarantee (`test_seeded_determinism.py`) holds under either path. One
  subtlety the tie-break exposes: the winner is `_rng.choice` indexing into a priority bucket, so
  the bucket's *order* must not vary run to run. Reconcile therefore re-evaluates the affected
  transitions in **registration order** (`_transition_order`) rather than raw `set` iteration
  order — otherwise Python's per-process string-hash seed would randomize bucket membership and a
  seeded net would fire differently across processes.

## Consequences

**Positive.** The enablement scan drops from `O(T)` to `O(K)`, where `K` is the number of
transitions actually touched by the last firing — typically far smaller than `T` in a wide
fan-out net. Lock-hold time per step collapses correspondingly, which is the direct lever on the
`ThreadPoolExecutor` starvation this ADR targets: less time under the lock per step means more
worker throughput once concurrency is the ceiling, not the scan. `is_quiescent()` becomes `O(1)`
for untimed nets, which lifts `run()`'s per-iteration cost ceiling, not just `step()`'s — a loop
that polls quiescence frequently no longer pays an `O(T)` tax on every poll. And CPU is no longer
spent evaluating transitions whose inputs did not change since the last check.

**Negative / cost.** Correctness now depends on state-management rigor: every mutation path that
can change a transition's enablement must set the corresponding dirty flag, and a missed one is
a silent dead net — a transition that should be enabled never gets re-checked, with no exception
or visible symptom. This is a second scheduler path that must be kept in step with the existing
full-scan fallback as the engine evolves; a change to how deposits, consumption, or capacity
release work must be mirrored in the routing/dirty-set bookkeeping. The eligibility gate also
narrows the win: timed nets and nets using `RANDOM`/`PRIORITY` binding get no speedup at all,
falling back to the pre-existing `O(T)` scan unchanged. Selection itself is genuinely `O(1)`
(a `_rng.choice` over the minimum non-empty priority bucket, with `O(1)` swap-remove on fire),
so `bench_transition_scan`'s drive — where each firing touches one private place — flattens to a
constant `us/step` even as `T` grows. The residual `K` is not the enabled count but the *fan-in*:
reconcile re-evaluates every transition routed from a dirtied place, so a place read by many
transitions (a shared hub, or a large `_volatile_transitions`/`_capacity_blocked` set) makes each
touch of it cost `O(fan-in)`. That is inherent — those transitions' enablement really can change
— and still far below the old `O(T)` whenever the fan-in is a fraction of the whole net.

## Verification

- `benchmarks/bench_transition_scan.py`: `scan us` (1-of-`T` enabled) flattens instead of
  climbing linearly, since the probe no longer resolves the `T - 1` disabled transitions; `us/step`
  on the drive benchmark drops sharply, collapsing the `O(T²)` total drive cost toward `O(T)`.
- All existing formal tests pass identically, including `tests/test_seeded_determinism.py` (via
  fallback, since it exercises `RANDOM` and `PRIORITY`), the BFT quiescence tests
  (`tests/test_consensus.py`), `tests/test_backpressure.py`, `tests/test_threshold.py`, and
  `tests/test_concurrent.py` — together proving the incremental scheduler skips no valid firing
  and preserves deterministic seeded ordering wherever the fast path engages.
- A new `tests/test_incremental_enablement.py` covering: routing-table correctness (input and
  output routing agree with the transitions a full scan would find), back-pressure release via
  `_output_routing`, retry re-arm via the reactivation heap, and fallback parity (fast-path and
  full-scan produce identical firing sequences on the same net and seed, for nets where both are
  eligible to run).

## Alternatives considered

- **Do nothing / keep the `O(T)` full scan.** The ceiling is real and measured directly by
  `bench_transition_scan.py`; a wide fan-out net like the BFT consensus workload pays for it on
  every step regardless of how much of the net actually changed.
- **Full incremental scheduling for all nets, no eligibility gate.** Rejected. Timed
  re-enablement and `RANDOM`/`PRIORITY` RNG-stream ordering are exactly the cases where an
  incorrectly-maintained incremental scheduler fails silently — a dead net or a broken seeded
  replay — for comparatively little gain on those net shapes (timed nets are usually not
  breadth-bound; `RANDOM`/`PRIORITY` nets are typically smaller in practice). Correctness-first
  fallback was chosen instead of a universal but riskier rewrite.
- **Recompute the enabled set from scratch each step, but skip binding resolution for disabled
  transitions via a cheaper pre-check.** Still `O(T)` per step in the membership loop itself,
  even if the per-transition cost were lower; the maintained enabled set avoids the loop over
  all `T` transitions entirely, not just the expensive part of visiting each one.
- **Order-statistics tree for `O(log N)` selection even when all `N` transitions are
  simultaneously enabled.** Deferred as a follow-up, tracked in
  [#45](https://github.com/philgresh/cpnx/issues/45). It addresses the residual cost noted in
  Consequences, but is not needed to eliminate the dominant cost this ADR targets — resolving
  transitions that are disabled and irrelevant to the last firing.
