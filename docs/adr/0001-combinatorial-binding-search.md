# ADR 0001 — Combinatorial Binding Search with Deterministic-Complete Default

- **Status:** Accepted; **complete** — amended by Phases 3–4 (see "Deviation from step 2").
  Phase 1 shipped in 0.3.1 (`BindingPolicy.FIRST`); Phase 2 shipped in
  0.3.2 (`BindingPolicy.RANDOM`/`PRIORITY`, `PetriNet(seed=...)`); Phase 3 shipped in
  0.6.0 via [#53](https://github.com/philgresh/cpnx/pull/53) (`FIRST` becomes the default,
  `LEGACY` retained as a deprecated opt-out); Phase 4 shipped in 0.7.0 (`LEGACY` removed)
- **Date:** 2026-07-02
- **Deciders:** cpnx maintainers
- **Related:** `docs/cpn-theory-audit.md` (Findings 1–3, §0 positioning)

---

## Context

cpnx today evaluates transition enabling against the **first `count` tokens** taken
from each input place (FIFO order, or reordered by an optional `InputArc.expression`).
The guard is then evaluated once against that single candidate set.

This produces **head-of-line (HoL) blocking**: if a place holds `[AAPL, MSFT]` and a
transition's guard wants `MSFT`, the engine tests only `AAPL`, the guard fails, and
the transition is reported disabled — even though a valid binding (`MSFT`) exists
further back in the queue. In true CPN semantics (Def 4.4), a transition is enabled if
**any** binding satisfies the guard and the input arc constraints.

A prior v0.3.0 review correctly identified this gap but deferred the fix, because the
naive framing — "add CPN combinatorial binding" — appeared to force CPN's
nondeterministic "pick any binding" behavior onto cpnx, which a workflow engine cannot
accept: production pipelines require reproducibility.

## Decision

Adopt **combinatorial binding search** for single-binding transition enabling, with a
**deterministic-complete default** and opt-in nondeterminism.

The central insight is that **completeness and determinism are orthogonal axes**:

1. **Completeness** — find a valid binding if one exists anywhere in the place, not
   just at the head.
2. **Determinism** — when multiple valid bindings exist, which one wins.

We add completeness while preserving determinism by imposing a **total, stable order**
on the search (insertion order, already given by the place deque) and taking the
**first** binding that satisfies the guard.

### Selection policies

Binding selection becomes a configurable policy, defaulting to the safe option:

| Policy | Behavior | Use case |
|--------|----------|----------|
| `first` (**default**) | Lazy insertion-order search; first satisfying binding wins. Complete **and** reproducible. | Workflow pipelines (default). Subsumes today's determinism guarantee while fixing HoL. |
| `random` (seeded) | Randomised binding choice among satisfying candidates; RNG seedable for replay. | Simulation, fairness testing, CPN-flavored exploration. |
| `priority` | Select by a token key (e.g. `created_at`, or a user-supplied field). | Domain tie-breaks (oldest-first, etc.). |

Policy is set **per-`Transition`** with a **per-`PetriNet` default**.

`InputArc.expression` is **retained**, not deprecated: it now feeds a custom ordering
*into* the deterministic search rather than pre-selecting the sole candidate. It
composes cleanly with the `first` policy.

### Scope discipline

This ADR covers **single-binding completeness for one transition only**. It does
**not** introduce full CPN concurrent *steps* (multisets of binding elements firing
simultaneously). `step()` remains one-transition-at-a-time, justified by Theorem 4.7
(any concurrent step decomposes into a sequence of single-element steps reaching the
same marking).

### Guardrails

- **Guard-free fast path.** No guard ⇒ first `count` tokens per arc always satisfy;
  no search. This is the common case and stays O(1).
- **Short-circuit.** `itertools.product` over per-arc candidate lists; stop at the first
  satisfying binding.
- **Search bound (time *and* memory).** A cap on combinations tried per enabling check.
  Each arc's candidate stream is truncated to `limit + 1` groups *before* forming the
  product (since `itertools.product` materializes each input iterable eagerly, an
  untruncated arc would build its full `C(N, count)` list up front — unbounded memory).
  Truncation is loss-free within the limit: in the product's lexicographic order a
  candidate's overall rank is ≥ its index in any single dimension. On exhaustion the
  transition is treated as disabled, surfaced via an **observable** signal — never a
  silent hang.
