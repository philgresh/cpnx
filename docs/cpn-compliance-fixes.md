# CPN Compliance Fix Plan

This document is a task list for an AI agent to address the 28 findings from a formal
CPN compliance audit of the petriq library. Work top-to-bottom — each tier unblocks the next.

**Repo:** `/Users/phil/workdir/github/philgresh/petriq`
**Run tests after each tier:** `.venv/bin/pytest tests/ -q && .venv/bin/ruff check src/ tests/`

---

## Tier 1 — Critical (fix first)

### C1 · `BaseException` not caught in action executor
**Edict:** X (Atomicity)
**File:** `src/petriq/engine.py` ~line 518–530

The `except Exception` block around the `action` call does not catch `KeyboardInterrupt`,
`SystemExit`, or `GeneratorExit`. When these occur: `_running_count` is never decremented,
consumed tokens are lost forever, and `is_quiescent()` permanently returns `False`.

**Fix:** Wrap the entire `_execute_transition` body in a `try/finally` that unconditionally
decrements `_running_count` and calls `self._work_available.set()`. Change `except Exception`
to `except BaseException` with a re-raise after cleanup.

```python
# Desired shape:
try:
    result = transition.action(consumed_tokens)
except BaseException as exc:
    success = False
    exc_info = exc
finally:
    with self._lock:
        self._running_count -= 1
        self._work_available.set()
```

---

## Tier 2 — High

### H1 · `FrozenDict._data` is a mutable backdoor
**Edict:** II (Immutability)
**File:** `src/petriq/tokens.py` lines 8–18

`FrozenDict._data` is a plain `dict`. Any code with a reference can write
`fd._data['key'] = value` and silently corrupt a token's payload.

**Fix:** After constructing and freezing `_data` in `__init__`, reassign it to
`types.MappingProxyType(frozen_dict)` so the backing store itself is read-only.
Add `__slots__ = ('_data', '_hash')` to prevent new attribute injection.

```python
import types

class FrozenDict:
    __slots__ = ('_data', '_hash')

    def __init__(self, data: dict):
        # freeze recursively first, then wrap in MappingProxyType
        frozen = {k: FrozenDict(v) if isinstance(v, dict) else v for k, v in data.items()}
        object.__setattr__(self, '_data', types.MappingProxyType(frozen))
        object.__setattr__(self, '_hash', None)
```

Update `__setattr__` (if present) and any `_data` mutation sites — there should be none
after this change since `MappingProxyType` blocks them at the C level.

---

### H2 · `Token.evolve()` reuses the parent's `id`
**Edict:** II (Immutability)
**File:** `src/petriq/tokens.py` lines 92–99

`dataclasses.replace(self, **new_fields)` copies `id` from the original token.
Destroyed and produced tokens share one identity, breaking id-based deque operations
and making error-routed and output tokens indistinguishable.

**Fix:** Inject a fresh `id` unless the caller explicitly passes one:

```python
def evolve(self, **new_fields) -> "Token":
    new_fields.setdefault("id", uuid4().hex[:8])
    return dataclasses.replace(self, **new_fields)
```

---

### H3 · Partial output deposit has no rollback
**Edict:** X (Atomicity)
**File:** `src/petriq/engine.py` lines 573–578

The output distribution loop calls `_deposit_under_lock()` one token at a time.
If `deposit()` raises mid-loop (e.g. `color_set` mismatch), some tokens land and
some don't — partial state with no rollback.

**Fix:** Validate all deposits in a pre-flight pass before touching any place, then
apply all-or-nothing:

