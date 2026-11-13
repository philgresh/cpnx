"""The purity check reads the callable's own body, or rejects it (#90).

It used to fall back, when it could not find the callable's `def` or `lambda`, to the text
`inspect.getsource` gave: it cut that at the first `=`, and when what was left did not parse
it let the callable through. A lambda on the last line of a call in a notebook cell
(`guard=lambda b: print(b))`) passed that way. Now every callable is looked up in the source
`inspect` holds for it, file or not, and one that is not found is rejected.
"""

import __future__

import ast
import asyncio
import dataclasses
import functools
import importlib
import importlib.util
import itertools
import linecache
import os
import sys
import time
import types
import warnings
import zipfile

import pytest

import cpnx.sandbox
from cpnx.certification import certify
from cpnx.linting import lint_callable
from cpnx.sandbox import _callable_nodes, _callable_nodes_or_empty, verify_callable_purity

_UNAVAILABLE = "source unavailable"
_numbers = itertools.count()


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


def test_a_file_edited_so_a_clean_def_starts_on_the_lambdas_line_is_rejected(tmp_path, either_way):
    # The running lambda is impure; the file now has a clean `def` on its line. Without
    # columns, the lookup fell back to that `def` and certified the lambda by it.
    module = _load_then_edit(
        tmp_path,
        "edited_def_90",
        "seen = []\nguard = lambda toks: seen.append(toks) or print(toks)\n",
        "seen = []\ndef helper(toks):\n    return True\n",
    )
    assert _callable_nodes(module.guard) == ()
    with pytest.raises(PermissionError, match=_UNAVAILABLE):
        verify_callable_purity(module.guard)
    assert certify(module.guard).reason == _UNAVAILABLE


def test_a_stale_pyc_is_not_judged_by_its_edited_file(tmp_path, either_way, monkeypatch):
    # A same-size edit within one mtime tick: the import reuses the old bytecode, so the code
    # that runs is not the code in the file. (That needs the bytecode written: `-B` is undone.)
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    path = tmp_path / "stale_90.py"
    path.write_text("guard = lambda t: print(t)\n")
    stamp = path.stat()
    sys.path.insert(0, str(tmp_path))
    try:
        importlib.import_module("stale_90")
        sys.modules.pop("stale_90")
        path.write_text("guard = lambda t: len(t)+1\n")
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        importlib.invalidate_caches()
        module = importlib.import_module("stale_90")
        assert module.guard.__code__.co_names == ("print",)  # the old bytecode runs
        with pytest.raises(PermissionError):
            verify_callable_purity(module.guard)
        assert not certify(module.guard).certified
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("stale_90", None)


def test_code_compiled_under_another_modules_name_is_not_judged_by_that_module(tmp_path, either_way):
    # `inspect` files a module's source under any file name its code was compiled with, and
    # the clean lambda on that line held the impure one's anchor.
    (tmp_path / "host_90.py").write_text("ok = lambda t: (t, t, t, t, t, True)\n")
    sys.path.insert(0, str(tmp_path))
    try:
        host = importlib.import_module("host_90")
        exec(compile("bad = lambda t: print(t)", "generated_rules_90.py", "exec"), host.__dict__)  # noqa: S102
        assert _callable_nodes_or_empty(host.bad) == ()
        with pytest.raises(PermissionError, match=_UNAVAILABLE):
            verify_callable_purity(host.bad)
        assert not certify(host.bad).certified
        assert "generated_rules_90.py" not in linecache.cache
        assert _is_pure(host.ok)
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("host_90", None)


def _load_then_edit(tmp_path, name, before, after):
    path = tmp_path / f"{name}.py"
    path.write_text(before)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path.write_text(after)
    return module


