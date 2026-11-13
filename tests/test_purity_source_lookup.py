"""The purity check reads the callable's own body, or rejects it (#90).

It used to fall back, when it could not find the callable's `def` or `lambda`, to the text
`inspect.getsource` gave: it cut that at the first `=`, and when what was left did not parse
it let the callable through. A lambda on the last line of a call in a notebook cell
(`guard=lambda b: print(b))`) passed that way. Now every callable is looked up in the source
`inspect` holds for it, file or not, and one that is not found is rejected.
"""

import functools
import importlib.util
import linecache
import sys
import time
import zipfile

import pytest

from cpnx.certification import certify
from cpnx.linting import lint_callable
from cpnx.sandbox import _callable_nodes, verify_callable_purity

_UNAVAILABLE = "source unavailable"


def _is_pure(func) -> bool:
    try:
        verify_callable_purity(func)
    except PermissionError:
        return False
    return True


def _cell(tmp_path, name, source):
    """Run `source` as a notebook does a cell: under a file name that is only in `linecache`."""
    filename = str(tmp_path / "kernel" / f"{name}.py")  # never written to disk
    linecache.cache[filename] = (len(source), None, source.splitlines(True), filename)
    namespace = {"__name__": "__main__", "time": time}
    exec(compile(source, filename, "exec"), namespace)  # noqa: S102 - a notebook cell is exec'd
    return namespace


_CELL = """\
arcs = dict(n=1,
            clean=lambda b: len(b) > 0,
            clocked=lambda b: time.monotonic() > 0,
            last=lambda b: print(b))
"""


def test_a_notebook_cell_lambda_is_read_from_the_cell(tmp_path):
    arcs = _cell(tmp_path, "cell_1", _CELL)["arcs"]
    # The last one's line ends the call: cut at its `=`, it did not parse, and it passed.
    with pytest.raises(PermissionError, match="Forbidden function call 'print'"):
        verify_callable_purity(arcs["last"])
    assert _is_pure(arcs["clean"])
    assert certify(arcs["clean"]).certified
    assert [finding.symbol for finding in lint_callable(arcs["clocked"])] == ["time.monotonic"]


@pytest.mark.skipif(not hasattr(linecache, "_register_code"), reason="the REPL keeps its input from 3.13")
def test_a_repl_lambda_is_read_from_the_repl_input():
    source = "f, g = (lambda t: t.payload['n'] > 0), (lambda t: print(t))\n"
    filename = "<python-input-90>"
    code = compile(source, filename, "exec")
    linecache._register_code(code, source, filename)
    namespace = {"__name__": "__main__"}
    exec(code, namespace)  # noqa: S102 - the REPL execs each input in __main__
    assert len(_callable_nodes(namespace["f"])) == 1
    assert _is_pure(namespace["f"])
    assert not _is_pure(namespace["g"])


def test_a_module_in_a_zip_archive_is_read_through_its_loader(tmp_path):
    archive = tmp_path / "pkg.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("zipped_guards_90.py", "clean = lambda t: t.payload['n']\nprints = lambda t: print(t)\n")
    sys.path.insert(0, str(archive))
    try:
        module = importlib.import_module("zipped_guards_90")
        assert _is_pure(module.clean)
        assert not _is_pure(module.prints)
    finally:
        sys.path.remove(str(archive))
        sys.modules.pop("zipped_guards_90", None)


def test_code_with_no_source_is_rejected():
    namespace = {}
    exec(compile("guard = lambda t: True", "<string>", "exec"), namespace)  # noqa: S102
    with pytest.raises(PermissionError, match=_UNAVAILABLE):
        verify_callable_purity(namespace["guard"])


def test_a_class_is_rejected_rather_than_read_as_text():
    class Key:
        def __init__(self, token):
            self.n = token.payload["n"]

    with pytest.raises(PermissionError, match=_UNAVAILABLE):
        verify_callable_purity(Key)


def test_a_callable_whose_file_no_longer_holds_it_is_rejected(tmp_path):
    path = tmp_path / "edited_90.py"
    path.write_text("\n\nguard = lambda t: open(t)\n")
    spec = importlib.util.spec_from_file_location("edited_90", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Line 3 no longer starts a function. Read as text, its tail parsed and passed.
    path.write_text("x = 1\ny = 2\nz = (x, y)\n")
    linecache.checkcache(str(path))
    with pytest.raises(PermissionError, match=_UNAVAILABLE):
        verify_callable_purity(module.guard)


def test_a_wrapped_callable_is_looked_up_in_its_own_file(tmp_path):
    # `getsourcelines` gives the line of the function a decorator wraps; the file must be that
    # function's too, not the wrapper's. Looked up in the wrapper's file, it was not found
    # and the lint reported nothing.
    (tmp_path / "wrappers_90.py").write_text(
        "import functools\n\n\ndef logged(func):\n"
        "    @functools.wraps(func)\n    def wrapper(*args):\n        return func(*args)\n\n    return wrapper\n"
    )
    (tmp_path / "wrapped_90.py").write_text(
        "import time\n\nfrom wrappers_90 import logged\n\n\n"
        "@logged\ndef guard(tokens):\n    return time.monotonic() > 0\n"
    )
    sys.path.insert(0, str(tmp_path))
    try:
        module = importlib.import_module("wrapped_90")
        assert [node.name for node in _callable_nodes(module.guard)] == ["guard"]
        assert [finding.symbol for finding in lint_callable(module.guard)] == ["time.monotonic"]
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("wrapped_90", None)
        sys.modules.pop("wrappers_90", None)


def test_a_partial_is_still_rejected():
    def key(token, scale):
        return token.payload["n"] * scale

    with pytest.raises(PermissionError, match=_UNAVAILABLE):
        verify_callable_purity(functools.partial(key, scale=2))
