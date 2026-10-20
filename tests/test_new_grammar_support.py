"""Grammar pins for Python 3.14 (:pep:`750`) and 3.15 (:pep:`798`, :pep:`810`).

:mod:`cpnx.certification`, :mod:`cpnx.sandbox` and :mod:`cpnx.linting` all decide by
matching on AST node **classes**. That makes two kinds of release change interesting:
a brand-new node class (3.14's ``TemplateStr``/``Interpolation``) and a new field on
an existing one (3.15's ``Import.is_lazy``/``ImportFrom.is_lazy``). Both are invisible
to a class-based match, which is why they are safe — but "invisible" is also how a
construct gets silently *admitted* as inline-safe. Certification is a safety boundary
where a false "certified" is worse than a crash, so the admissions below are pinned
rather than left to the reasoning that established them.

The syntax under test is a :exc:`SyntaxError` on older interpreters, so every guard is
built as a runtime string and never appears as a literal in this file — the pattern
``test_pure_evaluation.py`` already uses for source it must parse rather than run.
"""

import ast
import builtins
import importlib.util
import sys

import pytest

from cpnx.certification import _Certifier, _check_structural, _is_immutable, certify
from cpnx.linting import lint_callable
from cpnx.sandbox import verify_callable_purity
from cpnx.tokens import FrozenDict

_PY314 = sys.version_info >= (3, 14)
_PY315 = sys.version_info >= (3, 15)


def _load_guard(tmp_path, module_name: str, source: str, attr: str):
    """Write *source* to an importable module and hand back its *attr*.

    All three modules recover a callable's AST through :func:`inspect.getsourcefile`,
    so a guard under test has to live in a real file; an ``exec``-created function
    reports no source and would certify as "source unavailable" regardless of its body.
    """
    path = tmp_path / f"{module_name}.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        return getattr(module, attr)
    finally:
        del sys.modules[module_name]


def _comprehension_verdict(source: str, params: frozenset[str]):
    """Certify just the comprehension in *source* (an expression) against *params*."""
    node = ast.parse(source, mode="eval").body
    return _Certifier._check_comprehension(node, params)


# --------------------------------------------------------------------------
# PEP 750 — template strings (Python 3.14): two NEW node classes
# --------------------------------------------------------------------------


@pytest.mark.skipif(not _PY314, reason="PEP 750 t-strings are 3.14+ syntax")
def test_tstring_introduces_new_node_classes():
    # The premise of the two tests below: t-strings do not reuse JoinedStr.
    node = ast.parse("t'{x}'", mode="eval").body
    assert type(node).__name__ == "TemplateStr"
    assert [type(value).__name__ for value in node.values] == ["Interpolation"]


@pytest.mark.skipif(not _PY314, reason="PEP 750 t-strings are 3.14+ syntax")
def test_tstring_guard_is_certified(tmp_path):
    # TemplateStr/Interpolation are unknown to _check_structural, so they fall
    # through to Verdict(True). That is the right answer: a t-string defers
    # formatting instead of calling __format__, making it strictly tamer than the
    # f-string cpnx already admits.
    guard = _load_guard(
        tmp_path,
        "grammar_tstring_ok",
        "def guard(tokens):\n    return len(t'{tokens}'.strings) > 0\n",
        "guard",
    )
    assert certify(guard).certified is True


@pytest.mark.skipif(not _PY314, reason="PEP 750 t-strings are 3.14+ syntax")
def test_tstring_interpolation_is_still_walked(tmp_path):
    # The load-bearing half: admitting the *container* must not stop the walk from
    # reaching the expressions inside it. If a new node class ever became opaque to
    # ast.walk, a guard could smuggle arbitrary calls through an interpolation.
    guard = _load_guard(
        tmp_path,
        "grammar_tstring_bad",
        "def guard(tokens):\n    return len(t'{open(\"x\")}'.strings) > 0\n",
        "guard",
    )
    assert certify(guard).certified is False
    with pytest.raises(PermissionError):
        verify_callable_purity(guard)


# --------------------------------------------------------------------------
# PEP 798 — unpacking in comprehensions (Python 3.15): Starred lands in `elt`
# --------------------------------------------------------------------------


