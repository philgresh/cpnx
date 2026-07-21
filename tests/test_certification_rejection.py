"""Rejection cases for :mod:`cpnx.certification`.

One test per disqualifying property. Certification gates *inline* execution
(under the engine lock, with no timeout), so every rejection here is a callable
that must NOT earn that path — either because it could fail to terminate, or
because it reaches outside the closed world. We assert ``certified is False``;
reason strings are diagnostic, not contractual, so they are only spot-checked
with substring matches where it adds clarity.
"""

import functools

from cpnx.certification import certify

_MUTABLE_STATE = {"allow": True}
_MUTABLE_LIST = [True]


def _assert_rejected(fn, reason_contains: str | None = None) -> None:
    verdict = certify(fn)
    assert verdict.certified is False
    assert verdict.reason  # a rejection must explain itself
    if reason_contains is not None:
        assert reason_contains in verdict.reason


def test_reads_mutable_dict_closure():
    # The canonical impure guard: its truth value is external mutable state.
    def make():
        state = {"allow": True}
        return lambda toks: state["allow"]

    _assert_rejected(make(), "mutable")


def test_reads_mutable_dict_module_global():
    # Same hazard, reached via __globals__ rather than a closure cell.
    _assert_rejected(lambda toks: _MUTABLE_STATE["allow"], "_MUTABLE_STATE")


def test_reads_mutable_list_module_global():
    _assert_rejected(lambda toks: _MUTABLE_LIST[0], "_MUTABLE_LIST")


def test_nested_scope_binding_does_not_mask_outer_mutable_read():
    # A name bound only inside a nested `def` body must NOT make an *outer* read
    # of that same name (here, a mutable module global) look local. Regression
    # for a `_bound_names` scope leak that flattened nested-scope bindings into
    # the outer scope's bound set, wrongly certifying the outer mutable read.
    def guard(toks):
        def helper():
            _MUTABLE_STATE = 1  # noqa: F841 — nested binding must not leak to the outer scope
            return _MUTABLE_STATE

        return _MUTABLE_STATE["allow"]  # outer read of the GLOBAL mutable dict

    _assert_rejected(guard, "_MUTABLE_STATE")


def test_comprehension_target_does_not_mask_outer_mutable_read():
    # Python 3 gives a comprehension its own scope, so its `for`-target does not
    # bind in the enclosing scope. Collecting it into `bound` would mask an outer
    # read of a same-named mutable global — the same false accept as the nested-def
    # case above, one node type over.
    def guard(toks):
        _ = [_MUTABLE_STATE for _MUTABLE_STATE in toks]  # comp-scoped target; must not leak
        return _MUTABLE_STATE["allow"]  # outer read of the GLOBAL mutable dict

    _assert_rejected(guard, "_MUTABLE_STATE")


def test_comprehension_walrus_binding_is_treated_as_local():
    # Counterpart to the previous test: a walrus (`:=`) inside a comprehension *does*
    # leak to the enclosing function scope (PEP 572), so a later read of it is a
    # genuine local — even when it shadows a mutable global of the same name. It must
    # therefore certify (True); a blanket "skip everything inside a comprehension"
    # fix would wrongly reject it. This pins the walrus-preserving behaviour.
    def guard(toks):
        _ = [x for x in toks if (_MUTABLE_STATE := x)]  # walrus binds a LOCAL _MUTABLE_STATE
        return bool(_MUTABLE_STATE)  # reads the walrus-bound local, not the global

    assert certify(guard).certified is True


def test_for_statement_is_unbounded_iteration():
    def guard(toks):
        total = 0
        for _ in toks:  # only comprehensions (bounded by the arg) are allowed
            total += 1
        return total > 0

    _assert_rejected(guard, "for/while")


def test_while_statement_is_unbounded_iteration():
    def guard(toks):
        while toks:
            toks = toks[1:]
        return True

    _assert_rejected(guard, "for/while")


def test_import_statement():
    def guard(toks):
        import os

        return bool(os)

    _assert_rejected(guard, "import")


def test_global_declaration():
    def guard(toks):
        global _MUTABLE_STATE
        return True

    _assert_rejected(guard, "global")


def test_private_attribute_access():
    _assert_rejected(lambda toks: toks[0]._secret, "private")


def test_dunder_attribute_access():
    _assert_rejected(lambda toks: toks[0].__class__, "private")


def test_unwhitelisted_method_call():
    # `.extend` mutates and is not in ALLOWED_METHODS.
    _assert_rejected(lambda toks: toks[0].payload.popitem(), "popitem")


def test_call_to_unresolved_name():
    _assert_rejected(lambda toks: mystery(toks), "unresolved")  # noqa: F821


def test_call_to_non_function_global():
    # `str` is whitelisted, but a call to a *class* we resolve is not a
    # certifying user function.
    class Widget:
        pass

    def guard(toks):
        return bool(Widget(toks))

    _assert_rejected(guard)


def test_non_name_complex_call():
    # Calling the result of a subscript needs runtime resolution — reject.
    _assert_rejected(lambda toks: toks[0].handlers[0](toks))


def test_raises_via_unwhitelisted_constructor():
    # `raise ValueError(...)` is a call to a name outside the whitelist.
    def guard(toks):
        raise ValueError("nope")

    _assert_rejected(guard)


def test_source_unavailable_is_not_certified():
    # A lambda built by exec has no recoverable source; certification must
    # treat "can't verify" as "not certified", never as "allowed".
    namespace: dict = {}
    exec("f = lambda toks: True", namespace)  # noqa: S102
    _assert_rejected(namespace["f"], "source unavailable")


def test_decorated_callable_is_rejected():
    # The live object is the wrapper; the recoverable source is the inner body.
    # Certifying the body while running the wrapper would be unsound.
    def passthrough(fn):
        @functools.wraps(fn)
        def wrapper(toks):
            return fn(toks)

        return wrapper

    @passthrough
    def guard(toks):
        return len(toks) > 0

    _assert_rejected(guard, "decorated")


def test_non_callable_input():
    _assert_rejected(42)
