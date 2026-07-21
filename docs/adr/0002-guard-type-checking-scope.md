# ADR 0002 — Guard/Arc-Expression Type-Checking Scope

- **Status:** Accepted — shipped in the callable-only-expressions work (Phase 4)
- **Date:** 2026-07-21
- **Deciders:** cpnx maintainers
- **Related:** `docs/cpn-theory-audit.md` (`Type[G(t)] = Bool`, Findings 1–3, §0
  positioning); `docs/adr/0001-combinatorial-binding-search.md`

---

## Context

In full CPN formalism a transition guard is typed: `Type[G(t)] = Bool` (Def 4.2). More
broadly, every place `p` carries a colour set `C(p) ∈ Σ`, and arc expressions are typed
against it (`Type[E(a)] = C(p)_MS`) — Jensen & Kristensen ch 3.3/4.2. Guards, arc
expressions, and tokens are all checked for consistency against this shared type system.

cpnx guards and arc expressions are now **callable-only**: string expressions (and their
sandbox) were removed in this phase, in favor of ordinary Python callables (`def`s and
lambdas) as the sole expression form. Removing the string path closes off one kind of
error surface (sandbox escapes, string-eval typos) but reopens the question this ADR
answers: **now that expressions are arbitrary Python callables, how much of CPN's type
discipline should cpnx enforce on them at construction time?**

The candidate surfaces to check are:

1. The **return type** of a guard/predicate callable (`Transition.guard`,
   `OutputArc.expression`) — must it be `bool`?
2. The **parameter (input-token) type** of any expression callable — must it declare
   `list[Token]`?
3. The **runtime type of the tokens actually flowing through** the net — does a token's
   value match what the place/arc expects?

