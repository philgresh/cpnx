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
from cpnx.certification import certify
from cpnx.linting import lint_callable
from cpnx.sandbox import _callable_node, verify_callable_purity

ANNOTATED = "def guard(tokens: list) -> bool:\n    seen: int = len(tokens)\n    return seen > 0\n"


def _passthrough(tokens):
    return list(tokens)


def _load(path, name):
    linecache.checkcache(str(path))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules.pop(name, None)
    return module


def _rewrite_same_size(path, old, new):
    # A same-size edit, so only the mtime tells the cache the file changed.
    text = path.read_text()
    assert len(old) == len(new) and old in text
    path.write_text(text.replace(old, new))
    stat = os.stat(path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert os.stat(path).st_size == stat.st_size


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
    before = ast.dump(_callable_node(guard), include_attributes=True)
    assert certify(guard).certified
    assert certify(guard).certified
    assert ast.dump(_callable_node(guard), include_attributes=True) == before


@pytest.mark.parametrize("terms", [250, 400])
def test_a_guard_with_a_long_expression_still_certifies(tmp_path, terms):
    # Copying the cached node recursed once per AST level and raised RecursionError here.
    path = tmp_path / f"deep{terms}.py"
    path.write_text(f"def guard(tokens):\n    return {' + '.join(['len(tokens)'] * terms)} > 0\n")
    guard = _load(path, f"deep_guard_{terms}").guard
    assert certify(guard).certified
    Transition(name="t", inputs=[InputArc("p")], outputs=[OutputArc("q")], action=_passthrough, guard=guard)


def test_an_edited_file_is_parsed_again_by_every_check(tmp_path):
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
    path.write_bytes(b"# -*- coding: latin-1 -*-\ndef guard(tokens):\n    return 'caf\xe9' in tokens\n")
    guard = _load(path, "latin_guard").guard
    verify_callable_purity(guard)
    assert lint_callable(guard) == []
    assert certify(guard).certified
