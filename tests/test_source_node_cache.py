"""A callable's source file is parsed once, and caching it changes no check's verdict.

Every `InputArc`, `OutputArc` and `Transition` checks its callables on construction: the purity
blocklist (`cpnx.sandbox`), the side-effect lint (`cpnx.linting`) and inline-safety
certification (`cpnx.certification`). Each needs the callable's node in its source file. Finding
it used to parse the file and walk the whole tree on every check, which made building many nets
from the same functions slow in proportion to the size of the module that defines them.

The shared parse itself is covered in `tests/test_source_cache.py`. These tests add what a cache
must not change: the same verdict, message and warning on every check, from one thread or several.
"""

import ast
import collections
import importlib.util
import itertools
import os
import threading
import warnings

import pytest

from cpnx import InputArc
from cpnx.certification import certify
from cpnx.linting import CpnxLintWarning, lint_callable
from cpnx.sandbox import verify_callable_purity

_SOURCE = """\
import time


def plain(token):
    return token.payload["n"]


def prints(token):
    print(token)
    return True


def reads_the_clock(token):
    return time.monotonic() > token.payload["n"]


def sleeps_in_an_annotation(token: time.sleep(0)) -> bool:
    return token.payload["n"] > 0
"""

_names = itertools.count()


def _load(path, source=_SOURCE):
    """Write `source` to `path` and import it as a module of its own."""
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(f"source_node_cache_case_{next(_names)}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def module(tmp_path):
    return _load(tmp_path / "callables.py")


@pytest.fixture
def work(monkeypatch):
    """Count the parses of each file and the walks of a whole module, the two costs per check."""
    counts: collections.Counter = collections.Counter()
    lock = threading.Lock()
    parse, walk = ast.parse, ast.walk

    def counting_parse(source, filename="<unknown>", *args, **kwargs):
        with lock:
            counts["parse", os.fspath(filename)] += 1
        return parse(source, filename, *args, **kwargs)

    def counting_walk(node):
        if isinstance(node, ast.Module):
            with lock:
                counts["walk"] += 1
        return walk(node)

    monkeypatch.setattr(ast, "parse", counting_parse)
    monkeypatch.setattr(ast, "walk", counting_walk)
    return counts


def _check_all(func) -> None:
    verify_callable_purity(func)
    lint_callable(func)
    certify(func)


def test_checks_of_one_callable_parse_and_walk_its_file_once(module, work):
    for _ in range(3):
        _check_all(module.plain)

    assert work["parse", module.__file__] == 1
    assert work["walk"] == 1


def test_callables_of_one_file_share_its_parse(module, work):
    _check_all(module.plain)
    _check_all(module.reads_the_clock)

    assert work["parse", module.__file__] == 1
    assert work["walk"] == 2  # one search of the tree for each line a callable starts on


def test_arcs_built_from_one_callable_parse_its_file_once(module, work):
    for _ in range(5):
        InputArc("p", key=module.plain)

    assert work["parse", module.__file__] == 1
    assert work["walk"] == 1


def test_threads_checking_at_once_all_get_the_callable_s_verdicts(module):
    # The cache takes no lock, so threads that meet on a file nobody has parsed yet may each
    # parse it. Whichever parse a thread ends up reading, its verdicts are the callable's own.
    start = threading.Barrier(8)
    results, errors = [], []

    def check():
        try:
            start.wait()
            verify_callable_purity(module.plain)
            results.append((certify(module.plain).certified, lint_callable(module.reads_the_clock)[0].symbol))
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=check) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert results == [(True, "time.monotonic")] * 8


def test_a_rejected_callable_is_rejected_every_time(module):
    messages = []
    for _ in range(3):
        with pytest.raises(PermissionError) as raised:
            verify_callable_purity(module.prints)
        messages.append(str(raised.value))
        with pytest.raises(PermissionError):
            InputArc("p", filter=module.prints)

    assert messages == ["Forbidden function call 'print' inside CPN callable."] * 3


def test_callables_on_different_lines_of_one_file_keep_their_own_verdicts(module):
    for _ in range(2):
        verify_callable_purity(module.plain)
        with pytest.raises(PermissionError):
            verify_callable_purity(module.prints)
        assert certify(module.plain).certified
        assert lint_callable(module.plain) == []
        assert [finding.symbol for finding in lint_callable(module.reads_the_clock)] == ["time.monotonic"]


def test_every_construction_warns_about_a_lint_finding(module):
    for _ in range(3):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            InputArc("p", filter=module.reads_the_clock)
        assert [w.category for w in caught] == [CpnxLintWarning]


def test_an_edited_source_file_is_read_again(tmp_path):
    path = tmp_path / "edited.py"
    module = _load(path, "def check(token):\n    return token.payload['n'] > 0\n")
    verify_callable_purity(module.check)

    # The function object is the old one; the checks read its file as it is now.
    path.write_text("def check(token):\n    return print(token) or token.payload['n'] > 0\n", encoding="utf-8")

    with pytest.raises(PermissionError):
        verify_callable_purity(module.check)


def test_certification_leaves_annotations_for_the_other_checks(module):
    # Certification blanks annotations out of the node it reads. The purity check must still
    # see them afterwards, as it did when each check parsed the file for itself.
    assert certify(module.sleeps_in_an_annotation).certified
    with pytest.raises(PermissionError, match=r"\.sleep"):
        verify_callable_purity(module.sleeps_in_an_annotation)
    assert certify(module.sleeps_in_an_annotation).certified