cpnx's own data model constrains what (3) could even mean: `Token.color` (`src/cpnx/
tokens.py`) is an untyped `str | None` — a colour *tag*, not a value drawn from a typed
colour set — and `Token.payload` is an arbitrary `FrozenDict`. `Place` (`src/cpnx/
places.py`) does offer an optional `color_set: set[str] | None`, but it is a coarse,
opt-in, **deposit-time** membership filter over the `color` string tag; it says nothing
about `payload`, and nothing is checked at construction time, only when a token is later
deposited. There is no place/arc "colour-set consistency" check in the CPN ch 3.3 sense.

This ADR is the sibling of ADR 0001: that ADR decided how much of CPN's *binding*
semantics to adopt; this one decides how much of CPN's *type* semantics to adopt, for the
callables that replaced string expressions.

## Decision

Enforce a narrow, high-value slice of CPN's type discipline at construction time, and
explicitly decline the rest.

### 1. DO check the return annotation of boolean-predicate callables

`Transition.guard` and `OutputArc.expression` are boolean predicates — under CPN
formalism, `Type[G(t)] = Bool`. When either is assigned (`__setattr__` in
`src/cpnx/transitions.py`), the callable is passed through `_reject_non_bool_return`,
which inspects its **return annotation** via `typing.get_type_hints` and raises
`TypeError` on an unambiguous mismatch (`-> int`, `-> str`, `-> None`, ...).

The check is deliberately conservative, so it never punishes correct code:

- **Unannotated → pass.** Lambdas cannot carry a return annotation, so the common
  lambda predicate is accepted untouched; only an annotated `def` engages the check.
- **`bool` / `typing.Any` / a union containing `bool` → pass.** `bool` is the contract;
  `Any` is a deliberate opt-out; `bool | None` / `Optional[bool]` can return `bool`, so
  both are tolerated.
- **Unresolvable → pass.** A forward reference, a `TYPE_CHECKING`-only name, or any
  other resolution failure never breaks construction — annotation resolution is
  best-effort, not load-bearing.
- **Everything else raises `TypeError`.**

Rationale: the return type is *author-variable and semantically meaningful* — a guard
that returns a non-bool value is a genuine modeling error, not a stylistic quibble — and
this is the one place cpnx can cheaply assert `Type[G(t)] = Bool` directly. We raise
rather than warn because that is consistent with the sibling construction-time checks
landed in this same phase (string-expression rejection; the non-callable rejection on
`Transition.binding_priority_key` from ADR 0001 Phase 2); the existing corpus already
annotates its `def` guards `-> bool`, so the blast radius of raising is zero; and this
ships as part of the API-stabilizing 1.0.0 major, where a stricter contract is cheapest
to introduce.

### 2. DO NOT check the parameter (input-token) annotation

The same mechanism could inspect a callable's first-parameter annotation (expecting
`list[Token]`) with equal ease. We decline to enforce it.

Unlike the return type, the parameter type is **engine-fixed, not author-variable**: the
engine always calls the callable with a `list[Token]` — the candidate binding for a
guard, all place tokens for an `InputArc.expression`, the non-resource output tokens for
an `OutputArc.expression`. A wrong parameter annotation therefore catches nothing
behavioral; it flags a typo in a signature the engine never reads at call time.

It is also false-positive-prone: `Sequence[Token]`, `Iterable[Token]`, a bare `list`, a
`Token` subclass, or no annotation at all are all legitimate, correct signatures.
Enforcing a single expected annotation would punish correct code for zero safety gain.
(A lenient warn-only variant was considered and rejected as pure noise — see
Alternatives.)

### 3. DO NOT assert the runtime type/colour of the actual input tokens

This is the check that would genuinely catch "wrong tokens flowing through" — full CPN
type consistency (ch 3.3): verifying a token's value against the colour set of the place
or arc it flows through. It is impossible to do at construction time, for two
independent reasons:

- **No token instances exist yet.** Tokens are created and flow at `deposit()` / firing
  time, not at `Transition`/`OutputArc`/`InputArc` construction. There is nothing to
  check against until the net is running.
- **cpnx has no typed colour sets to check against even at runtime.** `Token.color` is
  an untyped `str | None` tag, and `Place.color_set` (where present) is an optional,
  coarse, deposit-time membership filter over that tag — not a type in the CPN sense,
  and it says nothing about `payload`, which is arbitrary.

This is precisely the "full CPN type consistency" that the project's locked scope
excludes, per `docs/cpn-theory-audit.md` Findings 1–3 and its §0 positioning: cpnx is
"a workflow engine inspired by CPNs, not a CPN simulator," trading colour-set typing for
identity-bearing work items whose payload is accumulated at runtime via `evolve()`.

## Consequences

**Positive**

- The guard/predicate return contract (`Type[G(t)] = Bool`) is now a hard
  construction-time boundary, not just documentation or convention.
- Lambdas — the common case — are entirely unaffected, since they carry no return
  annotation.
- Existing annotated `def` guards already comply; this is a zero-migration change.

**Negative / costs**

- The check is annotation-based, so it validates *annotations*, not runtime *values*.
  It cannot catch a guard annotated `-> bool` that returns something else at runtime —
  Python does not enforce annotations, and cpnx deliberately does not add a runtime
  return-value assertion on the hot path (see Alternatives).
- Users who want the parameter-type or full colour-set checks this ADR declines must
  build them themselves (e.g. an `assert isinstance` inside the callable); cpnx will not
  do it for them.

**Neutral**

- This is a strictly narrower guarantee than full CPN typing. It is scoped to match
  what ADR 0001 and the theory audit already established as cpnx's boundary: correctness
  of the *shape* of the contract cpnx's own engine relies on, not correctness of
  arbitrary domain data flowing through it.

## Alternatives considered

- **Full CPN type consistency (typed places / colour sets).** The theoretically
  "correct" way to assert input-token types: give every `Place` a real typed colour set,
  validate arc-expression outputs and token deposits against it, and check
  producer/consumer consistency across arcs. This is a substantial feature expansion —
  typed places, deposit-time validation beyond the existing coarse `color_set` string
  filter, and arc/place consistency checking — that contradicts the workflow-engine
  positioning and audit Findings 1–3. Deferred; if ever pursued, it belongs in its own
  ADR. This remains the real path for anyone who genuinely wants typed inputs.
- **Warn instead of raise on a non-bool return annotation.** Rejected for inconsistency
  with the sibling `TypeError` checks landed in this same phase (string-expression
  rejection, non-callable `binding_priority_key`), and because the blast radius of
  raising is already zero against the existing corpus — there is no migration cost a
  warning would soften.
- **Runtime return-value assertion** (`isinstance(result, bool)` on every guard/predicate
  call). Rejected: guards are evaluated speculatively and repeatedly — per transition per
  step, and per candidate binding under the `random`/`priority` search from ADR 0001 —
  so a per-call type assertion is unacceptable hot-path overhead for a contract that
  annotations already express at zero runtime cost.
- **Enforce the parameter (input-token) annotation alongside the return annotation.**
  Rejected: see Decision §2 — the parameter type is engine-fixed and checking it flags
  typos in an unread signature while rejecting legitimate variations
  (`Sequence[Token]`, `Iterable[Token]`, subclasses, no annotation).

## Open questions

- Whether a future opt-in "strict mode" should allow users to enable the declined
  checks (parameter annotation, or a debug-only runtime return-value assertion) for
  development/test environments where the hot-path cost is acceptable.
- Whether typed places / colour sets (Alternatives, first bullet) is ever worth pursuing
  as its own ADR, and if so, how it would interact with the untyped `payload` model.
