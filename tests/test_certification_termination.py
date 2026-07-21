"""Termination / hang-safety cases for :mod:`cpnx.certification`.

A certified callable runs **inline, under the engine lock, with no timeout**
(see the module docstring of ``cpnx.certification``). That is the whole point
of certification: skip the ~90x-costlier executor round-trip for callables we
can *prove* terminate. The flip side is that the proof is safety-critical — if
the verifier wrongly certified something that could fail to terminate, that
callable would run inline and, on the bad input, **hang the engine lock
forever** (every other caller waiting on the lock blocks with it). Every test
below is a shape that must NOT be certified, precisely because inlining it
would risk exactly that hang. The module also checks the contrapositive: a
callable built only from the admitted, provably-terminating vocabulary IS
certified, so the inline path stays available for the callables it's meant for.

Module-level definitions only: certification recovers source via
``inspect.getsource``, which does not work for callables defined inside a test
function (nested `def`s parse fine syntactically but the point of these tests
is the module-level call-graph analysis, not closures).
"""

from cpnx.certification import is_inline_safe

_MODULE_TUPLE = (1, 2, 3)


def hangs_with_while_true(toks):
    while True:
        pass


def test_unbounded_while_loop_is_not_inline_safe():
    """A `while True` body never returns; inlined under the lock, it would
    pin the lock forever with no timeout to reclaim it."""
    assert is_inline_safe(hangs_with_while_true) is False


def self_recursive(toks):
    return self_recursive(toks)


def test_unbounded_self_recursion_is_not_inline_safe():
    """Structurally, nothing bounds this recursion (no base case the
    certifier can see) — the certifier rejects any cyclic call graph rather
    than try to prove termination of recursion. If it were wrong and this ran
    inline, unbounded recursion would either hang or stack-overflow inside the
    lock, taking the engine down with it."""
    assert is_inline_safe(self_recursive) is False


def mutual_a(toks):
    return mutual_b(toks)


def mutual_b(toks):
    return mutual_a(toks)


def test_mutual_recursion_is_not_inline_safe():
    """Same cyclic-call-graph hazard as direct self-recursion, just spread
    across two functions. Inlining either one would risk hanging the lock for
    the same reason."""
    assert is_inline_safe(mutual_a) is False
    assert is_inline_safe(mutual_b) is False


def _sleeps(toks):
    import time

    time.sleep(999)
    return True


def guard_calls_sleeper(toks):
    return _sleeps(toks)


def test_transitively_reached_blocking_call_is_not_inline_safe():
    """`_sleeps` is uncertified (its `import` is structurally rejected), so
    the call-graph taint propagates to `guard_calls_sleeper` even though the
    guard's own body looks innocuous. An inlined blocking call (I/O, sleep,
    a network round-trip) would pin the engine lock for its full duration
    with no timeout to interrupt it."""
    assert is_inline_safe(_sleeps) is False
    assert is_inline_safe(guard_calls_sleeper) is False


def guard_iterates_module_constant(toks):
    return any(x > 0 for x in _MODULE_TUPLE)


def test_iteration_not_bounded_by_argument_is_not_inline_safe():
    """The certifier only admits comprehensions rooted at the callable's own
    argument, because that is the one iterable it can prove is finite (the
    engine builds it). Iterating anything else — even a finite module
    constant — falls outside the provable vocabulary, so it is rejected
    rather than risk inlining an iteration whose bound the verifier can't
    establish."""
    assert is_inline_safe(guard_iterates_module_constant) is False


def bounded_guard(toks):
    return any(t.color == "x" for t in toks)


def test_bounded_whitelisted_guard_is_inline_safe():
    """Contrapositive: a guard built entirely from the admitted vocabulary —
    a whitelisted builtin (`any`) over a comprehension rooted at the
    argument, plus plain attribute/equality access — has a provably
    terminating body (the argument is finite and engine-built), so it earns
    the inline, no-timeout path."""
    assert is_inline_safe(bounded_guard) is True


def sorted_over_argument(toks):
    return sorted(toks)


def test_sorted_over_argument_is_inline_safe():
    """Another representative admitted shape: `sorted` (a whitelisted,
    O(n)-over-a-finite-iterable builtin) applied directly to the argument.
    No unbounded loop, no recursion, no opaque call — safe to run inline."""
    assert is_inline_safe(sorted_over_argument) is True