- **Callback off the lock.** Exhaustion is buffered during the (locked) enabling pass and
  the `on_binding_search_exhausted` callback is dispatched — de-duplicated per transition —
  after the lock releases, so it may safely call back into the net (per AGENTS.md §1).
- **Observability.** The selected binding is exposed on transition-fired callbacks so
  users can see which tokens the engine chose.

### Migration

Completeness changes existing net behavior (tokens previously FIFO-skipped will now be
consumed). Therefore it is **gated**:

1. **Opt-in first** — the `first` policy, off by default. **Implemented in 0.3.1** as
   `BindingPolicy.FIRST`, selectable per-`Transition` (`Transition.binding_policy`) with
   a per-`PetriNet` default (`PetriNet(binding_policy=...)`); the search bound is
   `PetriNet.binding_search_limit` and exhaustion surfaces via
   `PetriNet.on_binding_search_exhausted`. It was off by default (`BindingPolicy.LEGACY`),
   so existing nets were byte-for-byte unaffected — the escape hatch for conservative users
   during the transition window. (Through 0.5.0 `LEGACY` was the default; Phase 3 below
   flips that while keeping `LEGACY` selectable.)
2. **Default-on later** — *(planned)* flip the default at a major version bump, retaining
   a `first_only`/legacy opt-out for one deprecation cycle.
   *Carried out by Phases 3–4 below: Phase 3 flips the default and keeps `LEGACY` as the
   opt-out, Phase 4 removes it one release later. See "Deviation from step 2" for the one
   point on which it still differs.*

**Phase 2 shipped in 0.3.2:** the `random` and `priority` policies land as
`BindingPolicy.RANDOM`/`PRIORITY`. Both consume the same satisfying-binding enumeration as
`first` (extracted into an internal generator) — `random` reservoir-samples one binding,
`priority` takes the min-key binding — so there was no engine rearchitecture. `random` draws
from a per-net seeded RNG introduced as `PetriNet(seed=...)`, which also now drives the
scheduler's equal-priority tie-break so a seeded net replays end-to-end. To keep seeded
`random` runs reproducible, the enabling/quiescence **probes** use an existence-only check
that never draws the RNG (only the actual firing resolution samples). Unlike `first`,
`random`/`priority` cannot short-circuit and enumerate the full bounded candidate set; when
that set exceeds `binding_search_limit` they select over a truncated prefix and fire the
exhaustion callback.

