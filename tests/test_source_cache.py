"""The purity check, linter and certifier share one parse per source file.

Each used to parse the callable's whole defining file and walk its whole tree for every guard,
key and action, on every transition built: about 0.1 s per callable in a 2,400-line test
module, which made the shared-place property tests take minutes.
"""

import ast
import importlib.util
import linecache
import os
import sys

import pytest

from cpnx import InputArc, OutputArc, Transition
from cpnx.certification import Verdict, certify
from cpnx.linting import lint_callable
from cpnx.sandbox import _callable_nodes, verify_callable_purity

ANNOTATED = "def guard(tokens: list) -> bool:\n    seen: int = len(tokens)\n    return seen > 0\n"


def _passthrough(tokens):
    return list(tokens)


def _load(path, name):
    linecache.cache.pop(str(path), None)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules.pop(name, None)
    return module


def _rewrite_same_size(path, old, new):
    # Same size and same mtime: only the bytes tell the cache the file changed.
    before = os.stat(path)
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = os.stat(path)
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


def test_one_file_is_parsed_once_for_many_callables(tmp_path, monkeypatch):
    path = tmp_path / "many.py"
    path.write_text("".join(f"def g{i}(tokens):\n    return {i}\n" for i in range(5)))
    module = _load(path, "many_callables")
    parses = []
    real_parse = ast.parse

    def counting_parse(source, filename="<unknown>", *args, **kwargs):
        if filename == str(path):
            parses.append(filename)
        return real_parse(source, filename, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", counting_parse)
    for i in range(5):
        guard = getattr(module, f"g{i}")
        verify_callable_purity(guard)
        assert lint_callable(guard) == []
        assert certify(guard).certified
    assert len(parses) == 1


def test_certifying_skips_annotations_without_changing_the_shared_tree(tmp_path):
    path = tmp_path / "annotated.py"
    path.write_text(ANNOTATED)
    guard = _load(path, "annotated_guard").guard
    (node,) = _callable_nodes(guard)
    before = ast.dump(node, include_attributes=True)
    assert certify(guard).certified
    assert certify(guard).certified
    assert _callable_nodes(guard) == (node,)
    assert ast.dump(node, include_attributes=True) == before


def test_a_local_annotation_naming_a_module_class_is_skipped(tmp_path):
    # `_Box` resolves to a class, which would not certify: only skipping the AnnAssign's
    # annotation lets this guard through.
    path = tmp_path / "local_annotation.py"
    path.write_text(
        "class _Box:\n    pass\n\n\ndef guard(tokens):\n    seen: _Box = len(tokens)\n    return seen > 0\n"
    )
    assert certify(_load(path, "local_annotation_guard").guard).certified


WALRUS_IN_ANNOTATION = {
    "local": "def guard(tokens):\n    x: (STATE := int) = 1\n    return len(STATE) > 0\n",
    "parameter": "def guard(tokens: (STATE := list)):\n    return len(STATE) > 0\n",
    "return": "def guard(tokens) -> (STATE := bool):\n    return len(STATE) > 0\n",
}


@pytest.mark.skipif(sys.version_info >= (3, 14), reason="a walrus in an annotation is a SyntaxError from 3.14")
@pytest.mark.parametrize("where", sorted(WALRUS_IN_ANNOTATION))
def test_a_walrus_in_an_annotation_does_not_hide_a_mutable_global(tmp_path, where):
    # The annotation never binds STATE when the guard runs, so `len(STATE)` reads the
    # module's live list and the guard must not certify.
    path = tmp_path / f"walrus_{where}.py"
    path.write_text("STATE = [1]\n\n\n" + WALRUS_IN_ANNOTATION[where])
    module = _load(path, f"walrus_{where}_guard")
    module.STATE = [1]
    verdict = certify(module.guard)
    assert not verdict.certified
    assert "STATE" in verdict.reason


NESTED_ANNOTATIONS = (
    "def evil():\n    print('called')\n    return int\n\n\n"
    "def guard(tokens):\n    def helper(x: evil()) -> evil():\n        return x\n\n    return True\n"
)


def test_a_nested_defs_annotations_are_checked_when_they_run_at_call_time(tmp_path):
    # Before 3.14, a nested def's annotations run every time the guard runs, so `evil()`
    # is called from the guard and it must not certify. From 3.14 they are lazy.
    path = tmp_path / "nested_annotations.py"
    path.write_text(NESTED_ANNOTATIONS)
    verdict = certify(_load(path, "nested_annotations_guard").guard)
    assert verdict.certified is (sys.version_info >= (3, 14))
    if not verdict.certified:
        assert "evil" in verdict.reason


def test_a_nested_defs_annotations_are_skipped_under_the_future_import(tmp_path):
    path = tmp_path / "nested_future.py"
    path.write_text("from __future__ import annotations\n\n" + NESTED_ANNOTATIONS)
    assert certify(_load(path, "nested_future_guard").guard).certified


CLASS_IN_GUARD = {
    # Before 3.14 the class body runs its annotation, calling `evil()`, on every guard call.
    "annotation": "def guard(tokens):\n    class C:\n        x: evil() = 1\n\n    return True\n",
    # The class body's STATE would count as a local binding and hide the read of the global.
    "binding": "def guard(tokens):\n    class C:\n        STATE = 0\n\n    return len(STATE) > 0\n",
}


@pytest.mark.parametrize("case", sorted(CLASS_IN_GUARD))
def test_a_class_inside_a_guard_does_not_certify(tmp_path, case):
    path = tmp_path / f"class_{case}.py"
    path.write_text("STATE = [1]\n\n\ndef evil():\n    return int\n\n\n" + CLASS_IN_GUARD[case])
    verdict = certify(_load(path, f"class_{case}_guard").guard)
    assert not verdict.certified
    assert verdict.reason == "class definition"


def test_a_walrus_in_a_default_value_does_not_hide_a_mutable_global(tmp_path):
    # The default ran once, at definition: inside the guard, STATE is still the module's list.
    path = tmp_path / "walrus_default.py"
    path.write_text("STATE = [1]\n\n\ndef guard(tokens, _=(STATE := 0)):\n    return len(STATE) > 0\n")
    module = _load(path, "walrus_default_guard")
    module.STATE = [1]
    verdict = certify(module.guard)
    assert not verdict.certified
    assert "STATE" in verdict.reason


MODULE_PRELUDE = "STATE = [1]\nCALLS = []\n\n\ndef evil(*a):\n    CALLS.append(1)\n    return int\n\n\n"

# Everything that runs when a guard is called must be walked. Each case calls `evil()` (which
# appends to a global) on every call, so the certifier must reject the call to it.
RUNS_AT_CALL_TIME = {
    "nested_decorator": "def guard(tokens):\n    @evil\n    def h(x):\n        return x\n\n    return True\n",
    "nested_default": "def guard(tokens):\n    def h(x=evil()):\n        return x\n\n    return True\n",
    "lambda_default": "def guard(tokens):\n    h = lambda x=evil(): x  # noqa: E731\n    return True\n",
    "annotated_target": "def guard(tokens):\n    evil().x: int\n    return True\n",
    "fstring": "def guard(tokens):\n    return f'{evil()}' != ''\n",
}


@pytest.mark.parametrize("case", sorted(RUNS_AT_CALL_TIME))
def test_code_that_runs_at_call_time_is_checked(tmp_path, case):
    path = tmp_path / f"runs_{case}.py"
    path.write_text(MODULE_PRELUDE + RUNS_AT_CALL_TIME[case])
    verdict = certify(_load(path, f"runs_{case}_guard").guard)
    assert not verdict.certified
    assert "evil" in verdict.reason


# A default value runs once, at definition, but a default holding a mutable global hands the
# callable live state through a parameter whose reads are never checked. The read in the
# default is what must reject it.
MUTABLE_DEFAULT = {
    "positional": "def guard(tokens, live=STATE):\n    return len(live) > 0\n",
    "keyword_only": "def guard(tokens, *, live=STATE):\n    return len(live) > 0\n",
    "lambda": "guard = lambda tokens, live=STATE: len(live) > 0  # noqa: E731\n",
}


@pytest.mark.parametrize("case", sorted(MUTABLE_DEFAULT))
def test_a_default_holding_a_mutable_global_does_not_certify(tmp_path, case):
    path = tmp_path / f"default_{case}.py"
    path.write_text(MODULE_PRELUDE + MUTABLE_DEFAULT[case])
    verdict = certify(_load(path, f"default_{case}_guard").guard)
    assert not verdict.certified
    assert verdict.reason == "reads mutable/uncertified external 'STATE'"


# Conservative rejects, not soundness rules: each guard really does bind STATE locally, so it
# never reads the global. The certifier rejects them only because it doesn't model these
# binding forms (except-as, match captures, a walrus in a nested lambda's default). Teaching
# it one of them may flip its case to certified; that is not a hole opening.
BINDING_FORM_NOT_MODELLED = {
    "except_name": (
        "def guard(tokens):\n    try:\n        pass\n    except ValueError as STATE:\n        pass\n"
        "    return len(STATE) > 0\n"
    ),
    "match_capture": (
        "def guard(tokens):\n    match tokens:\n        case [STATE]:\n            pass\n    return len(STATE) > 0\n"
    ),
    "nested_walrus_default": (
        "def guard(tokens):\n    h = lambda x=(STATE := 0): x  # noqa: E731\n    return len(STATE) > 0\n"
    ),
}


@pytest.mark.parametrize("case", sorted(BINDING_FORM_NOT_MODELLED))
def test_a_binding_form_the_certifier_does_not_model_is_conservatively_rejected(tmp_path, case):
    path = tmp_path / f"unmodelled_{case}.py"
    path.write_text(MODULE_PRELUDE + BINDING_FORM_NOT_MODELLED[case])
    verdict = certify(_load(path, f"unmodelled_{case}_guard").guard)
    assert not verdict.certified
    assert verdict.reason == "reads mutable/uncertified external 'STATE'"


@pytest.mark.parametrize("terms", [250, 400, 1500])
def test_a_guard_with_a_long_expression_still_certifies(tmp_path, terms):
    # Every AST walk in the gates must be iterative: a recursive one (copying the node,
    # collecting bindings) raised RecursionError somewhere between 250 and 1500 terms. Much
    # past 1500, Python 3.10's own compiler can't import the module.
    path = tmp_path / f"deep{terms}.py"
    path.write_text(f"def guard(tokens):\n    return {' + '.join(['len(tokens)'] * terms)} > 0\n")
    guard = _load(path, f"deep_guard_{terms}").guard
    assert certify(guard).certified
    Transition(name="t", inputs=[InputArc("p")], outputs=[OutputArc("q")], action=_passthrough, guard=guard)


def _deep_tuple(depth):
    value = ()
    for _ in range(depth):
        value = (value,)
    return value


def _helper_chain(length):
    helpers = "def h0(t):\n    return True\n\n\n" + "".join(
        f"def h{i}(t):\n    return h{i - 1}(t)\n\n\n" for i in range(1, length)
    )
    return helpers + f"def guard(tokens):\n    return h{length - 1}(tokens)\n"


# The walks are iterative, but a value or call graph nested past the recursion limit still
# recurses (_is_immutable on a closed-over tuple, certifying one helper per call). certify()
# never raises, so that must come back as not certified rather than as a RecursionError.
@pytest.mark.parametrize("shape", ["deep_tuple", "helper_chain"])
def test_something_nested_past_the_recursion_limit_is_not_certified(tmp_path, shape):
    limit = sys.getrecursionlimit()
    path = tmp_path / f"nested_{shape}.py"
    if shape == "deep_tuple":
        path.write_text("DEEP = None\n\n\ndef guard(tokens):\n    return DEEP is not None\n")
        module = _load(path, f"nested_{shape}_guard")
        module.DEEP = _deep_tuple(2 * limit)
    else:
        path.write_text(_helper_chain(limit + 200))
        module = _load(path, f"nested_{shape}_guard")
    assert certify(module.guard) == Verdict(False, "too deeply nested to certify")


def test_an_edited_file_is_parsed_again_by_every_check(tmp_path, monkeypatch):
    # With bytecode written, a same-size edit within one mtime tick reloads the old `.pyc`:
    # the code runs from before the edit, and the gates refuse it as not in its file
    # (`tests/test_purity_source_lookup.py`). Without, each load compiles the file as it is.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    path = tmp_path / "edited.py"
    path.write_text("import random\n\n\ndef guard(tokens):\n    return bool(tokens)\n")
    guard = _load(path, "edited_guard").guard
    verify_callable_purity(guard)
    assert certify(guard).certified

    _rewrite_same_size(path, "bool(tokens)", "open(tokens)")
    guard = _load(path, "edited_guard").guard
    assert not certify(guard).certified
    with pytest.raises(PermissionError, match="open"):
        verify_callable_purity(guard)

    path.write_text("import random\n\n\ndef guard(tokens):\n    return len(tokens) + 1\n")
    assert lint_callable(_load(path, "edited_guard").guard) == []
    _rewrite_same_size(path, "len(tokens) + 1", "random.random()")
    assert [f.symbol for f in lint_callable(_load(path, "edited_guard").guard)] == ["random.random"]


def test_a_latin_1_source_file_is_decoded_by_its_coding_cookie(tmp_path):
    path = tmp_path / "latin.py"
    # A callable whose source cannot be read and parsed fails the purity check, so passing it
    # shows this file was decoded.
    path.write_bytes(b"# -*- coding: latin-1 -*-\ndef guard(tokens):\n    return tokens.get('caf\xe9')\n")
    guard = _load(path, "latin_guard").guard
    verify_callable_purity(guard)
    assert lint_callable(guard) == []
    assert certify(guard).certified
