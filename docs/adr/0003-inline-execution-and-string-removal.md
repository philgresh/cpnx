# ADR 0003 — Inline Execution of Certified Callables & Removal of the String Expression Surface

- **Status:** Accepted — shipped in the callable-only-expressions work (Phases 1–3; 1.0.0)
- **Date:** 2026-07-21
- **Deciders:** cpnx maintainers
- **Related:** `docs/adr/0002-guard-type-checking-scope.md`;
  `docs/adr/0001-combinatorial-binding-search.md`; `docs/cpn-theory-audit.md`

---

## Context

cpnx evaluated guards and arc expressions through two divergent systems, chosen entirely by
*syntax* — whether the user wrote a string or a callable — rather than by anything proven about
the code:

|          | enforcement | execution | measured |
| -------- | ----------- | --------- | -------- |
| string   | **whitelist** (`SandboxEvaluator`) | inline | 0.26 µs |
| callable | **blocklist** (`verify_callable_purity`) | `ThreadPoolExecutor` round-trip | 9.6 µs |

Two security models and two execution models, with a ~90× cliff between them and no
documentation of it. Measured this session (M4 Pro, py3.14):

- Dispatching a **no-op** through the executor costs 9.636 µs — statistically identical to
  dispatching the real predicate (9.283 µs). The body contributes ~nothing; the cost is 100%
  dispatch.
- A direct callable call is **0.108 µs** — 2.4× faster than the compiled string path. The
  callable is already compiled; it loses only because it is wrapped in a thread.
- In the guarded Concurrency-Cafe benchmark this accounted for essentially the whole ~9×
  guarded slowdown (334k evaluations × ~10.2 µs ≈ 3.4s against a measured 3.47s penalty).

The defect: a self-contained callable is as safe as a string, yet paid 90× to run it. Nothing
about the *syntax* a user chose to express a guard should determine how much it costs to
evaluate.

**Precedent already exists for inlining without a timeout.** `_reduce_min_key` (`engine.py`)
calls `binding_priority_key` directly (`candidate_key = key_fn(flat)`) — inline, under the
engine lock, with no timeout — on the exact same binding-resolution path where guards and arc
expressions were paying the 9.6 µs executor round-trip. This ADR does not invent a new trust
posture; it makes an existing one consistent.

Two constraints framed the design:

1. **Side effects cannot be prevented in Python.** A name-based blocklist is evadable, any
   opaque call defeats it, and even attribute reads can have effects (ORM lazy-loading). The
   executor's `expr_timeout_secs` (`PetriNet.__init__`, `engine.py:255`) bounds *duration*, not
   *effects* — so the 9.6 µs never bought effect-safety, only time-bounding. A certifier must
   therefore be **closed-world** (only a fixed, library-controlled vocabulary), not
   effect-detecting, and the guard contract must be documented, not claimed as enforced.
2. **Guards are evaluated speculatively and repeatedly** — per transition per `step()`, and per
   candidate binding under `BindingPolicy.RANDOM`/`PRIORITY` (ADR 0001) — up to 334k times for
   500 orders in the Concurrency-Cafe benchmark. A side-effecting guard evaluated at that
   frequency is catastrophic, and nothing told users so.

## Decision

### 1. Certified callables run inline, under the engine lock, with no timeout

`src/cpnx/certification.py` supplies a closed-world verdict (`certify` / `is_inline_safe`) that
is deliberately a whitelist, not the effect-detecting blocklist in
`cpnx.sandbox.verify_callable_purity`. A callable certifies only if it draws solely on a fixed,
library-controlled vocabulary of builtins and methods, calls only user helpers that themselves
certify, iterates only over its own finite argument, and closes over nothing mutable. A
callable meeting all of these cannot loop forever and cannot reach outside the values it is
handed, so calling it inline without a timeout is sound.

`engine.py`'s four dispatch sites (`_order_available`, `_resolve_input_tokens`,
`_check_transition_guard`, `_is_arc_active`) branch on the flag `transitions.py` computes at
construction: `certified → fn(tokens)` inline, `else → _call_expr` (the executor path, still
bounded by `expr_timeout_secs`). Only uncertified callables keep the round-trip.

Justification:

- **Certification proves what the executor's timeout never did.** A closed, library-controlled
  vocabulary means a certified callable cannot diverge (bounded iteration, no unbounded
  recursion) and cannot reach outside its argument (no imports, no opaque calls, no mutable
  closure state). The executor's timeout only ever bounded *duration*; it was never an
  effect-safety mechanism to begin with, so removing it for the certified subset gives up
  nothing that was actually being enforced.
- **The engine already trusts inline calls on this exact path.** `_reduce_min_key` invokes
  `binding_priority_key` inline, under the lock, with no timeout, on the same resolution
  path guards and arc expressions traverse. Certification extends that same trust posture to
  guards and arc expressions instead of introducing a new one.