**Phase 3 — shipped in 0.6.0, via [#53](https://github.com/philgresh/cpnx/pull/53):** the
**default flip**. `FIRST` becomes the default for both
`PetriNet(binding_policy=...)` and the policy a `Transition` inherits when its own
`binding_policy` is left `None`. `BindingPolicy.LEGACY` is **kept, and still works exactly as
before** — this is the `first_only`/legacy opt-out step 2 asked for, and it is what makes the
flip recoverable: a net that depended on head-only resolution sets `binding_policy=LEGACY`
and is byte-for-byte unaffected while it migrates.

Selecting `LEGACY` now emits `CpnxDeprecationWarning` at every entry point that can put it in
force — `PetriNet(binding_policy=...)`, the `PetriNet.binding_policy` setter, and
`Transition(binding_policy=...)` (including post-construction reassignment) — naming 0.7.0 as
the removal. That warning is the substance of the deprecation cycle, and it is new here: the
0.5.0 deprecation was **documentation-only**, so a net relying on `LEGACY` had no runtime
signal at all. The category deliberately subclasses `UserWarning` rather than
`DeprecationWarning`, because Python's default filters ignore `DeprecationWarning` outside
`__main__` — which would have reproduced 0.5.0's silence in a subtler form.

The flip is behaviour-changing for exactly one shape: a **guarded** transition that did not
set a policy explicitly now searches instead of testing only the head, which is the
head-of-line fix this ADR exists to deliver. Guard-free transitions are unaffected —
`_is_head_only` still routes them to the same O(1) head resolution, so the no-guard fast path
(and ADR 0005's count-only path) is unchanged.

**Phase 4 — shipped in 0.7.0:** `BindingPolicy.LEGACY` is removed. One release after the
flip, per step 2's deprecation cycle. At that point `FIRST` is a strict superset of everything
`LEGACY` offered — identical head selection whenever the head satisfies the guard, plus the
head-of-line fix when it does not — so there is deliberately **no replacement** for head-only
resolution *with* a guard; the incompleteness was the only thing `LEGACY` uniquely provided,
and it is the bug. A net that must bound the search to the head candidate can set
`PetriNet(binding_search_limit=1)`, but that is net-wide and signals
`on_binding_search_exhausted` whenever the head fails, so it is a diagnostic knob rather than
a silent `LEGACY` equivalent.

**Deviation from step 2.** Phases 3–4 carry out step 2 — flip, retain the opt-out for one
cycle, then remove — on one point of difference, recorded here rather than by amending the
step: step 2 said "at a major version bump", and this lands across *minor* bumps (0.5.0 →
0.6.0 → 0.7.0). The project is pre-1.0 (`Development Status :: 3 - Alpha`), where semver
allows breaking changes in a minor bump and where this repo has done exactly that before —
ADR 0004 is "shipped in 0.4.0 (breaking; pre-1.0)". Waiting for 1.0 would also strand the
head-of-line fix behind an opt-in indefinitely. Note that
[#43](https://github.com/philgresh/cpnx/issues/43) and the 0.5.0 changelog announced the
**removal** for v0.6.0; that is superseded here, because honouring step 2's deprecation cycle
requires 0.6.0 to be the flip and 0.7.0 the removal.

## Rationale — why the determinism worry is smaller than it appears

cpnx is **already nondeterministic** at a coarser grain than binding selection:
`step()` does `random.choice()` among equal-priority enabled transitions
(`engine.py`), and actions run concurrently on a thread pool. Users who need strict
determinism today already run `max_workers=1` with explicit `priority` values.

Deterministic binding selection therefore **follows the pattern the scheduler already
established** — deterministic default, opt-in randomness, full determinism available
to those who configure for it. We are not introducing a new class of nondeterminism;
we are extending an existing policy knob one level down.

## Consequences

**Positive**

- Closes the main CPN-conformance gap (Finding 3 / HoL blocking) without sacrificing
  reproducibility.
- `first` default is strictly more capable than today's matching and preserves the
  determinism contract.
- Opt-in `random`/`priority` policies open genuine simulation use cases.

**Negative / costs**

- Guard is evaluated once per candidate combination instead of once; mitigated by the
  guard-free fast path, lazy short-circuit, AST-cached guards, and the search bound.
- `count > 1` arcs raise search cost to `C(N, count)` per place, multiplied across
  arcs; the search bound makes this safe but means pathological guards degrade to
  "disabled" rather than exhaustive search.
- Two-phase migration adds a flag and a deprecation cycle.

**Neutral**

- `InputArc.expression` semantics shift from "sole candidate selector" to "ordering
  hint for the search" — behavior-compatible under the `first` policy.

## Alternatives considered

- **Do nothing / keep HoL blocking.** Rejected: it is the primary conformance gap and
  a genuine correctness surprise for users, workaround-able only via `InputArc.expression`.
- **Full CPN semantics (concurrent steps + pick-any nondeterminism).** Rejected:
  breaks reproducibility and is out of scope; Theorem 4.7 makes single-binding steps
  sufficient.
- **Combinatorial-by-default, opt into determinism.** Rejected in favor of the
  inverted default — a workflow engine's safe path should be the default, not a flag.

## Open questions

- Exact default for the search bound (fixed constant vs. proportional to place size).
- ~~Whether `priority` policy keys on a fixed set of token fields or an arbitrary
  user-supplied key function.~~ **Resolved (0.3.2):** an arbitrary user-supplied pure
  `Callable[[list[Token]], object]` (`Transition.binding_priority_key`), defaulting to
  oldest-first (`min` of `Token.created_at`). String-expression keys deferred.
- ~~Whether the `random` RNG is global or per-net.~~ **Resolved (0.3.2):** a per-net
  `random.Random(seed)` via `PetriNet(seed=...)`, shared with the scheduler tie-break.
- Precise shape of the observability hook for the selected binding.
