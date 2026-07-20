# CPN Theory Audit — `cpnx` vs Jensen & Kristensen (2009)

**Reference:** Kurt Jensen & Lars M. Kristensen, *Coloured Petri Nets: Modelling and
Validation of Concurrent Systems*, Springer 2009 (382 pp.).  
**Chapters consulted:** 4 (formal non-hierarchical CPNs), 6 (formal hierarchical
CPNs), 11 (formal timed CPNs).  
**Audit date:** 2026-07-01

---

## 0. Positioning — cpnx is a Workflow Engine Inspired by CPNs, not a CPN Simulator

Before reading the findings, understand the frame: **cpnx deliberately trades CPN's
declarative, anonymous-multiset semantics for an imperative, identity-bearing,
deterministic workflow-execution model.** Several "divergences" below (Findings 1, 2,
3) are not defects — they are the direct, intended consequences of that trade. This
section states the decision so it is not re-litigated as a conformance bug on each
re-reading.

**What CPN theory optimises for:** mathematical analysability — state-space
exploration, reachability, boundedness, liveness proofs. Tokens are anonymous
*values* drawn from a colour set; the marking is a multiset that counts colours;
enabling is "does *any* binding exist that satisfies the guard and arc expressions."

**What cpnx optimises for:** running real concurrent pipelines — enrichment, retry,
dead-lettering, rate-limiting, back-pressure, timeouts. Tokens are identity-bearing
*work items* (`id`, `payload`, `attempts`) that accumulate state via `evolve()`.

This produces three intentional departures:

| Concern | CPN theory | cpnx | Why the difference |
|---------|-----------|------|--------------------|
| Token production | output *arc expression* mints a multiset under the binding | transition `action` produces tokens; output arcs *route* them (bool predicate) | Actions do real work (I/O, enrichment); arcs are pipeline routing |
| Token consumption | `E(p,t)⟨b⟩` — a multiset value under a binding | `InputArc.expression` orders available tokens; engine takes first `count` | Selection must inspect identity/payload, not just colour |
| Enabling | a transition is enabled if *any* valid binding exists | first `count` tokens (FIFO or reordered) are the sole candidate | Determinism + reproducibility over combinatorial search |

**The cost of "fixing" this is not CPU — it is semantics.** Full CPN binding search
(lazy `itertools.product` + `next()` short-circuit) is survivable performance-wise.
The real blockers are: (1) **non-determinism** — if several bindings satisfy a guard,
CPN says "any"; a workflow engine needs a defined, reproducible winner; (2) **a
breaking behaviour change** — tokens previously FIFO-skipped would suddenly be
consumed, silently altering every existing net; (3) **identity vs. value** — cpnx's
retry/enrichment model keys on token identity, which combinatorial value-search
undermines.

**Consequence for this document:** Findings 1–3 are recorded as *deliberate
specialisations* with their theoretical delta made explicit, so the behaviour is
understood — not as bugs to close. The genuinely actionable items are the
**documentation gaps** (Findings 6, 7, 10) and the **one real correctness smell**
(Finding 11).

**Related history:** the input-side of this (Findings 2–3) was previously examined in a
v0.3.0 review that correctly identified **head-of-line (HoL) blocking** as the concrete
symptom: a place holding `[AAPL, MSFT]` whose guard wants `MSFT` reports the transition
disabled because only the head token `AAPL` is tested. The `InputArc.expression`
reordering hook was the interim escape hatch, pending a decision on true combinatorial
binding — since resolved by ADR 0001 below.

---

## 1. Formal CPN Invariants (Summary)

The following invariants are extracted verbatim from the book's formal definitions
(Defs 4.2–4.6, 6.1–6.7, 11.5–11.6).  They are the canonical constraints cpnx must
respect.

### 1.1 Non-Hierarchical CPN (Def 4.2)

A CPN is a nine-tuple `CPN = (P, T, A, Σ, V, C, G, E, I)` where:

