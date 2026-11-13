"""Each callable is checked against its own body, also when another one starts on its line.

The purity blocklist, the side-effect lint and inline-safety certification find a callable's
node in its source file. They used to go by the line it starts on alone, so of two lambdas
on one line both were read as the last one: an impure lambda passed the purity check and was
certified to run inline because its neighbour was clean.

The node is now one that starts on the callable's first line. Where the interpreter records
columns (`code.co_positions`, Python 3.11 and later) it is the one whose body holds the
callable's own code. Without columns, a node is ruled out when it cannot be the callable: a
`def` of another name, a lambda where the callable is a `def`, or a body that names
something the callable's code does not. Callables that are still alike after that cannot be
told apart; each is held to what any of them does, and the error says so.
"""

import importlib
import importlib.util
import inspect
import itertools
import os
import pkgutil
import subprocess
import sys

import pytest

import cpnx.sandbox
from cpnx import InputArc, OutputArc
from cpnx.certification import certify
from cpnx.linting import lint_callable
from cpnx.sandbox import verify_callable_purity

_SOURCE = """\
import time

from cpnx import Transition

prints, clean = (lambda t: print(t)), (lambda t: t.payload["n"])
clean_first, prints_last = (lambda t: t.payload["n"]), (lambda t: print(t))
clocked, steady = (lambda t: time.monotonic() > t.payload["n"]), (lambda t: t.payload["n"] > 0)
key_a, key_b = (lambda t: t.payload["a"]), (lambda t: t.payload["b"])
make = lambda k: time.sleep(0) or (lambda t: t.payload["n"] > k)
made = make(0)
def impure_body(tokens, pick=lambda t: t.payload["n"]):
    return print(tokens) or len(tokens) > 0
long_first, short_last = (lambda t: (t.payload["a"]
                                     + time.sleep(0))), (lambda t: t.payload["n"])
def keep(*checks):
    return lambda func: func
@keep(lambda t: t.payload["n"])
def decorated(tokens):
    return print(tokens) or len(tokens) > 0
sleeps, reads = (lambda t: t.payload.sleep()), (lambda t: t.payload.sleep)
pulls, plain = (lambda t: (yield t.payload["n"])), (lambda t: t.payload["n"])
clock_call, clock_ref = (lambda t: time.monotonic() > t.payload["n"]), (lambda t: (time.monotonic, t.payload["n"]))
def action_then_guard(**arcs):
    return Transition("t", action=lambda b: print(b), guard=lambda b: True, **arcs)
def guard_then_action(**arcs):
    return Transition("t", guard=lambda b: True, action=lambda b: print(b), **arcs)
def same(func):
    return func
def outer(seen):
    @same
    def inner(tokens):
        return len(seen) > 0
    return inner
def outer_plain(seen):
    def inner(tokens):
        return len(seen) > 0
    return inner
def outer_impure():
    @same
    def inner(tokens):
        count = len(tokens)
        print(count)
        return count == 1
    return inner
def outer_clocked():
    @same
    def inner(tokens):
        return time.monotonic() > 0
    return inner
@same
def top_impure(tokens):
    count = len(tokens)
    print(count)
    return count == 1
clean_g, dead_genexp = (lambda b: True), (lambda b: print(b) if 1 else (ww for ww in b))
clean_l, dead_listcomp = (lambda b: True), (lambda b: [ww for ww in b] if 0 else print(b))
clean_d, dead_dictcomp = (lambda b: True), (lambda b: print(b) or 0 and {ww: 1 for ww in b})
clean_n, dead_lambda = (lambda b: True), (lambda b: 0 and (lambda: ww) or print(b))
class Mangles:
    reads, prints = (lambda s: s.__x), (lambda s: print(s))
"""

_names = itertools.count()

has_columns = pytest.mark.skipif(sys.version_info < (3, 11), reason="code objects carry no columns before 3.11")


