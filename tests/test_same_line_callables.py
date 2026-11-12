"""Each callable is checked against its own body, also when another one starts on its line.

The purity blocklist, the side-effect lint and inline-safety certification find a callable's
node in its source file. They used to go by the line it starts on alone, so of two lambdas
on one line both were read as the last one: an impure lambda passed the purity check and was
certified to run inline because its neighbour was clean.

Where the interpreter records columns (`code.co_positions`, Python 3.11 and later) the node is
now the one whose body holds the callable's own code. Without columns, callables that start on
one line cannot be told apart, and each is held to what any of them does.
"""

import importlib
import importlib.util
import inspect
import itertools
import pkgutil
import sys

import pytest

import cpnx.sandbox
from cpnx import InputArc
from cpnx.certification import certify
from cpnx.linting import lint_callable
from cpnx.sandbox import verify_callable_purity

_SOURCE = """\
import time

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


@pytest.fixture
def no_columns(monkeypatch):
    """Check as an interpreter without column positions does (3.10, or `-X no_debug_ranges`)."""
    monkeypatch.setattr(cpnx.sandbox, "_body_anchor", lambda func: None)
    cpnx.sandbox._SOURCE_CACHE.clear()
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
    assert _lint_symbols(case.clocked) == ["time.monotonic"]
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
def test_a_decorated_def_is_not_taken_for_a_lambda_in_its_decorator(case):
    # The def's first line is its decorator's, and the only function node on that line is
    # the lambda passed to the decorator. The def's code lies outside that lambda, so the
    # def is not located at all, which is what a decorated def without such a lambda gets.
    assert not _is_pure(case.decorated)
    verdict = certify(case.decorated)
    assert not verdict.certified
    assert verdict.reason == "source unavailable"
    assert lint_callable(case.decorated) == []


# -- without columns: callables starting on one line share one verdict ---------------------------


def test_without_columns_purity_holds_each_lambda_to_its_neighbour(case, no_columns):
    for func in (case.prints, case.clean, case.clean_first, case.prints_last):
        with pytest.raises(PermissionError, match="Forbidden function call 'print'"):
            verify_callable_purity(func)


def test_without_columns_neither_lambda_of_a_mixed_line_is_certified(case, no_columns):
    for func in (case.prints, case.clean, case.clean_first, case.prints_last):
        verdict = certify(func)
        assert not verdict.certified
        assert "2 functions start on line" in verdict.reason


def test_without_columns_lint_reports_the_line_s_findings_for_each_lambda(case, no_columns):
    assert _lint_symbols(case.clocked) == ["time.monotonic"]
    assert _lint_symbols(case.steady) == ["time.monotonic"]


def test_without_columns_clean_lambdas_on_one_line_still_pass_and_certify(case, no_columns):
    for func in (case.key_a, case.key_b):
        verify_callable_purity(func)
        assert certify(func).certified
        assert lint_callable(func) == []


def test_without_columns_a_callable_alone_on_its_line_is_found_as_before(case, no_columns):
    assert not _is_pure(case.make)  # `make` and the lambda it returns start on one line
    assert _is_pure(case.short_last)  # the only function starting on its line
    assert certify(case.short_last).certified


# -- the position rule, on real code -------------------------------------------------------------


def _own_functions(owner, module_name: str):
    """The plain functions and methods defined in a module, reached through its classes too."""
    for value in vars(owner).values():
        value = getattr(value, "__func__", value)
        if inspect.isclass(value) and value.__module__ == module_name:
            yield from _own_functions(value, module_name)
        elif inspect.isfunction(value) and value.__module__ == module_name:
            yield value


def test_every_function_of_cpnx_itself_is_found_at_its_own_definition():
    # The rule rests on where the interpreter says a function's code lies, which each Python
    # version compiles a little differently (generators, closures, `with`, `try`). cpnx's own
    # few hundred functions are a sample of real code to hold it to.
    found = 0
    for info in pkgutil.iter_modules(cpnx.__path__):
        module = importlib.import_module(f"cpnx.{info.name}")
        for func in _own_functions(module, module.__name__):
            try:
                nodes = cpnx.sandbox._source_nodes(func)
            except (OSError, TypeError):  # generated by `dataclass`: no source
                continue
            if not nodes:  # decorated, so its first line is the decorator's: not located, as before
                continue
            found += 1
            where = (func.__name__, func.__code__.co_firstlineno)
            assert [(getattr(node, "name", None), node.lineno) for node in nodes] == [where], func.__qualname__
    assert found > 300