| Element | Type | Constraint |
|---------|------|------------|
| `P` | finite set | places |
| `T` | finite set | transitions; `P ∩ T = ∅` |
| `A ⊆ (P×T) ∪ (T×P)` | set of directed arcs | bipartite graph; no P→P or T→T arcs |
| `Σ` | non-empty set | colour sets |
| `V` | finite set | typed variables; `Type[v] ∈ Σ` |
| `C : P → Σ` | colour-set function | each place has exactly one colour set |
| `G : T → EXPR_V` | guard function | `Type[G(t)] = Bool` |
| `E : A → EXPR_V` | arc expression function | `Type[E(a)] = C(p)_MS` (multiset over the connected place's colour set) |
| `I : P → EXPR_∅` | initialisation function | closed expression (no free variables) |

### 1.2 Enabling and Occurrence (Defs 4.3–4.5)

A **binding** `b` for transition `t` assigns a value to every free variable in
`Var(t)` (free variables from the guard and all arc expressions of `t`).

A **binding element** `(t, b)` is **enabled** in marking `M` iff:
1. `G(t)⟨b⟩ = true`  — guard satisfied under binding `b`
2. `∀p ∈ P : E(p,t)⟨b⟩ ≪= M(p)` — each input arc multiset ≤ marking

When `(t, b)` **occurs** the new marking `M'` is:
```
M'(p) = (M(p) —— E(p,t)⟨b⟩) ++ E(t,p)⟨b⟩   ∀p ∈ P
```

A **step** `Y` is a non-empty finite multiset of binding elements.  `Y` is enabled
iff every `(t,b) ∈ Y` satisfies the guard and ∀p: ∑_{(t,b)∈Y} E(p,t)⟨b⟩ ≪= M(p).

### 1.3 Hierarchical CPN (Defs 6.1–6.2)

- Each **module** is a non-hierarchical CPN with a designated set of port places and a
  port-type function `PT : P_port → {IN, OUT, I/O}`.
- Substitution transitions have **no arc expressions or guards** — they are not
  directly enabled; only their submodule's transitions can fire.
- A **port–socket relation** `PS(t)` maps port places in the submodule to socket
  places in the parent. Related places must have identical colour sets and identical
  initial marking expressions.
- Enabling and occurrence work on **compound places** (equivalence classes of place
  instances related by port–socket or fusion).

### 1.4 Timed CPN (Defs 11.5–11.6)

- Tokens on **timed places** carry a timestamp.
- A step `Y` is enabled at time `t'` iff: guards hold, untimed places have sufficient
  tokens, and timed places have sufficient **timed** tokens with timestamp `≤ t'`
  (strictly: the timed multiset sum of `E(p,t)⟨b⟩_{+t'}` ≪≪= `M(p)`).
- `t'` must be the **smallest** such value (no earlier firing is possible).
- When `Y` occurs at `t'`, tokens removed from timed places are selected by
  timestamp ≤ `t'`, and tokens added to timed places carry timestamp `t' + delay`.

---

## 2. Audit Findings

### Finding 1 — OutputArc.expression is a Routing Predicate, not an Arc Expression

**Severity:** Deliberate specialisation (see §0). Documentation gap only.

**Formal requirement:** `E : A → EXPR_V` maps each arc to an expression whose
evaluation `E(t,p)⟨b⟩` yields a **multiset of tokens** to be added to place `p`.
The type must satisfy `Type[E(a)] = C(p)_MS`.

**Implementation:** [`transitions.py:71`](../src/cpnx/transitions.py)
```python
expression: Callable[[list[Token]], bool] | str | None
```
`OutputArc.expression` receives the full list of output tokens and returns a `bool`.
If `False`, the arc is **skipped** (no tokens deposited).  It is a conditional
routing gate, not a token-producing expression.

**Impact:** The arc expression cannot transform or multiply tokens.  A transition
action returning `[token_a, token_b]` cannot use output arcs to send `token_a` to
place X and `token_b` to place Y via distinct arc expressions — it can only route
all-or-nothing based on a predicate on the whole output list.

**Recommendation:** Document explicitly that cpnx output arcs are routing predicates,
not CPN arc expressions.  Consider a future `OutputArc` mode that accepts a
`Callable[[list[Token]], list[Token]]` producing the exact multiset to deposit.

---

### Finding 2 — InputArc.expression is a Token-Ordering Function, not an Arc Expression

**Severity:** Deliberate specialisation (see §0). Documentation gap only. Concrete
symptom is head-of-line blocking, resolved opt-in by `BindingPolicy.FIRST` — see
`docs/adr/0001-combinatorial-binding-search.md`.

**Formal requirement:** `E(p,t)⟨b⟩` evaluates to a multiset that is consumed
from place `p`.  The key point is that arc expressions are evaluated **under a
binding** — they produce a value determined by the variable assignment, not by
inspecting tokens already in the place.

**Implementation:** [`transitions.py:37`](../src/cpnx/transitions.py)
```python
expression: Callable[[list[Token]], list[Token]] | str | None
```
`InputArc.expression` receives the currently-available tokens and returns them in a
preferred consumption order.  The engine then takes the first `count` from that
ordering.  This is a **token-selection function**, not a multiset expression
evaluated under a variable binding.

**Impact:** Selection is token-identity-aware (can inspect `payload`, `color`,
`available_at`, etc.) which is more powerful in some ways but loses the algebraic
guarantee that the consumed multiset is always exactly `E(p,t)⟨b⟩` for some `b`.

**Recommendation:** Document as "inspired by CPN arc expressions, adapted for
identity-bearing tokens."

---

### Finding 3 — No Binding Concept

**Severity:** Deliberate specialisation (see §0). True combinatorial binding search is
a planned future feature — see `docs/adr/0001-combinatorial-binding-search.md` for the
deterministic-complete design that resolves the determinism/migration questions.

**Formal requirement:** The enabling rule (Def 4.4) is stated in terms of a
**binding** `b` — a variable assignment over `Var(t)`.  Guards and arc expressions
are both evaluated under the same binding, ensuring algebraic consistency.  The guard
`G(t)⟨b⟩` and each arc expression `E(p,t)⟨b⟩` share the same variable values.

**Implementation:** [`engine.py:570-579`](../src/cpnx/engine.py)
Guards and arc expressions both receive the candidate token list, but there is no
single binding object shared between them.  The guard and input arcs are evaluated
independently.

**Impact:** In theory, a guard can inspect the same variable values used to compute
consumed multisets — this consistency guarantee is absent.  In practice, the guard
receives the tokens that *would* be consumed, which approximates the intent but is
not the same formal object.

**Recommendation:** Acceptable for a practical executor; document the distinction.

---

### Finding 4 — Back-Pressure Check in Enabling Rule

**Severity:** Minor extension beyond CPN theory.

**Formal requirement:** The enabling rule (Def 4.4) only checks:
1. Guard is true.
2. Each input arc multiset ≤ place marking.

Output place capacity is **not** a condition of enabling in standard CPN theory.

**Implementation:** [`engine.py:563-568`](../src/cpnx/engine.py)
```python
for arc in transition.outputs:
    if arc.expression is not None:
        continue
    place = self.places.get(arc.place)
    if place is not None and not place.can_deposit(arc.count):
        return False
```
Transitions with unguarded output arcs to bounded places are blocked when the output
would exceed capacity.

**Assessment:** This is a standard implementation extension for bounded places (k-safe
nets).  It is not part of the CPN enabling rule but is necessary for correct
execution of bounded-place models.  The exception for guarded arcs is correct: since
a guarded arc may not fire, its destination capacity should not block enabling.

**Recommendation:** This extension is appropriate. The comment at
[`engine.py:560`](../src/cpnx/engine.py) already says "Back-pressure" — good.

---

### Finding 5 — Token Identity (UUID) has no CPN Theoretical Counterpart

**Severity:** Extension (not a divergence).

**Formal requirement:** In CPN theory, a token is a **value** drawn from a colour
set.  Tokens have no identity — only their colour (value) matters.  The multiset
`M(p)` counts how many tokens of each colour are present; the individual "instances"
are indistinguishable.

**Implementation:** [`tokens.py:105`](../src/cpnx/tokens.py)
```python
id: str = field(default_factory=lambda: uuid4().hex[:16])
```
Every token has a unique UUID.  `retrieve_specific` ([`places.py:121`](../src/cpnx/places.py))
operates on token identity.

**Impact:** Token identity enables useful engineering features: retry tracking
(`attempts`), payload accumulation, and `evolve()` semantics.  However, it means
two tokens with the same `color` and `payload` are *not* interchangeable, which is a
stronger distinction than CPN theory requires.

**Assessment:** Intentional and well-documented extension.  No action needed.

---

### Finding 6 — Substitution Transitions Have No Arc Expressions in Theory, but cpnx Requires Them

**Severity:** Structural divergence in hierarchical support.

**Formal requirement (Def 6.1, §5.1, §6.4):** Substitution transitions are **not**
directly enabled.  They have no arc expressions or guards.  Token exchange with
submodules occurs through the port–socket relation — related port and socket places
share the same marking (compound place).

**Implementation:** [`transitions.py:156`](../src/cpnx/transitions.py)
`SubstitutionTransition` extends `Transition` and keeps `inputs`/`outputs`.
[`engine.py:841-890`](../src/cpnx/engine.py) (`_execute_substitution_transition`):
- Consumes tokens from parent socket places via the parent's normal input arcs.
- Deposits them into subnet port places.
- Runs the subnet.
- Retrieves tokens from subnet output ports.

This is a **sequential handoff** model, not the simultaneous compound-place model of
the book.  The key differences:
1. In theory, port and socket places **are the same compound place** and always share
   the same marking.  In cpnx they are separate places bridged by an explicit copy.
2. In theory, the parent's subnet transitions can interleave with each other (they
   are simply transitions in a larger flat net after unfolding).  In cpnx, the
   subnet runs to **quiescence** before tokens are returned to the parent — it is a
   synchronous call, not asynchronous interleaving.
