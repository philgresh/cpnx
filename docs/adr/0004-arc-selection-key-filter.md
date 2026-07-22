# ADR 0004 — Splitting `InputArc.expression` into `key` + `filter`; Renaming `OutputArc.expression` to `condition`

- **Status:** Accepted — shipped (breaking; lands in `[Unreleased]`, pre-1.0)
- **Date:** 2026-07-21
- **Deciders:** cpnx maintainers
- **Related:** `docs/adr/0003-inline-execution-and-string-removal.md`;
  `docs/adr/0002-guard-type-checking-scope.md`; `docs/adr/0001-combinatorial-binding-search.md`;
  https://github.com/philgresh/cpnx/issues/25

---

## Context

In full CPN formalism, every arc — input or output — carries an *arc expression*: a typed
function relating the tokens on that arc to the place's colour set (`E : A → EXPR_V` with
`Type[E(a)] = C(p)_MS`, Jensen & Kristensen Def 4.2; see `docs/cpn-theory-audit.md`
Findings 1–2, which recorded exactly this mismatch). cpnx had collapsed both directions onto one field, `expression`, of one shape,
`Callable[[list[Token]], list[Token]]` on `InputArc` and `Callable[[list[Token]], bool]` on
`OutputArc`. The shared name suggested a shared mechanism. It was never one:

- On the **output** side, `expression` was always used as a boolean *activation* predicate —
  fire this arc or skip it. Every existing use, including `OutputArc.on_color()`, returns
  `bool`.
- On the **input** side, `expression` was a `list[Token] -> list[Token]` *selection*
  transform — the engine handed it the whole available pool and took the result as the
  consumption order/subset. In practice every real use was one of two shapes: a `sorted(...,
  key=...)` call, or a list-comprehension filter. Nothing in the corpus used the transform's
  full generality (e.g. positional truncation unrelated to sort or filter).

Naming both `expression` hid that the input side was doing token *selection* and the output
side was doing activation *decision* — two different CPN mechanisms wearing the same field
name.

The input side's opacity also had a concrete cost, distinct from the naming issue. Because a
`list[Token] -> list[Token]` callable is a black box to the engine, there is no way to
maintain a sorted or filtered structure across firings — the whole pool must be re-run
through the callable on every firing that touches the arc. `engine.py::_order_available`
(then operating over the single `expression` field) was consequently `O(N log N)` per call
with no persistence, so draining a deep expression-ordered place was `O(N^2 log N)` overall.
Every other selection path in the engine had already been linearized — the FIFO/timed
consumption path runs through `places._TokenStore`, an indexed structure maintained
incrementally — but an opaque transform can never be indexed, because there is no per-token
value to build an index *on*. A black-box function over a list has no interior to expose.

## Decision

### 1. `InputArc.expression` is removed; replaced by per-token `key` and `filter`

`InputArc` gains two keyword-only fields:

- `key: Callable[[Token], object] | None` — a pure per-token sort key. Eligible tokens are
  consumed in **ascending** key order (min-first), mirroring
  `Transition.binding_priority_key` (ADR 0001) for consistency across the two places cpnx
  asks a user for a sort key. Ties break by insertion order via a stable sort, so a keyed
  drain stays deterministic. Descending order is `key=lambda t: -f(t)` (or equivalent
  negation) — there is no separate `reverse=` flag, matching `binding_priority_key`'s
  precedent.
- `filter: Callable[[Token], bool] | None` — a pure per-token eligibility predicate. A token
  the filter rejects simply stays in the place; it is not an error and does not affect other
  arcs.

`engine.py::_order_available` applies `filter` first, then orders the survivors by `key`,
then the caller consumes the first `count`. With neither set, behavior is unchanged: `FIFO`.
A `key`/`filter` that raises — or a `key` whose values are mutually incomparable — makes the
arc unsatisfiable for that firing, exactly as a raising `expression` did before; this is not
a new failure mode, only a redistribution of the same one across two smaller callables.

Each of `key` and `filter` is purity-verified independently and carries its **own**
inline-safe flag (`_key_inline_safe` / `_filter_inline_safe`), computed the same way ADR
0003 computes it for guards: a callable that certifies as closed-world runs inline, under
the engine lock, with no timeout; an uncertified one runs on the existing timeout-bounded
expression pool, per token. Certification remains optional and additive — an uncertified
`key` or `filter` is fully supported, just costlier per evaluation. `filter` is a boolean
predicate, so it is subject to the same construction-time annotated-`-> bool` check as
`Transition.guard`/`OutputArc.condition` (ADR 0002): an annotated `-> int` filter raises
`TypeError` at construction, an unannotated one (every lambda) passes through unchecked.
`key` returns an arbitrary comparable, not a `bool`, so it is deliberately **not** subject to
that check — there is no single return annotation a sort key ought to have.