**Late-binding caveat — stated loudly, not buried.** Python resolves globals and free variables
at *call* time, not at construction time. Rebinding anything a certified callable references
(for example patching a helper with `unittest.mock.patch` after the transition is built)
silently substitutes uncertified code, which then runs inline with no timeout. AST analysis at
construction cannot see this; it is not closable by tightening the certifier. This is documented
as **undefined behaviour**, symmetric with two other frozen-at-construction rules already in
force: the closure-cell immutability requirement (every `__closure__` cell a certified callable
holds must be an immutable value) and a compiled string expression under the old sandbox, which
was likewise frozen at construction. Rejected mitigations:

- **Snapshotting resolved functions at certification time.** Breaks `mock.patch` and any other
  legitimate late-rebinding pattern, and diverges from ordinary Python name-resolution
  semantics that users otherwise expect.
- **Per-call identity guards** (re-verify a dependency's identity on every invocation). Costs a
  lookup per dependency per call, which erodes the inline win this ADR exists to capture.

### 2. The string expression surface is removed entirely (breaking)

`guard=` and `expression=` accept `Callable` only; passing a `str` to `Transition.guard`,
`InputArc.expression`, or `OutputArc.expression` raises `TypeError` at construction.
`SandboxEvaluator.evaluate_compiled` (the string-eval entry point) is deleted. The AST checker
that powered it — the `ALLOWED_BUILTINS` / `ALLOWED_METHODS` whitelist and its node-checking
logic — **survives**, repurposed as the certifier in decision 1: once that checker was extracted
to run over *any* AST rather than only a compiled string, the string sandbox and callable
certification became the **identical check**.

That equivalence is the whole rationale. Strings' one historical advantage — a stricter, closed
security model relative to the callable blocklist — evaporated the moment certification existed.
What remains genuinely unique to a string is that it can be **data**: loaded from JSON, a
database, or user input at runtime, with a shape unknown at authoring time. cpnx has no
net/guard serialization and no plan for one, so this capability has no consumer in the library —
it is exactly the data-defined-net feature this project is out of scope for.

The everyday reason anyone reaches for a string guard — parameterising a threshold or flag from
configuration — is fully covered by **config-via-construction-time-closure**: read the
configuration once, at net-build time, and close over the immutable result, which certifies
under the same rules as any other closure:

```python
threshold = config["max_weight"]                          # read once, at net-build time
guard = lambda toks: toks[0].payload["w"] <= threshold     # closes over an immutable float → certifies
```

Removing strings therefore costs nothing real while deleting a whole authoring surface, a
second security model, a documentation section, and a dispatch branch.

## Consequences

**Positive**

- The guarded hot path drops from an executor round-trip to a direct inline call; the guarded
  Concurrency-Cafe slowdown was essentially all dispatch, so this closes nearly the entire gap
  between guarded and guard-free throughput.
- One authoring surface (callables), one security model (closed-world certification), one
  documented execution contract.
- Certification is strictly additive over Phases 1–2: it never rejects anything that worked
  before, so the perf win landed before the breaking removal in Phase 3.

**Negative / costs**

- **Breaking change** — the string surface's removal gates the **1.0.0** major; any net
  constructed with a string guard or arc expression must migrate to a callable.
- Certification is closed-world and **advisory-not-enforced** for effects. The guard contract —
  a pure predicate over a candidate binding, evaluated an unbounded number of times, possibly
  under the engine lock — is documented, not mechanically enforced. A user can still write a
  certifying-but-effectful callable if they route the effect through the whitelisted vocabulary
  in a way that has no observable side channel the certifier checks for; nothing in this design
  claims otherwise.
- The late-binding caveat above is a genuine, permanent hole: it is a property of Python's name
  resolution, not a gap in the certifier's rules, and no mitigation considered was worth its cost.

**Neutral / deferred**

- **Executor batching stays deferred.** A corpus certification hit-rate of **88.7% combined**
  (81.7% across cpnx's own tests, 100% across the causal-trader downstream consumer) is
  comfortably over the ~80% go/no-go threshold set for this decision. Certification therefore
  covers the common case well enough that chunked-submit batching for the remaining uncertified
  callables is not worth building now; it remains available to revisit if the residual after
  this work proves material.

## Alternatives considered

- **Unify execution but keep both authoring surfaces.** Rejected: once the whitelist checker
  runs over any AST, strings' only remaining unique value is logic-as-data, which is out of
  scope for cpnx. Keeping strings alongside callables would preserve a second security model and
  a dispatch branch for no real gain.
- **A `trusted=True` escape hatch to force inlining of uncertified callables.** Rejected for
  v1: misplaced trust in an unproven callable is precisely the risk this work sets out to
  eliminate; adding a manual override reintroduces it by another name.
- **Snapshotting resolved functions / per-call identity guards** (mitigations for the
  late-binding caveat). Rejected as described in decision 1: snapshotting breaks legitimate
  rebinding patterns like `mock.patch`, and per-call identity checks erode the inline
  performance win.