3. The `subnet_deadline_secs` timeout has no CPN-theoretic counterpart.

**Impact:** The sequential model prevents subnets from producing output tokens
concurrently with the parent.  It is simpler to reason about but not behaviourally
equivalent to hierarchical CPN unfolding.

**Recommendation:** Document that cpnx hierarchical execution is a sequential
execution model, not an unfolding to a flat net.  Add a note that the subnet runs to
quiescence before tokens are returned — so any liveness property of the subnet must
be verified independently.

---

### Finding 7 — Port/Socket Boundary: Token is Cloned, not Shared

**Severity:** Minor divergence from hierarchical CPN semantics.

**Formal requirement:** Related port and socket places form a **compound place** —
they are literally the same place viewed from two modules.  Depositing into the
socket is the same event as depositing into the port.

**Implementation:** [`engine.py:867`](../src/cpnx/engine.py)
```python
subnet.deposit(port_name, token.evolve())
```
`evolve()` creates a **new token** with a new UUID.  The original socket token and
the port token are distinct objects.

**Impact:** The `attempts` counter and `payload` are copied, but the `id` differs.
Any downstream logic keying on token identity across the subnet boundary will see
different IDs.

**Recommendation:** Acceptable given the sequential model.  Document that token IDs
are not preserved across port/socket boundaries.