@pytest.mark.skipif(not _PY315, reason="PEP 798 comprehension unpacking is 3.15+ syntax")
def test_unpacking_comprehension_puts_starred_in_elt():
    node = ast.parse("[*a for a in rows]", mode="eval").body
    assert isinstance(node.elt, ast.Starred)
    assert ast.unparse(node) == "[*a for a in rows]"


@pytest.mark.skipif(not _PY315, reason="PEP 798 comprehension unpacking is 3.15+ syntax")
def test_unpacking_comprehension_stays_bounded_by_the_argument():
    # _check_comprehension reads only `generators`, so the Starred in `elt` cannot
    # affect the bound on iteration — and the bound is what the termination proof
    # rests on. Every comprehension form PEP 798 touches is checked.
    for source in (
        "[*t.value for t in tokens]",
        "{*t.value for t in tokens}",
        "{**t.value for t in tokens}",
        "(*t.value for t in tokens)",
    ):
        assert _comprehension_verdict(source, frozenset({"tokens"})).certified is True


@pytest.mark.skipif(not _PY315, reason="PEP 798 comprehension unpacking is 3.15+ syntax")
def test_unpacking_comprehension_over_a_global_in_the_iter_position_is_rejected():
    # The ordinary bound, unaffected by the unpacking: the `for` clause still has to iterate
    # something the engine built.
    verdict = _comprehension_verdict("[*t.value for t in _ELSEWHERE]", frozenset({"tokens"}))
    assert verdict.certified is False
    assert "not bounded" in verdict.reason


@pytest.mark.skipif(not _PY315, reason="PEP 798 comprehension unpacking is 3.15+ syntax")
@pytest.mark.parametrize(
    "source",
    [
        "[*_ELSEWHERE for t in tokens]",
        "{*_ELSEWHERE for t in tokens}",
        "{**_ELSEWHERE for t in tokens}",
        "(*_ELSEWHERE for t in tokens)",
        "[*local_list for t in tokens]",
    ],
)
def test_unpacked_expression_is_bounds_checked_like_an_iter_clause(source):
    # The position that actually needed closing. `[*xs for t in ts]` iterates `xs` just as
    # `for x in xs` would, but the AST puts it in `elt`/`key` rather than `generators`, so
    # before this check it was admitted unchecked: unpacking became a way to iterate a module
    # global or a function local that the equivalent `for` clause is rejected for.
    verdict = _comprehension_verdict(source, frozenset({"tokens"}))
    assert verdict.certified is False
    assert "not bounded" in verdict.reason


@pytest.mark.skipif(not _PY315, reason="PEP 798 comprehension unpacking is 3.15+ syntax")
def test_chained_local_unpacking_cannot_amplify_iteration(tmp_path):
    # Why the above matters beyond tidiness. Each line multiplies the result by len(toks), so
    # k lines iterate O(len(toks) ** (k + 1)) — inline, under the engine lock, with no timeout.
    # The pre-3.15 spelling of the same amplification (`for x in s1`) was already rejected.
    guard = _load_guard(
        tmp_path,
        "grammar_chained_unpack",
        "def guard(toks):\n    s1 = [*toks for _ in toks]\n    s2 = [*s1 for _ in toks]\n    return len(s2) > 0\n",
        "guard",
    )
    assert certify(guard).certified is False


@pytest.mark.skipif(not _PY315, reason="PEP 798 comprehension unpacking is 3.15+ syntax")
def test_unpacking_comprehension_element_expression_is_still_walked(tmp_path):
    # The counterpart to the t-string walk test: `elt` is a position the certifier
    # does not inspect directly, so this pins that ast.walk still reaches into it.
    guard = _load_guard(
        tmp_path,
        "grammar_starred_elt",
        "def guard(tokens):\n    return len([*open(t.value) for t in tokens]) > 0\n",
        "guard",
    )
    assert certify(guard).certified is False
    with pytest.raises(PermissionError):
        verify_callable_purity(guard)


