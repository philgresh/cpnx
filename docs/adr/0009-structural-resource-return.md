# ADR 0009 — Structural resource-permit return (auto-synthesized self-loop)

- **Status:** Accepted
- **Date:** 2026-08-04
- **Relates to:** [0008](0008-color-domain-impact-analysis.md) (colour-domain impact
  analysis — this ADR removes that ADR's resource-permit caveat); the CPN-faithful
  cafe and honest `to_dot` shipped in #50 (the dashed `return (implicit)` edge is
  the cosmetic stopgap this ADR makes largely unnecessary).

## Context

A cpnx resource pool is a [`ResourcePlace`][cpnx.ResourcePlace]; a transition
*borrows* a permit (`Token.is_resource`) by declaring an `InputArc` on the pool.
The engine returns that permit to its pool once the firing completes — and it did
so through **two** different mechanisms:

1. **Explicit self-loop (CPN-faithful).** The transition declares both
   `InputArc(pool)` and a matching `OutputArc(pool)`; on a successful firing the
   permit is re-deposited through the output arc by the normal deposit plan. This
   is exactly how Jensen's CPN models a non-consuming resource — the double arc,
   there being no separate read/test arc.
2. **Implicit leftover-return (the departure).** If the transition declared **no**
   matching `OutputArc`, the engine swept the consumed permit up as *leftover* and
   returned it to the pool **outside the arc structure**
   (`_return_leftover_resources`).

Mechanism 2 makes the net's marking effect
`M' = M − Pre(t) + Post(t) + (auto-returned resources)`, where the last term is an
engine rule expressed by no arc. A purely structural reading of the net is
therefore wrong for pools — the pool looks like it monotonically drains when in
fact permits cycle. Concretely:

- `to_dot` cannot draw the return (there is no arc), so pools look sourceless.
- `analysis.trace_impact` (ADR 0008) walks `transition.outputs`, so a borrowing
  transition's blast radius never includes the pool it touches.
- Structural invariants (P-invariants, boundedness by inspection) do not hold from
  the arcs alone.

ADR 0008 records this as an explicit caveat. #50 mitigated it cosmetically by
drawing a dashed `return (implicit)` edge in `to_dot`, but the *model* stayed
structurally dishonest for every consumer other than that one renderer.

## Decision

**Synthesize the missing self-loop.** At [`validate`][cpnx.PetriNet.validate]
(once, idempotently), for every transition that borrows from a `ResourcePlace` via
an `InputArc` with no matching `OutputArc`, the net appends an `OutputArc(pool)`
marked `synthesized=True`. Mechanism 2's off-arc return is thereby converted into
mechanism 1's structural return, for free, with no change the author must make.

This is **behaviour-preserving**: resource permits were *already* always returned
on success, so the marking sequence is unchanged; only the *representation* of the
return changes (an off-arc engine sweep becomes an on-arc deposit). It is
**non-breaking**: no warning, no migration, no removal.

Synthesis is triggered before the first firing or introspection from every entry
point that needs it — `validate` (hence `run`), `step` (hence
`drive_to_quiescence`), `to_dot`, `trace_impact`, and `risk_report` — behind a
one-time flag that is reset when a place or transition is added, so a dynamically
built net re-synthesizes correctly.

### Overrides (two levels)
- **Declare your own arc.** Any author-declared `OutputArc(pool)` (any `count` /
  `condition`) suppresses synthesis for that pool — detection is "no matching
  output arc," so your arc *is* the match.
- **Opt out entirely.** `Transition(auto_return_resources=False)` suppresses
  synthesis for that transition, leaving it on the raw implicit leftover-return.

### Scope / known limitations
- **`consume_all` borrows are left on the implicit path.** A fixed-count
  `OutputArc` cannot express "return however many permits you drained," so a
  `consume_all` resource `InputArc` is skipped and its return stays off-arc (still
  correct, just not structural). `to_dot` still draws the dashed edge for these.
- **Asymmetric counts** (`InputArc(pool, count=2)` + `OutputArc(pool, count=1)`)
  already have a matching output arc by name, so no arc is synthesized and the
  residual permit is returned implicitly — same as before.
- **Two `InputArc`s on the same pool** synthesize a single return arc (detection
  dedupes by pool name), so the second borrowed permit rides the implicit path and
  is invisible to `to_dot`. Still behaviour-preserving (both permits return); the
  structural form is `InputArc(pool, count=2)`, which synthesizes `OutputArc(count=2)`.
- The **failure/rollback** return (`_rollback_failed_transition`) is untouched: a
  failed firing must return the permit regardless of the self-loop (an explicit
  `OutputArc` would not fire when the action raised either), so it stays on the
  rollback path and is correct.

## Consequences

- `to_dot`, `trace_impact`, `risk_report`, and structural invariants read resource
  pools honestly by construction; the ADR 0008 caveat is closed for the common
  case. The synthesized arc is a real `OutputArc`, so it needs no special-casing
  downstream — it renders and traces like any other output arc.
- Introspection (`to_dot` / `trace_impact` / `risk_report`) now performs a one-time
  idempotent mutation of `transition.outputs` the first time it is called on a net
  that has not yet been validated. This is intentional (it makes the picture
  match reality) and flagged via `OutputArc.synthesized` for anyone inspecting the
  arcs.
- `_return_leftover_resources` is retained — it still handles the `consume_all`,
  opt-out, and rollback paths — but is a no-op for the common synthesized case.

## Alternatives considered

- **Deprecate + warn, then require the self-loop (breaking).** Emit a
  `DeprecationWarning` on each implicit return and eventually make the author add
  the arc. Rejected: it pushes busywork onto every author for a return the engine
  can add itself, and the eventual removal is a breaking change — all to reach the
  same structural end state synthesis reaches transparently today.
- **Keep as-is (document only).** Rejected: perpetuates the ADR 0008 caveat and
  leaves every structural consumer other than the dashed-edge renderer wrong.
- **Compute the return arc on demand in each consumer** (don't mutate the net).
  Rejected: every consumer (`to_dot`, `trace_impact`, invariants, future tools)
  would have to re-derive the implicit return independently and stay in sync;
  materializing one honest arc is simpler and single-source-of-truth.

## Migration

None required. Existing nets that relied on the implicit return keep working and
simply become structurally honest. To restore the old off-arc behaviour for a
specific transition, set `auto_return_resources=False`.