---

### Finding 8 — Timed CPN: `advance_time` is Manual, Not Automatic

**Severity:** Usability gap (not a semantic error).

**Formal requirement (Def 11.6, item 5):** In a timed CPN, the global clock
advances to the **smallest time** at which any step is enabled.  Time advancement is
part of the occurrence rule — it is not a separate explicit operation.

**Implementation:** [`engine.py:168-178`](../src/cpnx/engine.py)
The clock only advances when the user calls `advance_time(new_time)` explicitly.
There is no automatic time advancement to the smallest `available_at` among tokens.

**Impact:** Without explicit `advance_time` calls, timed tokens with future
`available_at` values will never become available, causing permanent blocking.  The
net will appear dead (quiescent) rather than time-blocked.

**Recommendation:** Add a helper method (e.g. `advance_to_next_event()`) that
automatically advances `_model_time` to the smallest pending time gate and wakes the
run loop.  This would bring timed execution much closer to the formal semantics.

**Two traps that recommendation must account for.** Both were found empirically while
building `benchmarks/_driver.py`, which is a working implementation of this helper and
can serve as a reference:

1. **`min(token.available_at)` is not sufficient.**  Token cooldowns are only *one*
   class of time gate.  Input-arc **settle windows** are a second, and they are
   invisible to that formula — a settle window lives on `arc.settle_secs` measured
   against `place.last_deposit_time_model`, not on any token.  A net whose only
   pending gate is a settle window will not advance, and will look dead.

2. **The boundary can round to exactly the present.**  `last_deposit + settle_secs`
   is mathematically in the future whenever the window is unmet, but the model clock
   carries `time.monotonic()` values around 7.4e6 where one float64 ULP is ~1e-9.  At
   that magnitude the sum can round to exactly `model_time`, so a `boundary > now`
   test fails while the engine's own `elapsed >= settle_secs` is *also* false by a
   fraction of a nanosecond.  Neither side can advance and the net strands.  Floor the
   computed boundary at `math.nextafter(now, math.inf)`.

The failure mode is quiet, which is the dangerous part: a driver hitting trap 2 stops
and reports a completed run.  Observed at 100 orders in the cafe benchmark — six
tokens stranded in a `ThresholdPlace`, `is_quiescent()` returning `False`, 97 of 100
orders served, and the benchmark printing its results table regardless.