**This change is the API and correctness half only.** The per-firing filter+sort in
`_order_available` still re-scans and re-sorts the whole available pool on every firing that
touches the arc, so draining a deep keyed place is still `O(N^2 log N)` — unchanged from
before. What the split buys is *indexability*, not the index itself: a per-token `key` is a
value the engine could maintain a persistent sorted structure on incrementally, the way
`places._TokenStore` already does for FIFO/timed consumption; an opaque `list[Token] ->
list[Token]` transform never could be, because it has no interior for an index to attach to.
Building that index is deferred to
[issue #25](https://github.com/philgresh/cpnx/issues/25) and is explicitly **not** part of
this change — do not read this ADR as claiming the perf win has landed.

### 2. `OutputArc.expression` is renamed to `condition`

Same type (`Callable[[list[Token]], bool] | None`), same skip-the-arc-on-`False` semantics,
same construction-time annotated-`bool` check (ADR 0002) — this is a rename, not a behavior
change. `OutputArc.on_color()` now builds a `condition=` internally. The rename exists so the
field name is honest about what CPN calls this mechanism on the output side: an activation
condition, not a selection expression. It also removes the false symmetry with the (now
split) input side — after decision 1, `InputArc` no longer has any single field named
`expression` for `condition` to appear to parallel.

### 3. `key` is ascending/min-first, not descending

Chosen to mirror `Transition.binding_priority_key` (ADR 0001), which is also ascending/
min-first with an insertion-order tiebreak. A user who has already written a
`binding_priority_key` can reuse the identical key function (or the identical mental model)
for an `InputArc.key` — one min-first convention across both features, rather than two sort
conventions to remember depending on which knob is being turned. Descending order is one
negation away (`lambda t: -f(t)`, or equivalent for non-numeric keys) and this ADR does not
add a `reverse=` parameter to save that negation.

### 4. `consume_all` continues to bypass `key` and `filter`

A `consume_all=True` arc takes every available token in FIFO order, ignoring both
selection callables — a token the `filter` rejects is still consumed. This is exactly what
`consume_all` did to the old `expression`, and it is preserved deliberately rather than by
oversight.

It is, admittedly, the least comfortable decision here. `filter` reads as a declaration of
*eligibility*, and "consumed anyway" is not what a declaration of eligibility ought to
mean; under `expression` — a selection *ordering* — the bypass was unremarkable, because
"drain everything" plainly moots an ordering. The pull toward honoring `filter` under
`consume_all` is real. It was rejected for this change on scope grounds: nothing in cpnx's
corpus combines the two, so the change would be unmotivated by any actual use, and
"`consume_all` means all" is at least a rule that can be stated in one line and cannot
surprise anyone who has read it. Honoring `filter` under `consume_all` remains open as a
later change; it would be breaking, and should be made on purpose rather than as a
side effect.

What the bypass must not be is *silent*. Documentation only helps the reader who already
suspects there is something to look up, and the whole failure mode here is a caller who
believes `filter` means what it says. So the combination also emits a `UserWarning` at
construction — and on any reassignment that creates it — naming the specific parameters
being ignored. This deliberately warns rather than raises: the combination is
well-defined, existing code may rely on it, and `consume_all` is not made unusable by the
presence of a stray `key`. It is `warnings.warn`, not a print or a logger call, so callers
keep the standard filtering controls and test suites can promote it to an error.

Consequently the bypass is documented as an explicit warning on `InputArc`, commented at
the bypass in `engine.py::_resolve_input_tokens`, emitted at runtime by
`InputArc._warn_if_drain_ignores_selection`, and pinned by
`test_consume_all_bypasses_key_and_filter` (behavior) plus
`TestConsumeAllSelectionWarning` (the warning, including that it fires once for the
finished arc rather than once per conflicting field, and not at all without the conflict).
Callers who want "drain everything eligible" should use a large `count` rather than
`consume_all`.

### 5. Uncertified `key`/`filter`/`condition` remain fully allowed

Consistent with ADR 0003's certified-is-strictly-additive posture: certification changes
*how* a callable runs (inline vs. pooled), never *whether* it is accepted. Requiring
certification for `key`/`filter` is a possible later tightening — nothing here forecloses
it — but it is not today's rule, and an uncertified selection callable is exactly as valid
as an uncertified guard.

## Consequences

**Positive**

- Two honest, independently-named mechanisms (`key`, `filter`) replace one opaque transform,
  matching what the CPN literature's arc-expression concept actually decomposes into for
  every real selection use this project has seen.
- `filter` gets the same construction-time `-> bool` check as `guard`/`condition` for free,
  closing a gap where a mis-annotated selection callable on the input side previously had no
  return-type check at all (the old `expression` was `list[Token] -> list[Token]`, a shape
  ADR 0002's boolean check never covered).
- `key` and `filter` are independently certifiable and independently timeout-bounded — a
  cheap certifying `filter` paired with an expensive uncertified `key` (or vice versa) no
  longer forces both through the pool, the way one opaque `expression` necessarily did.
- Lays the necessary (but not sufficient) groundwork for a persistent per-arc key-index
  (issue #25): the per-token `key` is now a value, not a hidden step inside an opaque
  closure, so an index has something concrete to be built on.

**Negative / costs**

- **Breaking change.** `InputArc(expression=...)` no longer exists; any construction call
  using it raises `TypeError` (unknown keyword). See Migration below.
- An arbitrary `list[Token] -> list[Token]` transform that is neither a sort nor a filter —
  for example, positional truncation unrelated to any per-token property, or a transform
  whose output order depends on relationships *between* tokens rather than each token's own
  value — has **no direct equivalent** under `key`/`filter`. This is by design: such a
  transform cannot be decomposed into a per-token key and a per-token predicate, which is
  exactly the property that made it unindexable in the first place. Nothing in the existing
  corpus used this generality, so the migration cost across cpnx's own test suite and the
  private downstream corpus surveyed for ADR 0003 is zero, but a hypothetical arbitrary
  transform genuinely has nowhere to go.
- The `O(N^2 log N)` deep-drain cost for a keyed/filtered place is **unchanged** by this ADR.
  Anyone hoping this change alone fixes that performance characteristic will be disappointed;
  it is the prerequisite for issue #25, not the fix.

**Neutral**

- `OutputArc`'s rename (`expression` → `condition`) is a pure breaking rename with no
  behavioral change; it is bundled into this ADR because it removes the naming confusion this
  ADR is otherwise about, not because it shares a mechanism with the input-side split.

## Migration

- `InputArc(expression=lambda tokens: sorted(tokens, key=f))` → `InputArc(key=f)`.
- `InputArc(expression=lambda tokens: [t for t in tokens if p(t)])` → `InputArc(filter=p)`.
- A combined sort-then-filter (or filter-then-sort) `expression` → pass both `key=` and
  `filter=`; the engine always applies `filter` first, then `key`, regardless of the order
  written in an old combined callable, so re-check that this reordering doesn't change which
  tokens survive if the old callable filtered *after* sorting on a key that also depended on
  position.
- `OutputArc(expression=p)` → `OutputArc(condition=p)` (pure rename, no other change).
- An `expression` that performed a transform which is not a sort or a filter (e.g. positional
  truncation, or an order that depends on relationships between tokens rather than each
  token's own value) has no direct replacement under this API — see Consequences above. Such
  a transform must be restructured as (or replaced by) some combination of a per-token `key`
  and `filter`, or, if that is genuinely impossible, it is out of scope for what `InputArc`
  now expresses.

## Alternatives considered

- **Keep one `expression` field, just retype it per direction.** Rejected: it does not fix
  the indexability problem (the input side would still be an opaque `list[Token] ->
  list[Token]` transform), and it keeps the shared name implying a shared mechanism that
  never existed.
- **Add `key`/`filter` alongside the existing `expression`, deprecate later.** Rejected for
  the same reason ADR 0003 rejected keeping strings alongside callables: cpnx is pre-1.0 and
  actively clawing changes back into `[Unreleased]` rather than accumulating deprecated
  surface area a stabilizing 1.0 would then have to carry. A clean break costs nothing extra
  against a corpus with zero uses of the transform's undecomposable generality.
- **Build the persistent key-index in this same change.** Rejected for scope: this change is
  already breaking on its own; landing the index alongside it would couple an API redesign to
  a performance optimization and make either harder to review or revert independently. Tracked
  separately as issue #25.
- **A `reverse=` flag on `key` instead of ascending-only.** Rejected: it's one negation away
  for the caller, and adding it would make `InputArc.key` inconsistent with
  `Transition.binding_priority_key`, which has no such flag.
