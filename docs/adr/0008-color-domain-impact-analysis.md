# ADR 0008 — Declarative colour-domain blast-radius (impact) analysis

- **Status:** Accepted
- **Date:** 2026-08-03
- **Supersedes / relates to:** [0007](0007-best-effort-side-effect-linting.md)
  (best-effort side-effect linting — the **sibling safety prong**);
  [0006](0006-incremental-enablement.md) (place→transition routing / topology
  vocabulary reused by the tracer); the analysis module (`cpnx.analysis`).

## Context

`cpnx` admits **arbitrary Python callables** in transition `action`s, so the
engine cannot know which token colours a firing produces or mutates — the same
Halting-Problem wall ADR 0007 records for enabling inscriptions applies here to
*effects*. ADR 0007's linter answers **which** transitions reach outside the
decidable core (network / database / clock / randomness in guards, keys, and
filters). It does not answer the operator's next question: given a transition
that side-effects a token colour domain, **how far downstream can that effect
spread?** A lint finding names a hazard; it does not bound the hazard's reach.

High-level Petri nets as standardised in **ISO/IEC 15909-1:2019** (*Systems and
software engineering — High-level Petri nets — Part 1*) route tokens by colour
via structural arc inscriptions. `cpnx` deliberately keeps arc `key`/`filter`
selectors Turing-complete rather than restricting them to a decidable inscription
algebra, so the net's colour routing is not statically legible from the arcs
alone. That leaves the two prongs complementary but incomplete: the linter flags
*hazards*, and nothing bounds their *blast radius*.

