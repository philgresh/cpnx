# ADR 0005 — Count-only fast path for `consume_all` enablement

- **Status:** Accepted — merged in [#40](https://github.com/philgresh/cpnx/pull/40) (unreleased; after 0.4.1)
- **Date:** 2026-07-27
- **Deciders:** cpnx maintainers
- **Related:** `docs/adr/0001-combinatorial-binding-search.md` (guard-free fast path, binding
  policies, RNG determinism); `docs/adr/0004-arc-selection-key-filter.md` §4 (`consume_all`
  bypasses `key`/`filter`); `benchmarks/consensus/` (the BFT workload that surfaced the cost);
  `benchmarks/bench_consume_all_drain.py` (the micro-benchmark)

---

## Context

A `consume_all=True` input arc consumes a place's **entire** available pool in one firing
(ADR 0004 §4). Correct — but its *enablement resolution* was quadratic when the pool was deep.

`_resolve_binding` / `_binding_exists` resolve a transition by gathering each input arc's
eligible tokens through `_arc_available` → `_materialize_pool`. For a `consume_all` arc that
last step is the "full peek" route: `place.peek(len(place))` — an O(N) copy of the whole
available pool (`engine.py::_materialize_pool`). The problem is *how often* that copy runs. A
deep place fills one token at a time (votes accruing under a `ThresholdPlace` barrier; work
queuing before a batch drain), and on **every** enablement probe and **every** losing
binding-selection step the engine re-materialized the whole pool merely to answer "can it
fire?". A place that grows to N tokens therefore pays `1 + 2 + … + N = O(N²)` of copying
before its single sweeping firing.

This is not hypothetical: it is exactly the shape of the BFT consensus benchmark's `yes_votes`
pool (`benchmarks/consensus/`), whose 80-of-100 quorum barrier accumulates a deep pool that a
single `consume_all` commit then sweeps. Under a concurrent flood the `O(N²)` peek throttled
the thread pool. `benchmarks/bench_consume_all_drain.py` isolates the per-probe half: the
eager full-pool peek climbs `O(N)` per probe (≈20 µs at N=1 000 rising to ≈278 µs at N=16 000),
so the aggregate accumulate-then-drain is `O(N²)`.

Nothing about answering "is this transition enabled?" *requires* the pool. For a `consume_all`
arc it is a pure **count** question — is `can_retrieve(count)` satisfied and has the arc
settled? The tokens are needed only at consume time, and `Place.retrieve_all` already produces
them in one pass then.

## Decision

Add a **count-only fast path** for `consume_all` enablement: resolve a qualifying transition
by count alone, defer materialization to firing.

- A new sentinel, `_DRAIN` (a `_DrainAll` singleton), stands in for "the whole pool, resolved
  lazily" inside a binding. `_try_count_only_binding` builds a binding that pairs each
  `consume_all` arc with `_DRAIN` (via `_count_only_arc_binding`) instead of a materialized
  token list, after checking only `place.can_retrieve(arc.count)` and the settle window. Any
  non-`consume_all` arc on the same transition is still resolved the ordinary bounded way
  (FIFO head / key-index), never the full peek.
- `_resolve_binding` and `_binding_exists` consult the fast path first and fall through to the
  existing resolver when it declines (`_NO_FAST_PATH`).
- `_consume_binding` interprets `_DRAIN` by calling `place.retrieve_all(model_time=m_time)`
  under the same lock that selected the binding — so the whole-pool read happens **exactly
  once**, at firing, consistent with the `can_retrieve` check that enabled it.

### Eligibility boundary (why it is safe)

`_qualifies_for_count_only` engages the fast path only when **all** hold:

1. `transition.guard is None`,
2. `transition.binding_priority_key is None`,
3. the transition is **head-only** (`LEGACY`, or guard-free `FIRST` — see `_is_head_only`),
4. at least one input arc has `consume_all=True`.

The first three are the correctness- and determinism-critical exclusions. A `guard` and a
`binding_priority_key` are the only things that *read* an arc's tokens between resolution and
firing; a head-only transition with neither inspects no token before it fires. Crucially it
also draws **no RNG** while resolving (only `RANDOM`/`PRIORITY` sample or rank candidates). So
deferring the whole-pool read to consume time is behaviour-identical *and* cannot perturb the
seeded RNG stream — `test_seeded_determinism.py` passes unchanged, which is the load-bearing
guarantee. Requirement 4 keeps the blast radius minimal: a transition without a `consume_all`
arc gains nothing (its ordinary resolution is already bounded) and is left on the existing
path.

`consume_all` continues to ignore `key`/`filter` on this path exactly as on the old one
(ADR 0004 §4) — the fast path changes *when* the pool is read, never *which* tokens a sweep
takes.

## Consequences

**Positive.** Draining a deep `consume_all` place drops from `O(N²)` to `O(N)`; peak memory
falls too (the pool is no longer copied on every probe, only materialized once at consume).
`bench_consume_all_drain.py` shows the per-probe cost flat at ~0.7–1.0 µs regardless of N
against the eager path's `O(N)` climb (ratio widening 23× → 278×).

**Negative / cost.** A new sentinel type and a second resolution path to keep in step with the
primary resolver. The fast path is guarded by a tight eligibility predicate, and its behaviour
is pinned by regression tests, but any future change to guard/priority/head-only semantics must
revisit `_qualifies_for_count_only`. The initial implementation also pushed
`_try_count_only_binding` to cyclomatic rank C; it was split into `_qualifies_for_count_only` +
`_count_only_arc_binding` to satisfy the repo's complexity gate, at the cost of two more small
methods.

## Verification

- `tests/test_consume_all_fastpath.py` — deep-place drain (N=500) in one firing; `ThresholdPlace`
  gate then full sweep; a **guard** still sees the materialized batch (fast path excluded); a
  **`binding_priority_key`** excluded; `filter` still ignored; mixed `consume_all` + plain arc.
- `tests/test_engine_refactor.py::test_resolve_binding_consume_all` — asserts the lazy `_DRAIN`
  binding, then that `_consume_binding` sweeps the pool.
- `tests/test_seeded_determinism.py` — unchanged and passing (the RNG-neutrality guarantee).
- `benchmarks/bench_consume_all_drain.py` — the per-probe `O(1)` vs `O(N)` evidence.

## Alternatives considered

- **A heap / ordered structure for the pool.** Wrong tool: draining `consume_all` is not an
  ordering problem — it takes *everything*, in FIFO order, and already has an incremental store
  (`places._TokenStore`). A heap would not remove the per-probe full-copy.
- **Cache the materialized pool across probes.** Still `O(N)` to build and invalidate on every
  deposit into the place; the count-only probe avoids building it at all.
- **Widen the fast path to guarded/priority transitions.** Rejected: those must see the tokens
  (guard) or draw RNG while ranking (priority), so caching or deferring their resolution would
  change behaviour or the seeded stream. Left on the existing path.
- **Do nothing.** The `O(N²)` was invisible until a workload built a deep `consume_all` pool;
  the consensus benchmark made it concrete, so the fix is data-backed rather than speculative.