@pytest.mark.skipif(not _PY315, reason="PEP 798 comprehension unpacking is 3.15+ syntax")
def test_dict_unpacking_comprehension_has_a_none_value_field():
    # `{**d for d in ds}` parses with the unpacked expression in `key` and `value`
    # set to None — the inverse of ast.Dict, where `{**d}` is keys=[None]. Nothing in
    # cpnx reads DictComp.value (and ast.walk skips None children), but the asymmetry
    # is surprising enough to pin for anyone who adds a DictComp branch later.
    node = ast.parse("{**d for d in ds}", mode="eval").body
    assert isinstance(node, ast.DictComp)
    assert node.value is None
    assert isinstance(node.key, ast.Name)


# --------------------------------------------------------------------------
# PEP 810 — lazy imports (Python 3.15): a new field on Import/ImportFrom
# --------------------------------------------------------------------------


@pytest.mark.skipif(not _PY315, reason="PEP 810 lazy imports are 3.15+ syntax")
def test_lazy_import_is_rejected_despite_the_new_is_lazy_field():
    # _check_structural matches on the node class, so the added field changes nothing
    # and a lazy import is rejected like any other. Rejecting it is also correct: a
    # deferred import in enabling logic is still an import.
    for source, expected_class in (
        ("lazy import json", ast.Import),
        ("lazy from pathlib import Path", ast.ImportFrom),
    ):
        node = ast.parse(source).body[0]
        assert isinstance(node, expected_class)
        assert node.is_lazy
        verdict = _check_structural(node)
        assert verdict is not None
        assert verdict.certified is False
        assert verdict.reason == "import"


