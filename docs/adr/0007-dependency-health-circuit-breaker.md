# ADR 0007 — Dependency-Health Circuit Breaker

- **Status:** Accepted
- **Date:** 2026-07-29
- **Deciders:** cpnx maintainers
- **Related:** `docs/adr/0006-incremental-enablement.md` (the eligibility gate this feature
  deliberately fails, routing breaker nets to the full-scan scheduler); `cpnx.CircuitBreakerPlace`,
  `cpnx.InputArc` (`test=`), `cpnx.Transition` (`breaker=`); `examples/circuit_breaker.py`;
  `tests/test_test_arc.py`, `tests/test_circuit_breaker.py`

---

## Context

A net frequently has several transitions that depend on the **same external service**. When that
service degrades, the net keeps doing expensive **upstream** work whose result is discarded because
a **downstream** transition's call to the service fails:

```
[enrich]  →  (intermediate tokens)  →  [deliver → external service]
   ^ costly, runs regardless                 ^ fails while the service is down
```

Every upstream firing during the outage is wasted compute, and the failure is a **shared**
condition — multiple transitions call the same service — not local to one transition. The only way
to express this before now was an application-level "is-it-up?" flag plus a lock, checked at the
call site and flipped on failure/recovery. That approach is **invisible** to
`snapshot`/`to_dot`/`validate` (the control flow lives in app globals, not the net), **racy**
(it needs an app-level lock the engine already owns), and **not reusable** (a second dependency
needs the identical machinery). Two hand-rolled copies of one coordination pattern is the signal
that it belongs in the engine as a first-class, analyzable primitive.

The gap has two halves. cpnx's existing primitives cannot express a **shared health gate**:

1. **No non-consuming test/read arc.** `InputArc` always *consumes*. A gate shared by many
   *concurrent* transitions must test token presence **without consuming it**, or the first
   transition to fire destroys the signal (or the gate serializes every dependent transition).
   `ResourcePlace` mismodels "health" as a finite permit pool and forces `capacity ≥ max
   concurrency`; `ThresholdPlace` is a consume-in-batches release gate, not a presence test.
2. **No net-visible place to hold breaker state, and no coupling from "action failed" → "mutate a
   place."** The engine already observes action success/failure (it drives retries) but does not
   expose that to trip a place, and there is nowhere in the net to hold the failure count and
   cooldown deadline where `validate`/`snapshot` can see them.

## Decision

Add two primitives: a **non-consuming test/read arc** and a self-contained **`CircuitBreakerPlace`**
the engine trips on classified failures and re-arms via a probe.

### 1. Test/read arc — `InputArc(..., test=True)`

A `test=True` input arc gates a transition on token **presence** (at least `count` available)
without consuming anything. It is threaded through binding resolution as an empty consume set:

- **enablement** reuses the existing `Place.can_retrieve(count)` check (`_test_arc_satisfied`), so a
  test arc engages no new code path for satisfiability;
- it **binds no specific token** and resolves to `[]` in the `_Binding`
  (`_gather_arc_pools`/`_count_only_arc_binding`/`_resolve_input_tokens`/`_arc_options`), so the
  guard never sees its tokens and `_consume_binding` skips it entirely — nothing is retrieved,
  nothing is deposited, nothing enters the rollback/retry path;
- because it binds no token id, **many transitions can test the same place concurrently** without
  serializing on it — the property a shared health gate requires.

`test=True` combined with `consume_all=True` is a contradiction (a draining arc consumes) and raises
`TypeError` at construction. `key`/`filter` are ignored on a test arc (there is nothing to order or
select — only to count).

### 2. `CircuitBreakerPlace`

A `Place` subclass that owns a single binary **health** signal plus its lifecycle state
(`closed`/`open`), consecutive-failure count, cooldown deadline, and probe. Dependent transitions
gate on it with a test arc; a transition whose failures should trip it names it via
`Transition.breaker` (**many transitions may name the same breaker** — the dependency is shared).

- **Trip:** in `_execute_transition`, under the engine lock right after commit/rollback, a bound
  transition's outcome is fed to `breaker.record_result(success, exc, now)`. A failure is classified
  by the consumer's `trip_predicate`; after `failure_threshold` *consecutive classified* failures
  the breaker opens. A success resets the count. `trip_predicate` runs under the lock (like a
  `guard`), so it must be a trivial pure classifier; a predicate that raises is treated as
  "does not trip" and never crashes the engine.
- **Gating while open:** `CircuitBreakerPlace.can_retrieve` returns `False` for a real firing while
  open, so every test-arc-gated transition is disabled and its input tokens queue in place — the
  entire objective. Out-of-band `trip()`/`close()` cover the case where the consumer learns of the
  outage (or recovery) from something other than a gated transition's failure.
- **Recovery:** the engine services breakers once per `run`/`drive_to_quiescence` loop pass
  (`_service_breakers`). Once the cooldown elapses, a supplied `probe` is run **off the engine lock**
  (it may do I/O), exactly one at a time (`_probing` flag) — a truthy result closes the breaker, a
  falsy result or an exception re-opens it. With no probe, the breaker closes optimistically and the
  next real firing is the trial. Because a gated transition never fires while open, at most one probe
  is ever in flight (the single-probe guarantee).

### 3. Reuse timing, not new machinery, for quiescence and wake-up

`is_quiescent` must not mistake an open breaker with queued work for a finished net, and `run` must
wake at the cooldown deadline to attempt recovery. Rather than special-case breakers in the
scheduler, `CircuitBreakerPlace` speaks the engine's existing timed-token vocabulary:

