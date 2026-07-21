"""Acceptance cases for :mod:`cpnx.certification`.

Each test asserts that a callable which should be considered safe to run
inline is in fact certified — i.e. ``certify(fn).certified is True`` and the
``is_inline_safe`` shortcut agrees. These are pure certifier unit tests: they
say nothing about *how* a certified callable is executed by the engine.
"""

from cpnx.certification import certify, is_inline_safe

# Module-level immutable global used by the "reads an immutable module global"
# case below. It must live at module scope so the lambda's free/global read
# resolves to it via __globals__.
THRESHOLD = 19


def _assert_certified(fn) -> None:
    verdict = certify(fn)
    assert verdict.certified is True
    assert is_inline_safe(fn) is True


def test_bool_of_param_is_simplest_accepting_shape():
    # bool(toks) is a single whitelisted-builtin call on the parameter itself;
    # no iteration, no attribute access, nothing external to worry about.
    fn = lambda toks: bool(toks)  # noqa: E731
    _assert_certified(fn)


def test_comprehension_filtering_on_attribute_of_param():
    # The comprehension's iterable (`toks`) is rooted at the parameter, and
    # `.color` is a plain (non-dunder, non-private) attribute read on the
    # comprehension's own loop variable, so it needs no external resolution.
    fn = lambda toks: bool([t for t in toks if t.color == "lead"])  # noqa: E731
    _assert_certified(fn)


def test_subscript_and_compare_on_param():
    # Subscripting the parameter and comparing to a literal touches nothing
    # outside the argument; Compare/Subscript nodes are not disqualifying.
    fn = lambda toks: toks[0].payload["val"] == 42  # noqa: E731
    _assert_certified(fn)


def test_arithmetic_and_generator_reduction():
    # `len` and `sum` are whitelisted builtins; the generator expression fed
    # to `sum` iterates `toks`, which is rooted at the parameter.
    fn = lambda toks: len(toks) >= 2 and sum(t.payload["w"] for t in toks) < 100  # noqa: E731
    _assert_certified(fn)


def test_sorted_with_nested_key_lambda():
    # `sorted` is a whitelisted iteration builtin called on the parameter.
    # The nested `key=` lambda is itself a certifying callable: it only reads
    # its own parameter's attribute, so the whole expression certifies.
    fn = lambda toks: bool(sorted(toks, key=lambda t: t.created_at))  # noqa: E731
    _assert_certified(fn)


def test_any_all_next_over_generator_rooted_at_param():
    # `any`/`all`/`next` are whitelisted iteration builtins; each generator
    # expression here iterates `toks` directly, so iteration stays bounded by
    # the engine-built argument.
    any_fn = lambda toks: any(t.payload.get("ready") for t in toks)  # noqa: E731
    all_fn = lambda toks: all(t.startswith("lead") for t in toks)  # noqa: E731
    next_fn = lambda toks: next((t for t in toks if t.color == "lead"), None) is not None  # noqa: E731
    _assert_certified(any_fn)
    _assert_certified(all_fn)
    _assert_certified(next_fn)


def test_whitelisted_method_calls_on_payload():
    # `.get`, `.startswith`, `.items` are all in ALLOWED_METHODS; calling them
    # on an attribute of the parameter is not a call to an unresolved name and
    # needs no transitive certification.
    def fn(toks):
        return (
            toks[0].payload.get("val") is not None
            and toks[0].id.startswith("lead-")
            and bool(toks[0].payload.items())
        )
    _assert_certified(fn)


def test_factory_closure_over_two_immutable_floats():
    # `make` returns a lambda that closes over `low`/`high`. Both are floats,
    # which are in _IMMUTABLE_LEAVES, so the free-variable reads are allowed.
    def make(low, high):
        return lambda toks: low <= toks[0].payload["w"] <= high

    fn = make(17.0, 19.0)
    _assert_certified(fn)


def test_closure_over_frozenset_and_tuple_of_immutables():
    # frozenset is an immutable leaf type directly; a tuple is walked
    # recursively and accepted so long as every element is itself immutable.
    allowed_colors = frozenset({"lead", "gold"})
    allowed_weights = (1.0, 2.0, 3.0)

    fn = lambda toks: toks[0].color in allowed_colors and toks[0].payload["w"] in allowed_weights  # noqa: E731
    _assert_certified(fn)


def test_reads_immutable_module_level_global():
    # THRESHOLD is defined at module scope above and is an int, which is an
    # immutable leaf type, so the global read resolves cleanly.
    fn = lambda toks: toks[0].payload["w"] > THRESHOLD  # noqa: E731
    _assert_certified(fn)


def test_def_statement_guard_not_just_lambda():
    # Certification is not lambda-only: a `def` guard with the same shape of
    # body (whitelisted builtin call on the parameter) certifies too.
    def guard(toks):
        return len(toks) > 0

    _assert_certified(guard)


def test_nested_tuple_unpacking_comprehension_target():
    # The comprehension target `a, b` is a Tuple of Name nodes; both `a` and
    # `b` are collected as in-comprehension targets, and the generator's
    # iterable `pairs` is the parameter itself, so iteration is bounded.
    fn = lambda pairs: bool([a for a, b in pairs])  # noqa: E731
    _assert_certified(fn)


def test_dict_and_set_comprehensions_rooted_at_param():
    # DictComp and SetComp are checked by the same comprehension rule as
    # ListComp/GeneratorExp: the iterable must be rooted at a parameter.
    dict_fn = lambda toks: bool({t.id: t.payload["w"] for t in toks})  # noqa: E731
    set_fn = lambda toks: bool({t.color for t in toks})  # noqa: E731
    _assert_certified(dict_fn)
    _assert_certified(set_fn)


def test_transitive_call_to_certifying_helper_function():
    # `_is_lead` is itself a plain, certifying function; calling it by name
    # from another lambda requires (and gets) transitive certification.
    def _is_lead(tok):
        return tok.color == "lead"

    fn = lambda toks: any(_is_lead(t) for t in toks)  # noqa: E731
    _assert_certified(fn)


def test_none_and_bool_literal_and_no_arg_builtin_calls():
    # `None`/`True`/`False` are literal constants (no name resolution at all),
    # and `bool()` with no arguments is still just a whitelisted builtin call.
    fn = lambda toks: toks[0].payload.get("val") is not None and bool()  # noqa: E731
    _assert_certified(fn)
