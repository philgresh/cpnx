"""Transitive certification and closure/scope edge cases for :mod:`cpnx.certification`.

The transitive rule is what lets guards be factored into readable helpers
instead of one dense lambda: a call to a user function is allowed iff that
function itself certifies, recursively. This module pins that behaviour, the
cycle rejection that keeps the termination proof intact, and the memoisation
that keeps a diamond call graph from blowing up or leaking across functions.
"""

from cpnx.certification import certify


# --- module-level helpers, resolved via __globals__ ---
def _pure_leaf(toks):
    return len(toks) > 0


def _calls_pure_leaf(toks):
    return _pure_leaf(toks)


def _loops(toks):
    total = 0
    for _ in toks:
        total += 1
    return total


def _calls_loops(toks):
    return _loops(toks)


def _ping(toks):
    return _pong(toks)


def _pong(toks):
    return _ping(toks)


def _self_recursive(toks):
    return _self_recursive(toks[1:]) if toks else 0


# a diamond: top -> {left, right} -> shared_leaf
def _shared_leaf(toks):
    return len(toks)


def _left(toks):
    return _shared_leaf(toks) > 0


def _right(toks):
    return _shared_leaf(toks) < 100


def _diamond_top(toks):
    return _left(toks) and _right(toks)


class TestTransitiveCertification:
    def test_guard_calling_certifying_helper_certifies(self):
        assert certify(_calls_pure_leaf).certified is True

    def test_guard_calling_looping_helper_is_tainted(self):
        # The helper's unbounded loop must disqualify its caller.
        verdict = certify(_calls_loops)
        assert verdict.certified is False
        assert "_loops" in verdict.reason

    def test_deep_chain_certifies_through_multiple_hops(self):
        def hop_a(toks):
            return _calls_pure_leaf(toks)

        def hop_b(toks):
            return hop_a(toks)

        assert certify(hop_b).certified is True

    def test_diamond_call_graph_certifies(self):
        # _shared_leaf is reached by two paths; memoisation must not confuse it.
        assert certify(_diamond_top).certified is True


class TestCycleRejection:
    def test_direct_self_recursion_is_rejected(self):
        # A self-recursive helper could fail to terminate; reject it so it never
        # runs inline under the lock without a timeout.
        verdict = certify(_self_recursive)
        assert verdict.certified is False
        assert "cyclic" in verdict.reason

    def test_mutual_recursion_is_rejected(self):
        verdict = certify(_ping)
        assert verdict.certified is False
        assert "cyclic" in verdict.reason


class TestMemoisationDoesNotLeak:
    def test_same_named_distinct_helpers_are_certified_independently(self):
        # Two functions both named "helper" but with different bodies must be
        # judged on their own AST, not conflated by name. The memo is keyed by
        # function object, so this must hold.
        def make_good():
            def helper(toks):
                return len(toks) > 0

            return lambda toks: helper(toks)

        def make_bad():
            def helper(toks):
                for _ in toks:  # unbounded
                    pass
                return True

            return lambda toks: helper(toks)

        assert certify(make_good()).certified is True
        assert certify(make_bad()).certified is False


class TestClosureCells:
    def test_closure_over_immutable_scalars_certifies(self):
        def make(low, high):
            return lambda toks: low <= toks[0].payload["w"] <= high

        assert certify(make(1, 9)).certified is True

    def test_closure_over_nested_immutable_tuple_certifies(self):
        bounds = ((1, 2), (3, 4))
        assert certify(lambda toks: toks[0].payload["w"] in bounds).certified is True

    def test_closure_over_mutable_container_is_rejected(self):
        cache = {}

        def guard(toks):
            return cache.get("x") == len(toks)

        assert certify(guard).certified is False

    def test_closure_over_certifying_helper_is_allowed(self):
        # A free variable that is itself a certifying function is fine — it is
        # not mutable external *state*, and the call is transitively checked.
        def make():
            def leaf(toks):
                return bool(toks)

            return lambda toks: leaf(toks)

        assert certify(make()).certified is True