Note this only bites logical-clock driving.  Under `run()` the wall clock keeps
advancing past the boundary on the next poll, so the window resolves on its own.

---

### Finding 9 — Timed CPN: `is_quiescent` May Return True When Net is Only Time-Blocked

**Severity:** Semantic gap in quiescence detection.

**Formal requirement:** A timed CPN is in a **dead marking** only if no step is
enabled at any future time.  A net with tokens whose `available_at > current_time`
is time-blocked, not dead.

**Implementation:** [`engine.py:390-407`](../src/cpnx/engine.py)
`is_quiescent()` calls `_is_transition_potentially_enabled()` with
`model_time=float("inf")` — so it *does* consider tokens in cooldown as
potentially-available.  This is documented as intentional for `PacedResourcePlace`.

However, `is_dead()` ([`engine.py:423-435`](../src/cpnx/engine.py)) uses
`_is_transition_enabled()` with the current model time.  If time has not been
advanced, a time-blocked net will appear dead even though transitions would be
enabled at a future time.

**Recommendation:** Document the distinction between `is_dead()` (current-time
check) and `is_quiescent()` (ignores timing).  Add a method
`is_time_blocked() -> bool` that returns True when the net is not quiescent but `is_dead()` is True.

---

### Finding 10 — Guard Receives Token List, Not a Binding

**Severity:** Documentation issue.

**Formal requirement:** `G(t)⟨b⟩` — guard evaluated under binding `b`.

**Implementation:** [`transitions.py:138`](../src/cpnx/transitions.py)
```python
guard: Callable[[list[Token]], bool] | str | None = None
```
The guard receives `candidate_tokens` — the list of tokens that would be consumed
([`engine.py:573-578`](../src/cpnx/engine.py)).

The docstring says "boolean predicate over the binding" which is misleading: the
guard does not receive a `b` object but a list of actual candidate tokens.

**Recommendation:** Update the docstring to say "boolean predicate over the candidate
input tokens" and clarify that it is evaluated after token selection, not before.

---

### Finding 11 — Comment at engine.py:684 Does Not Match Code

**Severity:** Documentation bug.

**Code:** [`engine.py:684`](../src/cpnx/engine.py)
```python
# Resource arcs are never guarded — resources must always return to a place.
...
for arc in transition.outputs:
    is_res = isinstance(self.places.get(arc.place), (ResourcePlace, PacedResourcePlace))
    if arc.expression is None:
        active_outputs.append((arc, is_res))
    elif isinstance(arc.expression, str):
        if SandboxEvaluator.evaluate_compiled(arc._compiled_expression, {"tokens": output_tokens_data}):
            active_outputs.append((arc, is_res))
    elif self._call_expr(arc.expression, output_tokens_data, ...):
        active_outputs.append((arc, is_res))
```

The comment says "Resource arcs are never guarded" but the code evaluates
`arc.expression` for resource arcs too — if `arc.expression` returns `False`, the
resource arc is skipped.  This contradicts the stated invariant and could cause
resource tokens to be permanently lost (not returned to their source place).

**Recommendation:** Either:
- Enforce the invariant by checking `is_res` before evaluating `arc.expression` and
  always including resource arcs in `active_outputs` regardless of expression; or
- Remove the comment and document that resource arc expressions are evaluated but
  unevaluated-to-False resource arcs trigger an error rather than silent loss.

The current behaviour is a **potential token-conservation bug**: a resource arc with
an expression that returns `False` will not add the resource to `active_outputs`,
so the resource token is not returned to its source place (it falls into the
`while res_deque:` cleanup at line 768, which *does* return leftover resources —
so there is a safety net, but the comment is still misleading).

---

### Finding 12 — SinkPlace.peek Raises ValueError, Inconsistent with can_retrieve→False

**Severity:** Minor interface inconsistency.

**Implementation:** [`places.py:548-550`](../src/cpnx/places.py)
```python
def peek(self, count: int = 1, model_time: float | None = None) -> list[Token]:
    """SinkPlace is terminal — raises ValueError."""
    raise ValueError("SinkPlace is terminal — tokens are absorbed, not retrievable")
```

The enabling check in `_is_transition_enabled` ([`engine.py:531`](../src/cpnx/engine.py))
calls `can_retrieve` first; since `SinkPlace.can_retrieve` always returns `False`, a
`SinkPlace` will never be used as an *input* place during normal operation.  However,
callers that call `peek` defensively on an arbitrary place will get a `ValueError`
rather than `[]`.

