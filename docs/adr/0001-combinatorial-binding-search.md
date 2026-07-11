# ADR 0001 — Combinatorial Binding Search with Deterministic-Complete Default

- **Status:** Accepted — Phase 1 shipped in 0.3.1 (opt-in, `BindingPolicy.FIRST`)
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
- **Lazy short-circuit.** `itertools.product` + `next()` over per-arc candidate lists;
  stop at the first satisfying binding.
- **Search bound.** A cap on combinations tried per enabling check. On exhaustion the
  transition is treated as disabled, surfaced via an **observable** signal (callback /
  debug hook) — never a silent hang.
- **Observability.** The selected binding is exposed on transition-fired callbacks so
  users can see which tokens the engine chose.

### Migration

Completeness changes existing net behavior (tokens previously FIFO-skipped will now be
consumed). Therefore it is **gated**:

1. **Opt-in first** — the `first` policy, off by default. **Implemented in 0.3.1** as
   `BindingPolicy.FIRST`, selectable per-`Transition` (`Transition.binding_policy`) with
   a per-`PetriNet` default (`PetriNet(binding_policy=...)`); the search bound is
   `PetriNet.binding_search_limit` and exhaustion surfaces via
   `PetriNet.on_binding_search_exhausted`. Off by default (`BindingPolicy.LEGACY`), so
   existing nets are byte-for-byte unaffected. This is the escape hatch for conservative
   users during the transition window.
2. **Default-on later** — *(planned)* flip the default at a major version bump, retaining
   a `first_only`/legacy opt-out for one deprecation cycle.

The `random`/`priority` policies (Phase 2) and the default flip (Phase 3) remain planned.

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
- Whether `priority` policy keys on a fixed set of token fields or an arbitrary
  user-supplied key function.
- Precise shape of the observability hook for the selected binding.
