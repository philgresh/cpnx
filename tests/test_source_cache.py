"""The purity check, linter and certifier share one parse per source file.

Each used to parse the callable's whole defining file and walk its whole tree for every guard,
key and action, on every transition built: about 0.1 s per callable in a 2,400-line test
module, which made the shared-place property tests take minutes.
"""

import importlib.util
import linecache
import os
import sys

import pytest

from cpnx import sandbox
from cpnx.certification import certify
from cpnx.linting import lint_callable
from cpnx.sandbox import _callable_node, verify_callable_purity

ANNOTATED = "def guard(tokens: list) -> bool:\n    return len(tokens) > 0\n"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules.pop(name, None)
    return module


def test_one_file_is_parsed_once_for_many_callables(tmp_path):
    path = tmp_path / "many.py"
    path.write_text("".join(f"def g{i}(tokens):\n    return {i}\n" for i in range(5)))
    module = _load(path, "many_callables")
    for i in range(5):
        guard = getattr(module, f"g{i}")
        verify_callable_purity(guard)
        assert lint_callable(guard) == []
        assert certify(guard).certified
    parsed = sandbox._SOURCE_CACHE[str(path)]
    assert sorted(parsed.targets) == [1, 3, 5, 7, 9]


def test_certifying_does_not_strip_annotations_from_the_shared_tree(tmp_path):
    path = tmp_path / "annotated.py"
    path.write_text(ANNOTATED)
    guard = _load(path, "annotated_guard").guard
    assert certify(guard).certified
    node = _callable_node(guard)
    assert node.args.args[0].annotation is not None
    assert node.returns is not None
    assert certify(guard).certified


def test_an_edited_file_is_parsed_again(tmp_path):
    path = tmp_path / "edited.py"
    path.write_text("def guard(tokens):\n    return True\n")
    verify_callable_purity(_load(path, "edited_guard").guard)
    path.write_text("def guard(tokens):\n    print(tokens)\n    return True\n")
    stat = os.stat(path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    linecache.checkcache(str(path))
    edited = _load(path, "edited_guard").guard
    with pytest.raises(PermissionError, match="print"):
        verify_callable_purity(edited)