- `can_retrieve(count, model_time=+inf)` returns `True` while open — the same `+inf` ignore-timing
  sentinel the rest of the engine uses — so `is_quiescent`'s timing-ignoring probe reports the
  breaker as *eventually* available, and the net is "waiting for a boundary," not quiescent. It is,
  however, a **dead marking** for that instant (`is_dead` uses real time → `False`), which matches
  `is_dead`'s documented snapshot semantics.
- `earliest_available_boundary(now)` returns the cooldown deadline while open, so `run` /
  `drive_to_quiescence`'s existing clock loop wakes to service the probe with no new polling.

### 4. Breaker nets run on the full-scan scheduler

Registering a `CircuitBreakerPlace` sets `_has_timed_features = True`, so `_incremental_eligible`
(ADR 0006) is `False` and the net uses the full-scan scheduler. This is correct, not a limitation:
the cooldown re-arm is clock-driven with **no marking mutation**, exactly the class of
re-enablement ADR 0006 excludes from the dirty-set fast path (alongside `PacedResourcePlace` and
`settle_secs`). The full scan re-checks enablement each step and already consults
`earliest_available_boundary`, so breaker recovery integrates with zero scheduler changes.

### 5. Analyzability

`validate()` enforces that a breaker place is referenced only by test arcs (a consuming input arc or
an output arc targeting a breaker raises `TypeError`) and that a `breaker` binding names a registered
`CircuitBreakerPlace`. `snapshot` surfaces the breaker's `state`/`consecutive_failures`/`probe_at`,
and `to_dot` draws it as a double circle labelled with its state and renders test arcs as dashed,
hollow-headed edges. This net-visibility is the decisive advantage over the hand-rolled application
global it replaces.

## Consequences

**Positive.** The wasted-compute problem is solved as net state: once the breaker opens, no gated
transition is newly enabled, so upstream compute drops to ~0 and input tokens queue rather than
being enriched-then-discarded; recovery is automatic on probe success with no restart. The breaker's
state is analyzable (`validate`/`snapshot`/`to_dot`) and concurrency-safe through the engine's own
marking-mutation path — no application locks or module globals. A test arc is a general primitive,
useful for any presence gate, not only breakers.

**Negative / cost.** A breaker is a **reactive** primitive: it trips on *observed* failures, and a
non-consuming test arc provides no permit-style back-pressure, so firings already selected or
in-flight when the breaker trips still complete. Under aggressive pipelining a backlog can be
submitted before the first failures are observed; the savings accrue on *subsequent* enablement
checks once the breaker is open, which is the correct and only achievable guarantee for a reactive
breaker. Consumers who also need a hard concurrency cap should combine the breaker with a
`ResourcePlace`. `trip_predicate` runs under the engine lock and must stay trivial. A breaker net
forgoes the ADR 0006 fast path (as every timed net does). Finally, `failure_threshold` should be set
below a bound transition's `max_retries` if the intent is to trip before retries dead-letter a token.

## Verification

- `tests/test_test_arc.py` — presence gating, non-consumption, `< count` disables,
  `test`+`consume_all` rejected, guard sees only consuming arcs, and many transitions testing one
  place concurrently.
- `tests/test_circuit_breaker.py` — trip after exactly `failure_threshold` classified failures,
  unclassified/raising predicates never trip, success resets the count, gating while open,
  out-of-band trip/close, single-in-flight probe recovery, deterministic trip+probe via
  `drive_to_quiescence`, an open breaker holding work while not-quiescent-but-dead, optimistic
  (no-probe) recovery, validation rejections, `_incremental_eligible` off for breaker nets, and a
  **second independent dependency** re-expressed via the primitive (two breakers tripping and
  recovering independently in one net).
- `tests/test_visualization.py` — snapshot exposes breaker state; `to_dot` renders the breaker node
  and dashed test arcs.
- `examples/circuit_breaker.py` — end-to-end: while down, work is held (`incoming`/`enriched`
  populated, `delivered` empty); after recovery all requests deliver.
- All pre-existing tests pass unchanged (the incremental scheduler, back-pressure, and determinism
  suites are unaffected — a net without a breaker takes exactly the paths it did before).

## Alternatives considered

- **Separate `ReadArc`/`TestArc` class instead of a `test=True` flag.** Rejected: a flag reuses the
  routing/enablement/consume plumbing with a handful of guarded branches, where a new arc type would
  touch every place that iterates `Transition.inputs`. The flag keeps the blast radius small.
- **Compose the breaker from existing primitives** (a plain health-token `Place` re-armed by a
  consumer-wired `PacedResourcePlace` tick + a probe transition). Rejected in favour of a
  self-contained place: the consumer supplies only `trip_predicate` and `probe`, and the failure→trip
  coupling and single-in-flight probe are engine-owned rather than re-hand-rolled per net — the same
  motivation that made this a library primitive in the first place.
- **Model "open" as a cooling health token that auto-matures.** This gives gating and quiescence for
  free but re-arms optimistically on a timer with no probe gate, and lets *every* gated transition
  fire at once when the token matures — no single-probe guarantee. Rejected as the default; the
  optimistic behaviour is retained only as the explicit `probe=None` mode.
- **Full-fidelity breaker on the incremental fast path.** Rejected: clock-driven re-arm with no
  marking mutation is precisely what ADR 0006's eligibility gate excludes; special-casing it in the
  dirty-set scheduler would add a second timed mechanism for no benefit, since a breaker net is not
  breadth-bound in the way the fast path optimizes.
- **Inhibitor (enable-while-*absent*) arc**, to route waiting tokens to a cheap park/skip transition
  during an outage instead of letting them queue. Deferred to a follow-up; the test/read arc
  (enable-while-*present*) is the piece the breaker needs, and the inverse is an independent feature.