1. For each `(arc, token)` pair, call `place.can_accept(token)` (a new non-mutating
   check that runs the same validation as `deposit()` but doesn't enqueue).
2. Only if all checks pass, loop again and actually deposit.
3. On pre-flight failure, set `success = False` and let the existing error path handle routing.

Add `Place.can_accept(token) -> bool` that mirrors the `color_set` and `bound` checks
without modifying state.

---

### H4 · `InputArc`/`OutputArc`/`Transition.guard` callables are entirely unsandboxed
**Edict:** VII (Pure Evaluation)
**File:** `src/petriq/engine.py` lines 228, 427, 449, 478, 491, 550–551

The string-expression path goes through `SandboxEvaluator`; the callable path does not.
A closure that captured `requests`, a DB session, or the filesystem runs unimpeded.

**Fix (pragmatic — no subprocess isolation required):**
1. Add a `timeout_secs: float = 1.0` parameter to `PetriNet.__init__`.
2. Wrap every guard/expression callable invocation in a `concurrent.futures` call with
   that timeout, running on a separate executor from the action pool:

```python
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

_EXPR_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cpnx-expr")

def _call_expr(fn, *args, timeout=1.0):
    fut = _EXPR_EXECUTOR.submit(fn, *args)
    try:
        return fut.result(timeout=timeout)
    except FuturesTimeout:
        raise RuntimeError(f"Expression {fn!r} exceeded {timeout}s — possible I/O call")
```

3. Document clearly that full hermetic sandboxing requires string expressions through
   `SandboxEvaluator` and that callable expressions are timed but not I/O-isolated.

Note: full closure isolation (preventing captured module references) requires subprocess
execution and is out of scope here — document that limitation explicitly in the README.

---

### H5 · Subnet place topology mutated at runtime by parent
**Edict:** VI (Encapsulation)
**File:** `src/petriq/engine.py` lines 657–659

`_execute_substitution_transition()` calls `subnet.add_place(Place(port_name))` when
a port isn't pre-registered. The subnet's topology is thus defined by the parent at
execution time, not by the subnet author at construction time.

**Fix:** Validate in `SubstitutionTransition.__post_init__` that every port name in
`port_socket_map` already exists in `subnet.places`. Remove the dynamic `add_place` fallback.

```python
def __post_init__(self):
    missing = [p for p in self.port_socket_map if p not in self.subnet.places]
    if missing:
        raise ValueError(
            f"SubstitutionTransition '{self.name}': subnet has no places for ports {missing}. "
            "Pre-declare port places in the subnet before wrapping it."
        )
```

---

### H6 · Parent writes `_model_time` into child net without child's lock
**Edict:** VI (Encapsulation)
**File:** `src/petriq/engine.py` lines 662–663

`subnet.advance_time(self._model_time)` is called from the parent's thread pool worker
without holding the child's lock. If two parent transitions reference the same subnet
instance this races.

**Fix:** Either:
- Require subnet instances to be unique per `SubstitutionTransition` (enforce in
  `__post_init__` — two transitions may not share a subnet object), OR
- Acquire `subnet._lock` before calling `subnet.advance_time()`.

The simpler option is uniqueness enforcement — subnets as shared mutable singletons
are an anti-pattern here regardless.

---

### H7 · settle_secs always uses wall clock, ignoring model time
**Edict:** V (Chronology)
**File:** `src/petriq/engine.py` line 416

`settle_secs` enforcement computes `time.monotonic() - place.last_deposit_time`.
When the net operates in logical-clock mode (`advance_time()` was called), settle
windows are still governed by real time — the model clock has no effect on them.

**Fix:** Add a parallel `last_deposit_time_model: float` attribute to
`PacedResourcePlace`. When `_model_time` is set, populate that attribute from
`model_time` (not `time.monotonic()`) and use it in the settle check:

```python
# In engine._is_transition_enabled():
if self._model_time is not None:
    elapsed = self._model_time - place.last_deposit_time_model
else:
    elapsed = time.monotonic() - place.last_deposit_time
if elapsed < arc.settle_secs:
    return False
```

---

## Tier 3 — Medium

### M1 · `FrozenDict._hash` is mutable
**Edict:** II (Immutability)
**File:** `src/petriq/tokens.py` line 18

With `__slots__` added in H1, `_hash` is already slot-protected. Compute the hash
eagerly in `__init__` rather than lazily, so there is no post-construction mutation at all:

```python
object.__setattr__(self, '_hash', hash(tuple(sorted(self._data.items()))))
```

Remove the lazy-compute branch in `__hash__`.

---

### M2 · `PacedResourcePlace.deposit()` clock-domain mismatch
**Edict:** V (Chronology)
**File:** `src/petriq/places.py` line 300

`last_deposit_time` is always written as `time.monotonic()` even when `model_time` is
passed. Downstream of H2 (`Token.evolve()` gets a fresh id) and H7 (dual clock
attributes), update `deposit()` to use `model_time` for `last_deposit_time_model` when
set, and `time.monotonic()` otherwise for the wall-clock attribute.

---

### M3 · Pre-flight supply failure routes data tokens to `error_place`, not source
**Edict:** X (Atomicity)
**File:** `src/petriq/engine.py` lines 553–571

When the action returns too few tokens to satisfy output arcs, data tokens go to
`error_place` rather than being returned to their source places. True rollback would
return them so the transition can be retried.

**Fix options (pick one):**
- **Option A (true rollback):** Return data tokens to `token_sources[place_name]` rather
  than `error_place`. The transition will re-enable and may fire again.
- **Option B (document as designed):** Add a docstring to `_execute_transition` explicitly
  stating that supply failures are terminal for the token — it is routed to `error_place`
  for inspection rather than retried.

Option B is acceptable if the library's contract is "actions are responsible for producing
the right number of tokens." Document it clearly so users don't expect retry behaviour.

---

### M4 · `add_transition()` does not reject Transition→Transition arcs
**Edict:** VIII (Bipartite Topology)
**File:** `src/petriq/engine.py` lines 407–410

A user who wires `InputArc("my_transition_name")` gets a silent `False` from
`_is_transition_enabled()` rather than a structural error. The bipartite constraint
is not enforced at construction time.

**Fix:** In `add_transition()`, after the transition is registered, cross-check all arc
place names:

```python
for arc in transition.inputs + transition.outputs:
    if arc.place in self.transitions:
        raise TypeError(
            f"Arc target '{arc.place}' is a Transition, not a Place. "
            "Arcs must connect Places↔Transitions only."
        )
```

Note: this check works only for places already registered. For arcs that name places
not yet added, the check is deferred to `run()` — add a `validate()` method that does
a full topology check and call it at the start of `run()`.

---

### M5 · `_running_count` not decremented on `MemoryError`/`RecursionError`
**Edict:** X (Atomicity)
**File:** `src/petriq/engine.py` lines 527–530

Covered by the `try/finally` added in C1 — no separate change needed if C1 is
implemented as a `finally` block wrapping the whole `_execute_transition` body.

Verify C1's `finally` runs in the `MemoryError` case by adding a test.

---

### M6 · `verify_callable_purity` silently passes uninspectable callables
**Edict:** VII (Pure Evaluation)
**File:** `src/petriq/sandbox.py` lines 97–100

`inspect.getsource()` fails on compiled extensions and `functools.partial`. The
outer `except` swallows the error and accepts the callable with "allow with caution."

**Fix:** Raise `PermissionError` (or a new `ImpureCallableError`) when source is
unavailable:

```python
except OSError:
    raise PermissionError(
        f"Cannot verify purity of {fn!r}: source unavailable. "
        "Use a plain lambda or def-statement function instead."
    )
```

---

### M7 · Subnet token identity leaks across port/socket boundary
**Edict:** VI (Encapsulation)
**File:** `src/petriq/engine.py` lines 655–659

Tokens are deposited into the subnet as the same Python objects. After H2 (`evolve()`
gets a new id), call `token.evolve()` when crossing the port boundary so each token
gets a fresh identity scoped to the child net:

```python
subnet.deposit(port_name, token.evolve())
```

---

## Tier 4 — Low / Documentation

### L1 · `advance_time()` allows equal timestamps (not strictly monotonic)
**File:** `src/petriq/engine.py` line 122

Change `if new_time < self._model_time` to `if new_time <= self._model_time`.

---

### L2 · `Token.available_at = 0.0` sentinel is undocumented
**File:** `src/petriq/tokens.py` line 78

Document that `0.0` means "immediately available" (not a real monotonic timestamp).
Add a named constant `AVAILABLE_NOW: float = 0.0` and use it as the default.

---

### L3 · `marking` property has wrong return-type annotation and stale docstring
**File:** `src/petriq/engine.py` lines 300–312

- Change annotation from `dict[str, list[Token]]` to `dict[str, tuple[Token, ...]]`.
- Remove or correct the warning "mutating them affects the net" — Token is frozen.

---

### L4 · `FrozenDict.copy()` name is misleading
**File:** `src/petriq/tokens.py` line 44–45

Rename to `as_dict()` to prevent confusion with `dict.copy()` idioms that imply
a mutable working copy. Update all call sites.

---

### L5 · `action` should not be verified by `verify_callable_purity`
**File:** `src/petriq/transitions.py` line 102

Actions are explicitly allowed to have side effects (they call DBs, LLMs, etc.).
Running them through the same purity check as guards conflates two fundamentally
different callable classes. Remove the `verify_callable_purity(action)` call and
add a comment explaining the intentional distinction.

---

### L6 · `SandboxEvaluator` AST denylist should be an allowlist
**File:** `src/petriq/sandbox.py` lines 29–36

The denylist (`open`, `print`, `eval`, `exec`, `__import__`) only blocks `ast.Name`
calls — not attribute calls (`builtins.open`, `os.system`). Replace the denylist
AST check with an allowlist: reject any `ast.Call` whose `func` is not in a known
set of safe mathematical/comparison function names.

---

### L7 · Subnet deadline is hardcoded
**File:** `src/petriq/engine.py` line 666

Add `subnet_deadline_secs: float = 30.0` to `SubstitutionTransition` and use it:
```python
subnet.run(deadline=time.monotonic() + self.subnet_deadline_secs)
```

---

## Verification Checklist

After completing all tiers, confirm:

```bash
cd /Users/phil/workdir/github/philgresh/petriq
.venv/bin/pytest tests/ -q          # all tests pass (135+ expected)
.venv/bin/ruff check src/ tests/    # clean
```

Additional tests to write alongside the fixes:

- `test_frozen_dict_backdoor_blocked` — confirm `fd._data['x'] = 1` raises `TypeError`
- `test_evolve_generates_new_id` — confirm `t.evolve().id != t.id`
- `test_basexception_does_not_leak_running_count` — simulate `KeyboardInterrupt` mid-action
- `test_partial_deposit_rollback` — confirm no partial state on `color_set` mismatch mid-loop
- `test_bipartite_add_transition_rejects_transition_target` — structural wiring error
- `test_substitution_transition_requires_predeclared_ports` — enforce encapsulation at init
- `test_settle_secs_respects_model_time` — logical clock governs settle windows