@pytest.fixture(scope="module")
def case(tmp_path_factory):
    """A module whose callables share source lines, imported from a file of its own."""
    path = tmp_path_factory.mktemp("same_line") / "callables.py"
    path.write_text(_SOURCE, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(f"same_line_case_{next(_names)}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _forget_columns(monkeypatch):
    monkeypatch.setattr(cpnx.sandbox, "_body_anchor", lambda func: None)
    cpnx.sandbox._SOURCE_CACHE.clear()


@pytest.fixture
def no_columns(monkeypatch):
    """Check as an interpreter without column positions does (3.10, or `-X no_debug_ranges`)."""
    _forget_columns(monkeypatch)
    yield
    cpnx.sandbox._SOURCE_CACHE.clear()


@pytest.fixture(params=["columns as the interpreter records them", "no columns"])
def either_way(request, monkeypatch):
    """Run a test on both lookups: what must hold whether or not there are columns."""
    if request.param == "no columns":
        _forget_columns(monkeypatch)
    yield
    cpnx.sandbox._SOURCE_CACHE.clear()


def _is_pure(func) -> bool:
    try:
        verify_callable_purity(func)
    except PermissionError:
        return False
    return True


def _lint_symbols(func) -> list[str]:
    return [finding.symbol for finding in lint_callable(func)]


# -- with columns: each callable is its own node ------------------------------------------------


@has_columns
def test_purity_rejects_only_the_impure_lambda_of_a_line(case):
    assert not _is_pure(case.prints)
    assert _is_pure(case.clean)
    assert _is_pure(case.clean_first)
    assert not _is_pure(case.prints_last)


@has_columns
def test_certification_certifies_only_the_clean_lambda_of_a_line(case):
    assert not certify(case.prints).certified
    assert certify(case.clean).certified
    assert certify(case.clean_first).certified
    assert not certify(case.prints_last).certified


@has_columns
def test_lint_reports_a_clock_read_only_for_the_lambda_that_makes_it(case):
    (finding,) = lint_callable(case.clocked)
    assert finding.symbol == "time.monotonic"
    assert "functions start on line" not in finding.message
    assert _lint_symbols(case.steady) == []


@has_columns
def test_an_arc_takes_each_selection_callable_for_what_it_is(case):
    with pytest.raises(PermissionError, match="print"):
        InputArc("p", key=case.prints)
    arc = InputArc("p", key=case.clean)
    assert arc._key_inline_safe is True


@has_columns
def test_a_lambda_and_the_lambda_it_returns_are_told_apart(case):
    assert not _is_pure(case.make)
    assert _is_pure(case.made)
    assert certify(case.made).certified


@has_columns
def test_a_def_is_not_taken_for_a_default_lambda_on_its_first_line(case):
    assert not _is_pure(case.impure_body)
    assert not certify(case.impure_body).certified
    pick = case.impure_body.__defaults__[0]
    assert _is_pure(pick)
    assert certify(pick).certified


@has_columns
def test_a_lambda_ending_on_a_line_is_told_from_the_one_starting_there(case):
    assert not _is_pure(case.long_first)
    assert _is_pure(case.short_last)


@has_columns
def test_lambdas_that_name_the_same_things_are_told_apart_by_position(case):
    assert not _is_pure(case.sleeps)
    assert _is_pure(case.reads)
    assert not certify(case.pulls).certified
    assert certify(case.plain).certified
    assert _lint_symbols(case.clock_call) == ["time.monotonic"]
    assert _lint_symbols(case.clock_ref) == []


# -- with or without columns ---------------------------------------------------------------------


_DECORATED = "decorated callable (wrapper hides certified body)"


def _located_as(func) -> list[tuple[str | None, int]]:
    return [(getattr(node, "name", None), node.lineno) for node in cpnx.sandbox._callable_nodes(func)]


def test_a_decorated_def_is_not_taken_for_a_lambda_in_its_decorator(case, either_way):
    # The def's first line is its decorator's, and so is the lambda passed to the decorator.
    # The def is found as itself, from its first decorator's line, and never as that lambda.
    assert _located_as(case.decorated) == [("decorated", case.decorated.__code__.co_firstlineno + 1)]
    assert not _is_pure(case.decorated)
    verdict = certify(case.decorated)
    assert not verdict.certified
    assert verdict.reason == _DECORATED


def test_a_decorated_def_inside_a_function_is_not_taken_for_that_function(case, either_way):
    # Read as the function around it, `seen` would count as a local and the read of it would
    # certify. Read as itself, it is refused for its decorator.
    assert _located_as(case.outer([])) == [("inner", case.outer([]).__code__.co_firstlineno + 1)]
    assert certify(case.outer([])).reason == _DECORATED
    assert "'seen'" in certify(case.outer_plain([])).reason


def test_a_decorated_def_is_held_to_its_own_body(case, either_way):
    # Not located, these fell back to the source text, which gives up on any `=` and lets
    # the callable through; located, the purity check and the lint read their own bodies.
    for func in (case.outer_impure(), case.top_impure):
        with pytest.raises(PermissionError, match="Forbidden function call 'print'"):
            verify_callable_purity(func)
        assert not certify(func).certified
    assert _lint_symbols(case.outer_clocked()) == ["time.monotonic"]


def test_a_guard_on_one_line_with_an_impure_action_is_checked_alone(case, either_way):
    # An action may have side effects; the guard beside it names no `print`.
    arcs = {"inputs": [InputArc("p")], "outputs": [OutputArc("q")]}
    assert case.action_then_guard(**arcs).name == "t"
    assert case.guard_then_action(**arcs).name == "t"


def test_a_lambda_ending_on_a_line_does_not_start_there(case, either_way):
    assert _is_pure(case.short_last)
    assert certify(case.short_last).certified


# -- without columns: told apart by kind, name and what the body names ---------------------------


def test_without_columns_lambdas_that_name_different_things_are_told_apart(case, no_columns):
    assert not _is_pure(case.prints)
    assert _is_pure(case.clean)
    assert certify(case.clean).certified
    assert certify(case.clean_first).certified
    assert not certify(case.prints_last).certified
    assert _lint_symbols(case.clocked) == ["time.monotonic"]
    assert _lint_symbols(case.steady) == []


def test_without_columns_a_returned_lambda_is_told_from_the_one_that_returns_it(case, no_columns):
    # The outer lambda names `time`; the code of the one it returns does not.
    assert _is_pure(case.made)
    assert certify(case.made).certified
    # The outer one's code holds the inner one's, so by names alone it could be either.
    assert not _is_pure(case.make)


def test_without_columns_a_def_and_a_default_lambda_are_told_apart_by_kind(case, no_columns):
    assert not _is_pure(case.impure_body)
    pick = case.impure_body.__defaults__[0]
    assert _is_pure(pick)
    assert certify(pick).certified


def test_without_columns_purity_holds_lambdas_alike_to_each_other_and_says_so(case, no_columns):
    for func in (case.sleeps, case.reads):
        with pytest.raises(PermissionError) as raised:
            verify_callable_purity(func)
        message = str(raised.value)
        assert "Forbidden attribute call '.sleep'" in message
        assert "2 functions start on line" in message
        assert "separate lines" in message


def test_without_columns_neither_of_two_lambdas_alike_is_certified(case, no_columns):
    for func in (case.pulls, case.plain):
        verdict = certify(func)
        assert not verdict.certified
        assert "2 functions start on line" in verdict.reason
        assert "separate lines" in verdict.reason
        assert "await/yield" in verdict.reason


def test_without_columns_lint_reports_for_lambdas_alike_and_says_so(case, no_columns):
    for func in (case.clock_call, case.clock_ref):
        (finding,) = lint_callable(func)
        assert finding.symbol == "time.monotonic"
        assert "2 functions start on line" in finding.message
        assert "separate lines" in finding.message


def test_without_columns_a_lambda_told_apart_is_rejected_in_the_usual_words(case, no_columns):
    with pytest.raises(PermissionError) as raised:
        verify_callable_purity(case.prints)
    assert str(raised.value) == "Forbidden function call 'print' inside CPN callable."


def test_without_columns_a_lambda_that_names_more_could_still_be_its_neighbour(case, no_columns):
    # Names only rule a node out. `clocked` names everything `steady` does and more, so
    # going by names its code could have come from either, and both are read.
    assert len(cpnx.sandbox._callable_nodes(case.clocked)) == 2
    assert len(cpnx.sandbox._callable_nodes(case.steady)) == 1


def test_without_columns_clean_lambdas_on_one_line_still_pass_and_certify(case, no_columns):
    for func in (case.key_a, case.key_b):
        verify_callable_purity(func)
        assert certify(func).certified
        assert lint_callable(func) == []


_DEAD_BRANCHES = ("dead_genexp", "dead_listcomp", "dead_dictcomp", "dead_lambda")


@pytest.mark.parametrize("name", _DEAD_BRANCHES)
def test_a_nested_scope_in_a_dead_branch_does_not_rule_out_its_lambda(case, name, either_way):
    # No code object is made for a generator expression, comprehension or lambda in a branch
    # the compiler drops, so the names inside it reach no code. Counted, they ruled out the
    # impure lambda's own node, which was then judged by its clean neighbour and certified.
    func = getattr(case, name)
    with pytest.raises(PermissionError, match="Forbidden function call 'print'"):
        verify_callable_purity(func)
    assert not certify(func).certified


def test_without_columns_a_lambda_ruled_out_by_names_falls_back_to_its_whole_line(case, no_columns, monkeypatch):
    # Should the compiler drop a name some other way, no node left would use every name the
    # code does. Then every function starting on the line is read, never only the neighbour.
    real = cpnx.sandbox._lambda_names
    monkeypatch.setattr(cpnx.sandbox, "_lambda_names", lambda node: real(node) | {"never_compiled"})
    assert len(cpnx.sandbox._callable_nodes(case.prints)) == 2
    assert not _is_pure(case.prints)
    assert not certify(case.prints).certified


def test_without_columns_a_mangled_name_still_counts_as_its_own(case, no_columns):
    # In a class, `s.__x` compiles to `_Mangles__x`; the lambda that reads it is still found alone.
    assert len(cpnx.sandbox._callable_nodes(case.Mangles.reads)) == 1
    assert _is_pure(case.Mangles.reads)
    assert not _is_pure(case.Mangles.prints)


@has_columns
def test_an_interpreter_run_without_debug_ranges_takes_the_no_columns_path(case):
    # The tests above take the columns away by patching. This one asks a real interpreter
    # that was told to record none, so a change in how it reports "no column" shows up here.
    script = (
        "import importlib.util, sys\n"
        "import cpnx.sandbox as sandbox\n"
        "from cpnx.certification import certify\n"
        "spec = importlib.util.spec_from_file_location('same_line_case_child', sys.argv[1])\n"
        "case = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(case)\n"
        "assert sandbox._body_anchor(case.plain) is None, sandbox._body_anchor(case.plain)\n"
        "assert len(sandbox._callable_nodes(case.plain)) == 2\n"
        "assert not certify(case.plain).certified\n"
        "assert certify(case.clean).certified\n"
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(entry for entry in sys.path if entry)}
    child = subprocess.run(
        [sys.executable, "-X", "no_debug_ranges", "-c", script, case.__file__],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert child.returncode == 0, child.stderr


# -- the rule, on real code ----------------------------------------------------------------------


def _own_functions(owner, module_name: str):
    """The plain functions and methods defined in a module, reached through its classes too."""
    for value in vars(owner).values():
        value = getattr(value, "__func__", value)
        if inspect.isclass(value) and value.__module__ == module_name:
            yield from _own_functions(value, module_name)
        elif inspect.isfunction(value) and value.__module__ == module_name:
            yield value


def test_every_function_of_cpnx_itself_is_found_at_its_own_definition(either_way):
    # The rule rests on where the interpreter says a function's code lies and on what names
    # its code carries, which each Python version compiles a little differently (generators,
    # closures, `with`, `try`). cpnx's own few hundred functions are a sample of real code to
    # hold it to.
    found = 0
    for info in pkgutil.iter_modules(cpnx.__path__):
        module = importlib.import_module(f"cpnx.{info.name}")
        for func in _own_functions(module, module.__name__):
            try:
                nodes = cpnx.sandbox._callable_nodes(func)
            except (OSError, TypeError):  # generated by `dataclass`: no source
                continue
            if not nodes:
                # Only one the interpreter made up for a class's annotations has no node.
                assert func.__name__ == "__annotate__", func.__qualname__
                continue
            found += 1
            where = (func.__name__, func.__code__.co_firstlineno)
            located = [(getattr(node, "name", None), cpnx.sandbox._first_line(node)) for node in nodes]
            assert located == [where], func.__qualname__
    assert found > 300