def test_an_edit_to_a_default_alone_does_not_hide_the_default_that_runs(tmp_path, either_way):
    # Defaults run where the function is defined, so the function's code is the same after
    # the edit. The default it holds is checked as it is.
    module = _load_then_edit(
        tmp_path,
        "edited_default_90",
        "def guard(t, k=lambda b: print(b)):\n    return k is not None\n",
        "def guard(t, k=lambda b: bool(b)):\n    return k is not None\n",
    )
    with pytest.raises(PermissionError, match=f"Default 'k' of guard: .*{_UNAVAILABLE}"):
        verify_callable_purity(module.guard)
    assert certify(module.guard).reason.startswith("default 'k' is mutable or uncertified")


def test_a_mutable_default_value_is_refused_whatever_the_source_says(tmp_path, either_way):
    module = _load_then_edit(
        tmp_path,
        "edited_state_90",
        "STATE = []\ndef guard(t, s=STATE):\n    return len(s) > 0\n",
        "STATE = []\ndef guard(t, s=()):\n    return len(s) > 0\n",
    )
    with pytest.raises(PermissionError, match="Mutable default argument 's'"):
        verify_callable_purity(module.guard)
    assert certify(module.guard).reason.startswith("default 's' is mutable or uncertified")


def test_defaults_a_guard_may_read_still_certify(tmp_path, either_way):
    path = tmp_path / "plain_defaults_90.py"
    path.write_text(
        "def helper(t):\n    return len(t)\n\n\n"
        "def guard(t, n=3, pair=(1, 'a'), size=len, count=helper, *, cap=None):\n"
        "    return len(t) < n and pair[0] == 1 and size is not None and count is not cap\n"
    )
    spec = importlib.util.spec_from_file_location("plain_defaults_90", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    verify_callable_purity(module.guard)
    assert certify(module.guard).certified, certify(module.guard).reason


def test_an_unexpected_lookup_error_reaches_every_gate_alike(monkeypatch):
    def broken(path, func, code):
        raise RuntimeError("lookup bug")

    monkeypatch.setattr(cpnx.sandbox, "_parsed_source", broken)
    guard = lambda tokens: True  # noqa: E731
    for gate in (verify_callable_purity, certify, lint_callable):
        with pytest.raises(RuntimeError, match="lookup bug"):
            gate(guard)


def test_a_wrapped_callable_is_looked_up_in_its_own_file(tmp_path):
    # The lint reads the body a decorator wraps: its line, and its file, not the wrapper's.
    # Looked up in the wrapper's file, it was not found and the lint reported nothing.
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
        # The purity check reads the same body, but the wrapper is what runs: it refuses both.
        with pytest.raises(PermissionError, match="decorated"):
            verify_callable_purity(module.guard)
        assert "decorated" in certify(module.guard).reason
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("wrapped_90", None)
        sys.modules.pop("wrappers_90", None)


def test_a_partial_is_still_rejected():
    def key(token, scale):
        return token.payload["n"] * scale

    with pytest.raises(PermissionError, match=_UNAVAILABLE):
        verify_callable_purity(functools.partial(key, scale=2))


def _load(tmp_path, name, source):
    path = tmp_path / f"{name}.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        spec.loader.exec_module(module)
    return module


def test_a_compiler_warning_elsewhere_in_the_file_does_not_fail_the_gates(tmp_path, either_way):
    # The import warned once (or not at all, from a `.pyc`); the gates' own compile of the file
    # must not warn again, and under an "error" filter must not refuse the file's callables.
    # The parser warns too: an invalid escape sequence.
    module = _load(
        tmp_path,
        "warns_90",
        "PAT = '\\d+'\n\n\ndef guard(t):\n    return len(t) > 0\n\n\ndef odd(x):\n    return x is 1\n",
    )
    filters = warnings.filters
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("error")
        before = list(warnings.filters)
        verify_callable_purity(module.guard)
        assert certify(module.guard).certified, certify(module.guard).reason
        assert lint_callable(module.guard) == []
        # The silencing is undone, and is not a list put in place of the one in use.
        assert warnings.filters == before
    assert caught == []
    assert warnings.filters is filters


def test_silencing_a_files_warnings_leaves_other_warnings_alone(tmp_path):
    # While the gates compile one file, a warning about anything else still reaches its filter.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with cpnx.sandbox._quiet_about(str(tmp_path / "quiet_90.py")):
            warnings.warn("elsewhere", UserWarning, stacklevel=1)
            warnings.warn_explicit("here", SyntaxWarning, str(tmp_path / "quiet_90.py"), 1)
    assert [str(each.message) for each in caught] == ["elsewhere"]


def test_a_file_that_parses_but_no_longer_compiles_is_unavailable_to_every_gate(tmp_path):
    module = _load_then_edit(
        tmp_path,
        "uncompilable_90",
        "guard = lambda t: len(t) > 0\n",
        "guard = lambda t: len(t) > 0\nreturn 1\n",
    )
    with pytest.raises(PermissionError, match=_UNAVAILABLE):
        verify_callable_purity(module.guard)
    assert certify(module.guard).reason == "source unavailable"
    assert lint_callable(module.guard) == []


def test_a_cell_with_top_level_await_is_read_like_any_other(tmp_path, either_way):
    # IPython's autoawait (and `python -m asyncio`) compile such a cell with top-level await
    # allowed; the lambda in it is code like any other.
    source = "await asyncio.sleep(0)\nguard = lambda b: len(b) > 0\nloud = lambda b: print(b)\n"
    filename = str(tmp_path / "kernel" / "awaits_90.py")
    linecache.cache[filename] = (len(source), None, source.splitlines(True), filename)
    namespace = {"__name__": "__main__", "asyncio": asyncio}
    code = compile(source, filename, "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    asyncio.run(eval(code, namespace))  # noqa: S307 - the cell's coroutine, as the kernel runs it
    verify_callable_purity(namespace["guard"])
    assert certify(namespace["guard"]).certified, certify(namespace["guard"]).reason
    assert not _is_pure(namespace["loud"])


@dataclasses.dataclass
class _Threshold:
    limit: int

    def __call__(self, tokens):
        return len(tokens) > self.limit


def test_an_unhashable_callable_is_not_certified_rather_than_raising(tmp_path):
    module = _load(
        tmp_path,
        "unhashable_90",
        "def make(limit, threshold):\n"
        "    over = threshold(limit)\n"
        "    def guard(toks, _over=over):\n"
        "        return _over is not None and len(toks) > 0\n"
        "    return guard\n\n\n"
        "def reads(toks):\n    return OVER(toks)\n",
    )
    assert certify(module.make(3, _Threshold)).reason.startswith("default '_over' is mutable or uncertified")
    module.OVER = _Threshold(3)
    assert not certify(module.reads).certified
    assert certify(_Threshold(3)).reason == "unhashable callable"


def test_a_default_function_with_no_source_to_read_is_left_to_certification(tmp_path, either_way):
    namespace = {}
    exec(compile("def nowhere(x):\n    return x\n", "<no source 90>", "exec"), namespace)  # noqa: S102
    module = _load(
        tmp_path,
        "sourceless_default_90",
        "def joined(toks, _join=None):\n    return len(toks) > 0\n",
    )
    module.joined.__defaults__ = (namespace["nowhere"],)
    verify_callable_purity(module.joined)
    assert certify(module.joined).reason.startswith("default '_join' is mutable or uncertified")
    module.joined.__defaults__ = (os.path.join,)
    verify_callable_purity(module.joined)


def test_default_functions_that_hold_each_other_are_checked_once(tmp_path, either_way):
    module = _load(
        tmp_path, "cycle_90", "def a(t, k=None):\n    return len(t)\n\n\ndef b(t, k=None):\n    return len(t)\n"
    )
    module.a.__defaults__ = (module.b,)
    module.b.__defaults__ = (module.a,)
    verify_callable_purity(module.a)
    assert not certify(module.a).certified


def test_an_impure_default_function_is_named_with_its_parameter(tmp_path, either_way):
    module = _load(
        tmp_path,
        "named_default_90",
        "def loud(t):\n    print(t)\n\n\ndef guard(t, *, check=loud):\n    return len(t)\n",
    )
    with pytest.raises(PermissionError, match="Default 'check' of guard: Forbidden function call 'print'"):
        verify_callable_purity(module.guard)


def test_a_file_edited_only_in_its_layout_still_reads_the_callable(tmp_path, either_way):
    # The code is the same; only where its pieces sit in the line changed. Positions are not
    # what runs, so the edited file still holds the callable.
    module = _load_then_edit(
        tmp_path,
        "relaid_90",
        "guard = lambda t: len(t) > 0 and t[0] is not None\n",
        "guard = lambda t:   len(t) > 0   and   t[0] is not None\n",
    )
    linecache.checkcache(str(tmp_path / "relaid_90.py"))
    verify_callable_purity(module.guard)
    assert certify(module.guard).certified, certify(module.guard).reason


def test_code_rewritten_as_it_was_loaded_is_rejected_saying_so(tmp_path, either_way):
    # An import hook (pytest's assertion rewriting, typeguard) compiles other code than the
    # file says. The callable is rejected, with a message that does not blame its form.
    path = tmp_path / "hooked_90.py"
    path.write_text("def guard(t):\n    return len(t) > 0\n")
    tree = ast.parse(path.read_text())
    tree.body[0].body[0].value = ast.parse("len(t) > 1", mode="eval").body
    ast.fix_missing_locations(tree)
    namespace = {"__name__": "hooked_90"}
    exec(compile(tree, str(path), "exec"), namespace)  # noqa: S102 - what the hook would load
    with pytest.raises(PermissionError, match="import hook"):
        verify_callable_purity(namespace["guard"])


def test_a_builtin_default_certifies_only_if_a_guard_may_call_it(tmp_path):
    module = _load(
        tmp_path,
        "builtin_default_90",
        "def quiet(t, f=len):\n    return f is not None\n\n\ndef loud(t, f=print):\n    return f is not None\n",
    )
    assert certify(module.quiet).certified, certify(module.quiet).reason
    assert certify(module.loud).reason.startswith("default 'f' is mutable or uncertified")


def test_a_method_of_an_immutable_builtin_is_a_default_a_guard_may_hold(tmp_path):
    module = _load(
        tmp_path,
        "method_defaults_90",
        "def lowered(ts, f=str.lower, j='-'.join):\n    return f is not None and j is not None\n\n\n"
        "def grows(ts, f=list.append):\n    return f is not None\n",
    )
    assert certify(module.lowered).certified, certify(module.lowered).reason
    assert certify(module.grows).reason.startswith("default 'f' is mutable or uncertified")


@pytest.mark.skipif(sys.version_info < (3, 11), reason="code records column positions from 3.11")
def test_code_without_positions_is_still_found_by_its_compiled_twin(tmp_path, monkeypatch):
    # A `.pyc` written under `-X no_debug_ranges` and run without it: the running code has no
    # columns, the source compiled here has. The compiled twin still says which lambda it is.
    # By names alone the two lambdas cannot be told apart.
    module = _load(tmp_path, "twin_90", "low, high = (lambda t: len(t) > 0), (lambda t: len(t) > 1)\n")
    monkeypatch.setattr(cpnx.sandbox, "_body_anchor", lambda func: None)
    cpnx.sandbox._SOURCE_CACHE.clear()
    (low,), (high,) = _callable_nodes(module.low), _callable_nodes(module.high)
    assert ast.unparse(low.body) == "len(t) > 0"
    assert ast.unparse(high.body) == "len(t) > 1"


def test_the_source_cache_keeps_the_files_used_last(tmp_path, monkeypatch):
    monkeypatch.setattr(cpnx.sandbox, "_SOURCE_CACHE_SIZE", 2)
    cpnx.sandbox._SOURCE_CACHE.clear()
    modules = [_load(tmp_path, f"cached_{index}_90", "guard = lambda t: len(t) > 0\n") for index in range(3)]
    for module in modules:
        verify_callable_purity(module.guard)
    verify_callable_purity(modules[1].guard)
    assert list(cpnx.sandbox._SOURCE_CACHE) == [modules[2].__file__, modules[1].__file__]
    cpnx.sandbox._SOURCE_CACHE.clear()


def test_a_bound_method_of_a_value_holding_a_mutable_one_is_not_certified(tmp_path):
    module = _load(tmp_path, "bound_default_90", "def counts(ts, f=None):\n    return f is not None\n")
    module.counts.__defaults__ = (([],).count,)
    assert certify(module.counts).reason.startswith("default 'f' is mutable or uncertified")
    module.counts.__defaults__ = ((1, "a").count,)
    assert certify(module.counts).certified, certify(module.counts).reason


def test_a_global_and_a_default_holding_one_value_are_judged_alike(tmp_path):
    module = _load(
        tmp_path,
        "alike_rule_90",
        "SIZE = len\nLOWER = str.lower\nCONJ = complex.conjugate\n\n\n"
        "def by_global(ts):\n    return SIZE is not None and LOWER is not None\n\n\n"
        "def by_default(ts, size=len, lower=str.lower):\n    return size is not None and lower is not None\n\n\n"
        "def conj_global(ts):\n    return CONJ is not None\n\n\n"
        "def conj_default(ts, conj=complex.conjugate):\n    return conj is not None\n",
    )
    assert certify(module.by_global).certified, certify(module.by_global).reason
    assert certify(module.by_default).certified, certify(module.by_default).reason
    assert not certify(module.conj_global).certified
    assert not certify(module.conj_default).certified


class _Forwarding:
    """Runs its own `__call__`, and forwards every other attribute to the function it holds."""

    def __init__(self, func):
        self.func = func

    def __call__(self, *args):
        print("ran")  # what the gates must not miss

    def __getattr__(self, name):
        return getattr(self.func, name)


def _carrying_code(func):
    holder = type("Carrier", (), {"__call__": lambda self, *args: print("ran")})()
    holder.__code__ = func.__code__
    return holder


@pytest.mark.parametrize(
    "disguise",
    [
        _Forwarding,
        _carrying_code,
        lambda func: type("CarrierClass", (), {"__code__": func.__code__}),
        lambda func: types.MethodType(_Forwarding(func), object()),
    ],
    ids=["forwarding object", "object carrying __code__", "class carrying __code__", "method of a forwarder"],
)
def test_a_callable_object_is_not_judged_by_a_function_it_points_at(tmp_path, disguise):
    # What runs is the object's own `__call__`, not the code it forwards or carries.
    module = _load(tmp_path, f"pointed_at_{next(_numbers)}_90", "guard = lambda b: len(b) > 0\n")
    verify_callable_purity(module.guard)
    impostor = disguise(module.guard)
    assert _callable_nodes(impostor) == ()
    with pytest.raises(PermissionError, match=_UNAVAILABLE):
        verify_callable_purity(impostor)
    assert not certify(impostor).certified
    assert lint_callable(impostor) == []


@pytest.mark.parametrize("decorate", ["functools.lru_cache", "staticmethod"])
def test_lint_reads_a_decorated_callable_whose_wrapper_is_not_a_function(tmp_path, decorate):
    module = _load(
        tmp_path,
        f"wrapped_kind_{next(_numbers)}_90",
        f"import functools\nimport time\n\n\n@{decorate}\ndef cached(n):\n    return time.time() > n\n",
    )
    assert [finding.symbol for finding in lint_callable(module.cached)] == ["time.time"]
    with pytest.raises(PermissionError, match="decorated"):
        verify_callable_purity(module.cached)
    assert not certify(module.cached).certified


def test_a_default_whose_source_is_broken_is_refused_and_one_with_none_is_left_to_certification(tmp_path):
    helper = _load(tmp_path, "loud_helper_90", "def loud(t):\n    print(t)\n")
    module = _load(tmp_path, "holds_loud_90", "def guard(t, check=None):\n    return len(t)\n")
    module.guard.__defaults__ = (helper.loud,)
    with pytest.raises(PermissionError, match="Default 'check' of guard: Forbidden function call 'print'"):
        verify_callable_purity(module.guard)
    (tmp_path / "loud_helper_90.py").write_text("def loud(t:\n")  # no longer parses
    with pytest.raises(PermissionError, match=f"Default 'check' of guard: .*{_UNAVAILABLE}"):
        verify_callable_purity(module.guard)
    (tmp_path / "loud_helper_90.py").unlink()  # no source at all
    linecache.checkcache(str(tmp_path / "loud_helper_90.py"))
    verify_callable_purity(module.guard)
    assert not certify(module.guard).certified


def test_a_decorated_default_is_refused_for_its_wrapper(tmp_path):
    module = _load(
        tmp_path,
        "decorated_default_90",
        "import functools\n\n\ndef logged(func):\n    @functools.wraps(func)\n    def wrapper(t):\n"
        "        return func(t)\n\n    return wrapper\n\n\n"
        "def guard(t, check=None):\n    return len(t)\n",
    )
    # The wrapped function has no source to read, but its wrapper, which is what runs, does.
    namespace = {}
    exec(compile("def memo(t):\n    return len(t)\n", "<no source 90>", "exec"), namespace)  # noqa: S102
    module.guard.__defaults__ = (module.logged(namespace["memo"]),)
    with pytest.raises(PermissionError, match="Default 'check' of guard: .*decorated"):
        verify_callable_purity(module.guard)


def test_a_folded_nan_constant_still_matches_its_source(tmp_path, either_way):
    module = _load(tmp_path, "nan_90", "guard = lambda t: t == (1e999 - 1e999) or t == ((1e999 - 1e999) * 1j, 1)\n")
    verify_callable_purity(module.guard)
    assert certify(module.guard).certified, certify(module.guard).reason


def test_a_cell_compiled_under_an_earlier_future_import_is_read(tmp_path, either_way):
    # A notebook keeps the `__future__` flags of earlier cells: this cell's code carries
    # `annotations` without saying so in its text.
    source = "def guard(t: Undeclared) -> bool:\n    return len(t) > 0\n"
    filename = str(tmp_path / "kernel" / "future_90.py")
    linecache.cache[filename] = (len(source), None, source.splitlines(True), filename)
    namespace = {"__name__": "__main__"}
    exec(compile(source, filename, "exec", flags=__future__.annotations.compiler_flag), namespace)  # noqa: S102
    verify_callable_purity(namespace["guard"])
    assert certify(namespace["guard"]).certified, certify(namespace["guard"]).reason


@pytest.mark.parametrize(
    ("default", "forbidden"),
    [
        ("print", "print"),
        ("os.system", "system"),
        ("time.sleep", "sleep"),
        ("functools.partial(print, end='')", "print"),
        ("functools.partial(functools.partial(time.sleep, 0))", "sleep"),
    ],
)
def test_a_forbidden_builtin_held_as_a_default_is_refused(tmp_path, default, forbidden):
    module = _load(
        tmp_path,
        f"forbidden_default_{next(_numbers)}_90",
        f"import functools\nimport os\nimport time\n\n\ndef guard(t, run={default}):\n    return len(t) > 0\n",
    )
    with pytest.raises(PermissionError, match=f"Forbidden function '{forbidden}' held as default 'run' of guard"):
        verify_callable_purity(module.guard)


def test_a_bound_method_held_as_a_default_is_checked_as_its_function(tmp_path):
    module = _load(
        tmp_path,
        "method_default_90",
        "class Noisy:\n    def loud(self, t):\n        print(t)\n\n\n"
        "def guard(t, check=Noisy().loud):\n    return len(t) > 0\n",
    )
    with pytest.raises(PermissionError, match="Default 'check' of guard: Forbidden function call 'print'"):
        verify_callable_purity(module.guard)