def _lazy_guard_module(tmp_path, module_name, *, lazy):
    """Write a guard module whose imports are deferred (``lazy=True``) or eager, and load it.

    The eager build is the control: a deferred import must give a callable the *same* verdict
    and the same lint findings as an ordinary one, because it names the same objects.
    """
    (tmp_path / "lazy_helpers.py").write_text(
        "MAX_BATCH = 10\n\n\ndef helper(n):\n    return n > 0\n", encoding="utf-8"
    )
    source = (
        "lazy import sqlite3\n"
        "lazy from lazy_helpers import helper, MAX_BATCH\n"
        "\n\n"
        "def guard_hazard(tokens):\n"
        "    return sqlite3.connect(':memory:') is not None\n"
        "\n\n"
        "def guard_helper(tokens):\n"
        "    return helper(len(tokens))\n"
        "\n\n"
        "def guard_const(tokens):\n"
        "    return len(tokens) < MAX_BATCH\n"
    )
    if not lazy:
        source = source.replace("lazy ", "")
    path = tmp_path / f"{module_name}.py"
    path.write_text(source, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        del sys.modules[module_name]
        sys.path.remove(str(tmp_path))


@pytest.mark.skipif(not _PY315, reason="PEP 810 lazy imports are 3.15+ syntax")
def test_lazy_imported_hazard_is_still_linted(tmp_path):
    # A deferred import leaves a proxy in __globals__ that resolves to no package, so the
    # linter's "resolve the symbol to its defining package" strategy silently missed every
    # hazard reached this way — `lazy import sqlite3` hid a `sqlite3.connect` in a guard.
    lazy_mod = _lazy_guard_module(tmp_path, "lazy_hazard_mod", lazy=True)
    eager_mod = _lazy_guard_module(tmp_path, "eager_hazard_mod", lazy=False)
    lazy_findings = [(f.category, f.symbol) for f in lint_callable(lazy_mod.guard_hazard)]
    eager_findings = [(f.category, f.symbol) for f in lint_callable(eager_mod.guard_hazard)]
    assert lazy_findings == eager_findings
    assert ("database", "sqlite3.connect") in lazy_findings


@pytest.mark.skipif(not _PY315, reason="PEP 810 lazy imports are 3.15+ syntax")
def test_lazy_imported_helper_and_constant_still_certify(tmp_path):
    # The proxy is not a function and not an immutable leaf, so before resolution the certifier
    # rejected both ("call to non-function", "reads mutable/uncertified external") and dropped
    # the callable off the inline path for using a language feature that changes nothing.
    lazy_mod = _lazy_guard_module(tmp_path, "lazy_ok_mod", lazy=True)
    eager_mod = _lazy_guard_module(tmp_path, "eager_ok_mod", lazy=False)
    for name in ("guard_helper", "guard_const"):
        lazy_verdict = certify(getattr(lazy_mod, name))
        eager_verdict = certify(getattr(eager_mod, name))
        assert lazy_verdict.certified is eager_verdict.certified, name
        assert lazy_verdict.certified is True, f"{name}: {lazy_verdict.reason}"


@pytest.mark.skipif(not _PY315, reason="PEP 810 lazy imports are 3.15+ syntax")
def test_certification_of_a_lazy_import_does_not_depend_on_call_order(tmp_path):
    # The worst of the three symptoms: invoking the guard resolved the proxy via LOAD_GLOBAL,
    # so an identical callable certified differently depending on whether anything had called
    # it yet. A safety verdict must not be a function of unrelated history.
    module = _lazy_guard_module(tmp_path, "lazy_order_mod", lazy=True)
    before = certify(module.guard_const)
    module.guard_const([1, 2])
    after = certify(module.guard_const)
    assert before.certified == after.certified
    assert before.certified is True


@pytest.mark.skipif(not _PY315, reason="PEP 810 lazy imports are 3.15+ syntax")
def test_module_level_lazy_import_does_not_disturb_a_guard_in_the_same_file(tmp_path):
    # PEP 810 permits `lazy` only at module scope, so a lazy import can never appear
    # inside a guard body. It can appear in the guard's *file*, which certification
    # parses whole — _find_target_node must still isolate the function.
    guard = _load_guard(
        tmp_path,
        "grammar_lazy_module",
        "lazy import json\n\n\ndef guard(tokens):\n    return len(tokens) > 0\n",
        "guard",
    )
    assert certify(guard).certified is True


# --------------------------------------------------------------------------
# PEP 814 — frozendict (Python 3.15), alongside cpnx's own FrozenDict
# --------------------------------------------------------------------------


class _HashableButMutable:
    """Hashable, so a frozen mapping accepts it; mutable, so closing over it is not safe."""

    def __init__(self) -> None:
        self.items: list[int] = []

    def __hash__(self) -> int:
        return 1


def _frozen_mapping_types():
    types_ = [FrozenDict]
    if _PY315:
        types_.append(builtins.frozendict)
    return types_


@pytest.mark.parametrize("frozen", _frozen_mapping_types())
def test_a_frozen_lookup_table_of_scalars_is_closable(frozen):
    # The arc/transition docs tell consumers to certify a callable by closing over immutable
    # data read at construction time; a frozen mapping is the obvious way to write a lookup
    # table, and rejecting it pushed them off the inline path (and arcs off the key index)
    # for following that advice.
    assert _is_immutable(frozen({"gold": 1, "silver": 2})) is True
    assert _is_immutable(frozen({})) is True


@pytest.mark.parametrize("frozen", _frozen_mapping_types())
def test_a_frozen_mapping_holding_mutable_state_is_still_rejected(frozen):
    # Being frozen is not being deeply immutable, so neither type is trusted blanketly:
    # `frozendict` is shallow by design, and `FrozenDict` admits any *hashable* value even
    # though it freezes nested dicts and lists. Both must fail on a live object.
    assert _is_immutable(frozen({"k": _HashableButMutable()})) is False
    assert _is_immutable(frozen({_HashableButMutable(): 1})) is False


@pytest.mark.skipif(not _PY315, reason="PEP 814 frozendict is a 3.15+ builtin")
def test_builtin_frozendict_shallowness_is_detected():
    # The concrete difference from FrozenDict: `frozendict` does not freeze what it holds, so
    # this one still owns a live dict that a guard could observe changing between firings.
    shallow = builtins.frozendict({"a": {"b": 1}})
    assert isinstance(shallow["a"], dict)
    assert _is_immutable(shallow) is False
    # FrozenDict freezes the same nesting on the way in, so it stays closable.
    assert _is_immutable(FrozenDict({"a": {"b": 1}})) is True


@pytest.mark.parametrize("frozen", _frozen_mapping_types())
def test_guard_closing_over_a_frozen_lookup_table_certifies(frozen):
    table = frozen({"gold": 1})

    def guard(tokens):
        return table.get("gold", 99) > 0

    assert certify(guard).certified is True