**CPN expectation:** `peek` on an empty place should return `[]`.  A SinkPlace from
the outside looks empty (`can_retrieve → False`), so `peek → []` would be consistent.

**Recommendation:** Change `peek` to return `[]` (or `self._tokens[:count]` for the
ring-buffer case with `keep_last > 0`) rather than raising.

---

### Finding 13 — step() Fires One Transition at a Time (No True Concurrent Steps)

**Severity:** Semantic simplification.

**Formal requirement (Def 4.5):** A step `Y` is a non-empty finite **multiset** of
binding elements that are all enabled concurrently (they do not compete for the same
tokens).  Theorem 4.7 allows decomposing a concurrent step into sequential
sub-steps; but formally, concurrent steps are the primitive.

**Implementation:** [`engine.py:267-273`](../src/cpnx/engine.py)
`step()` fires exactly **one** transition per call.  Concurrency comes from the
thread pool (multiple transitions can be *executing their actions* concurrently), but
the token-consumption step is serialised under `self._lock`.

**Assessment:** This is the standard simulator approach — Theorem 4.7 justifies it.
Any concurrent step can be decomposed into a sequence of single-element steps
without changing the final marking.  No action needed.

---

### Finding 14 — ThresholdPlace Encodes a Guard on the Place, not the Transition

**Severity:** Extension (semantic sugar).

**Formal requirement:** The threshold `|M(p)| >= k` is a guard condition that belongs
on the **transition**, not on the place.  In standard CPNs, places are passive.

**Implementation:** [`places.py:375`](../src/cpnx/places.py)
The threshold is a property of the `ThresholdPlace` and enforced in `can_retrieve`.

**Assessment:** Valid engineering shorthand — the docstring correctly says it is
equivalent to adding `|M(p)| >= threshold` to every downstream transition's guard.
No semantic error, but users should be aware this guard is hidden inside the place.

---

## 3. Summary Table

| # | Area | Severity | Status |
|---|------|----------|--------|
| 1 | OutputArc.expression is a bool predicate | Deliberate (§0) | Documentation gap |
| 2 | InputArc.expression is a token-ordering function | Deliberate (§0) | Documentation gap; HoL symptom |
| 3 | No binding concept | Deliberate (§0) | Deferred feature; documentation gap |
| 4 | Back-pressure in enabling rule | Minor extension | Correct; well-commented |
| 5 | Token UUID identity | Extension | Intentional; documented |
| 6 | SubstitutionTransition: sequential, not compound-place | Structural divergence | Intentional; needs documentation |
| 7 | Token cloned across port/socket boundary | Minor divergence | Acceptable; needs documentation |
| 8 | No automatic time advancement | Usability gap | Enhancement opportunity |
| 9 | is_dead() may misread time-blocked nets | Semantic gap | Enhancement opportunity |
| 10 | Guard docstring says "binding" but receives token list | Documentation bug | Fix docstring |
| 11 | Comment "resource arcs never guarded" contradicts code | Documentation + potential bug | Fix comment or enforce invariant |
| 12 | SinkPlace.peek raises instead of returning [] | Minor interface inconsistency | Fix peek |
| 13 | step() fires one transition at a time | Simplification | Justified by Theorem 4.7 |
| 14 | ThresholdPlace encodes guard on place | Extension | Documented shorthand |

---

## 4. Recommended Actions

**High priority (correctness/safety):**
1. **Finding 11** — Audit resource arc guard evaluation path; ensure resource tokens
   are never silently lost when `arc.expression` returns False.  Update or remove the
   misleading comment.

**Medium priority (documentation):**
2. **Findings 1, 2, 3** — Add a `docs/cpn-alignment.md` (or section in README)
   explaining how cpnx arc expressions and guards differ from CPN theory, and why.
3. **Finding 6** — Document that hierarchical execution is sequential (not compound-place
   unfolding) and what that means for liveness analysis.
4. **Finding 10** — Fix guard docstring in `Transition`.

**Low priority (enhancements):**
5. **Finding 8** — Add `advance_to_next_event()` helper for timed nets.
6. **Finding 9** — Add `is_time_blocked()` for clearer timed-net diagnostics.
7. **Finding 12** — Change `SinkPlace.peek` to return `[]` instead of raising.