We want a reachability-style bound that is **sound** (never hides a genuinely
reachable node) without pretending to a precision the language cannot support.
That is exactly the profile of a **cone-of-influence (COI) reduction**: prune the
model elements that cannot affect a property of interest by analysing the
dependency graph, keeping a slice that (bi)simulates the original with respect to
that property (Watanabe, Nishizawa & Takaki, "A Coalgebraic Representation of
Reduction by Cone of Influence," *ENTCS* 164 (2006) 177–194; invariant
**COALG-COI**). Its concurrent, over-approximate form — keep only the statements a
"real observer" can transitively observe, and prove that an over-approximate
slice never hides a reachable fault — is Telbisz, Bajczi, Szekeres & Vörös
("On-the-fly Cone-of-Influence Reduction for Model Checking Concurrent Software,"
SPIN'25; invariants **COI-OBSERVE**, **COI-REACHABLE**, **COI-SOUND**).

## Decision

Add `cpnx.analysis` — a declarative, forward, colour-gated COI slice over the
static net topology — and fuse it with ADR 0007's linter into one report.

### 1. Declare colours; do not infer them

A transition carries a new optional annotation
[`Transition.impacts_colors: frozenset[str] | None`][cpnx.Transition] naming the
token colour domain(s) it mutates or side-effects (e.g.
`impacts_colors={"order", "refund"}`). It is **purely declarative** — never
evaluated at run time, no effect on firing — in the spirit of ISO/IEC 15909-1:2019
arc inscriptions.

Declaration, not inference, is a necessity rather than a convenience: `cpnx`
cannot decide which colours an arbitrary Python `action` produces (the Halting
Problem, exactly as in ADR 0007). So impact is *declared* and the tracer uses the
declaration as a *precision* (Π, in the COI-OBSERVE sense) to prune the walk.

The field accepts any iterable of colour-name strings, coerced to a `frozenset`
by `coerce_color_domain`. A **bare `str` raises `TypeError`** (pass `{"order"}`,
not `"order"`, which would iterate to `{"o", "r", "d", "e"}`). `None` (the
default) means **undeclared**; an **empty set** is a distinct *narrowing*
declaration ("impacts no colour"). It is cheap to add incrementally — it changes
no existing behaviour.

### 2. `trace_impact` — a forward, colour-gated COI slice

`trace_impact(net, transition_name, *, colors=None) -> ImpactMap` (also
[`net.trace_impact(...)`][cpnx.PetriNet.trace_impact]) walks the static topology
**forward** by breadth-first search from the seed transition's **output** places,
hopping place → (consuming transition) → (its output places), terminating on
cycles (each transition is expanded once; the seed is never re-expanded).

The COI gate is a **colour-set disjointness test**: a downstream place is *pruned*
when its declared `color_set` is disjoint from the traced colour domain — the
place cannot carry a colour the effect deals in, so nothing reachable only through
it is in the cone. The walk reads only *structure* under the engine lock (the
place→consumers routing is rebuilt fresh each call, so it needs no scheduler state
and picks up transitions added after an earlier trace).

The result is an [`ImpactMap`][cpnx.ImpactMap] with fields `origin`, `colors`,
`places`, `transitions` (the seed **excluded**, even under a cycle back to it),
and `edges` (ordered, de-duplicated `(from, to)` pairs); `.to_dict()` is
JSON-serialisable and sorted for stable snapshots. Colour precision resolves as:
`colors=None` (default) uses the seed's `impacts_colors` declaration; an explicit
`colors=` **overrides** it for that trace (accepting the same shapes as the
field); an unknown `transition_name` raises `KeyError`.

Complexity is `O(V + E)` over places + arcs — consistent with the quadratic
data-flow-graph build bound of COI-DFG (Telbisz et al., SPIN'25, Def. 5).

### 3. Soundness stance: deliberate over-approximation

The slice is intentionally **over-approximate**, grounding its soundness in
**COI-SOUND** (Telbisz et al., SPIN'25, Theorem 1: an over-approximate slice
never hides a reachable fault) and **COALG-COI** (Watanabe et al., ENTCS 164
(2006): the reduced model (bi)simulates the original). Two rules make it sound by
construction:

- an **undeclared** origin (`impacts_colors is None`) traces **every** colour —
  no pruning — because no colour can be ruled out;
- a place whose **`color_set is None`** (accepts any colour) is **always**
  included — it cannot be excluded from a domain it does not constrain.

So an `ImpactMap` may *over-name* — list a place or transition a more precise
analysis would drop — but it never *omits* a node that is genuinely reachable
within the declared domain. Operators use it to **bound** risk, not to prove its
absence.

### 4. `risk_report` — fusing the two prongs

`risk_report(net) -> dict` (also [`net.risk_report()`][cpnx.PetriNet.risk_report])
is the verification / debugging entry point that fuses the two safety prongs. For
**every** transition it lints each enabling inscription (guard, arc `key`/`filter`,
`binding_priority_key`) via ADR 0007's `lint_callable`; for any transition that
either **trips the linter** *or* carries an explicit `impacts_colors` declaration,
it attaches that transition's blast radius. The shape is JSON-serialisable:

```json
{
  "findings": [
    {"transition": "<name>",
     "findings": [{"role": "<str>", "category": "<str>", "symbol": "<str>"}]}
  ],
  "impact_maps": {"<transition name>": "<ImpactMap.to_dict()>"}
}
```

`findings` lists only transitions with at least one lint finding; `impact_maps`
covers every transition that was linted-dirty **or** colour-annotated. In one
place it answers both "**which** transitions reach outside the decidable core?"
(ADR 0007) and "**how far** downstream can each one's effect spread?" (this ADR).

### 5. Visualisation integration

[`to_dot(net, *, highlight_impact_from=<transition name>)`][cpnx.to_dot] (also
[`net.to_dot(highlight_impact_from=...)`][cpnx.PetriNet.to_dot]) shades a blast
radius into the DOT export: impacted nodes are filled `#ffd9d9` and the seed
transition `#ff8080` with a heavier border. `None` (the default) leaves the
rendering exactly unchanged. `ImpactMap`, `trace_impact`, and `risk_report` are
exported from the top-level `cpnx` package.

## Consequences

- **Positive:** operators get a **sound** downstream bound — never omits a
  reachable node — that pairs with ADR 0007's linter to turn "this transition is a
  hazard" into "and here is exactly how far it can spread". The report is one
  JSON-serialisable object for CI / debugging, and the DOT highlighting makes a
  blast radius legible at a glance. The annotation is opt-in and behaviour-neutral,
  so it can be adopted incrementally with zero risk to existing nets.
- **Negative / limitations:** the trace is **conservative by construction** — it
  is topology + place-`color_set` based, not arc-inscription based, so it will
  over-name whenever declarations are coarse or a place is unconstrained
  (`color_set is None`). Because `cpnx`'s arc `key`/`filter` selectors are
  Turing-complete rather than structural colour inscriptions, the tracer cannot
  read per-arc colour routing; it prunes only at the place granularity. **Per-arc
  `impacts_colors` inscriptions** are recorded here as a possible future
  refinement — they would tighten the slice from place-level to arc-level
  precision — but they are not required for the sound bound this ADR delivers.
  The precision of any trace is only as good as the declarations it is given: an
  undeclared origin degrades to a universal (whole-net) trace.
- **Neutral:** `cpnx.analysis` shares ADR 0007's Halting-Problem framing and its
  "flag / bound, do not prove" honesty, but keeps a separate contract — the linter
  *classifies* transitions, the tracer *reaches* from them. The running
  demonstration is `benchmarks/impact_cafe.py` (with `tests/test_cafe_impact.py`),
  the impact-analysis companion to ADR 0007's `benchmarks/lint_cafe.py`.
