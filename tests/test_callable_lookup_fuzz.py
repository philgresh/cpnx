"""Generated source lines: every gate finds each callable's own node and judges it by it (#90).

The purity check, the side-effect lint and certification find a callable's node in its
source. Hand-written cases (`tests/test_same_line_callables.py`,
`tests/test_purity_source_lookup.py`) pin the shapes found so far. Here Hypothesis writes
lines of several lambdas, some inside a class, some spread over lines, plus a `def` with a
lambda default on its first line. It mixes in clean and impure calls, clock reads, private
(mangled) attributes, and nested scopes (generator expressions, comprehensions, lambdas),
live or in branches the compiler drops. Each callable's own node is read off the statement
that wrote it, and its true verdicts are what the gates say when made to read that node:

- with column positions, the node found is its own alone, and every verdict is the true one;
- without them (Python 3.10, `-X no_debug_ranges`), its own node is among those found, and
  no verdict is more lenient than the true one: not pure or certified where that is not,
  and no lint finding of its own left out;
- a callable whose own body holds a forbidden call never passes the purity check or
  certifies, in either mode;
- the same source run as a notebook cell is judged as it is from a file, and run with no
  source at all it is rejected.
"""

import ast
import importlib.util
import itertools
import linecache
import sys
from contextlib import contextmanager

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import cpnx.sandbox
from cpnx.certification import certify
from cpnx.linting import lint_callable
from cpnx.sandbox import _body_anchor, _callable_nodes, verify_callable_purity

_FORBIDDEN = cpnx.sandbox._FORBIDDEN_FUNCS
_names = itertools.count()

_HEADER = "import random\nimport time\n\nCONST = 3\n\n"

# -- expressions over the parameter `b` -----------------------------------------------------------

_INNER = st.sampled_from(["w", "v", "fresh_name", "b"])
_LEAVES = st.sampled_from(
    [
        "b",
        "len(b)",
        "b.payload",
        "b.payload.get('n')",
        "CONST",
        "1",
        "0",
        "True",
        "None",
        "b.__secret",  # mangled to `_K__secret` inside class K
        "time.monotonic()",
        "random.random()",
        "print(b)",
        "open(b)",
        "time.sleep(0)",
    ]
)


def _scope(inner, body):
    return st.sampled_from(
        [
            f"({inner} for {inner} in b)",
            f"[{inner} for {inner} in b]",
            f"{{{inner} for {inner} in b}}",
            f"{{{inner}: 1 for {inner} in b}}",
            f"(lambda: {body})",
            f"(lambda {inner}: {body})",
            f"any({body} for {inner} in b)",
            f"[{body} for {inner} in b if {inner}]",
        ]
    )


def _dead(live, other):
    """`live`, with `other` in a branch the compiler drops."""
    return st.sampled_from(
        [
            f"({live} if 1 else {other})",
            f"({other} if 0 else {live})",
            f"({live} if True else {other})",
            f"({other} if None else {live})",
            f"(0 and {other} or {live})",
            f"(False and {other} or {live})",
            f"(({live}), 0 and {other})[0]",
            f"({live} if not 0 else {other})",
        ]
    )


def _expressions():
    def extend(children):
        pair = st.tuples(children, children)
        return st.one_of(
            pair.map(lambda p: f"({p[0]} or {p[1]})"),
            pair.map(lambda p: f"({p[0]}, {p[1]})"),
            pair.flatmap(lambda p: _dead(*p)),
            st.tuples(_INNER, children).flatmap(lambda p: _scope(*p)),
        )

    return st.recursive(_LEAVES, extend, max_leaves=6)


# -- source lines -----------------------------------------------------------------------------------


@st.composite
def _modules(draw):
    """Return `(source, names)`: a module of lambdas sharing lines, and where to find each one.

    Each name is `(owner, attribute)`: `owner` is `None` for a module global, `"K"` for an
    attribute of class `K`, `"fd"` for the default of the function `fd`, and `"made"` for
    the lambda that the lambda `m` returns.
    """
    in_class = draw(st.booleans())
    count = draw(st.integers(2, 3))
    bodies = [draw(_expressions()) for _ in range(count)]
    split = [draw(st.booleans()) for _ in range(count)]
    breaks = ["\n        " if cut else " " for cut in split]
    lambdas = [f"(lambda b:{gap}{body})" for gap, body in zip(breaks, bodies, strict=True)]
    targets = [f"f{i}" for i in range(count)]
    maker = draw(st.booleans())
    if maker:
        # A lambda and the lambda it returns start on one line, beside the others.
        lambdas.append(f"(lambda k: (lambda b: {draw(_expressions())}))")
        targets.append("m")
    line = f"{', '.join(targets)} = {', '.join(lambdas)}\n"
    owner = "K" if in_class else None
    if in_class:
        line = "class K:\n    " + line.replace("\n        ", "\n            ")
    source = _HEADER + line
    names = [(owner, target) for target in targets]
    if maker:
        names.append(("made", "m" if owner is None else "K.m"))
    if draw(st.booleans()):
        default, result = draw(_expressions()), draw(_expressions())
        source += f"def fd(b, k=lambda b: {default}):\n    return {result}\n"
        names += [("fd", "k"), (None, "fd")]
    return source, names


def _own_nodes(source):
    """Return the node each callable of `source` was compiled from, by where it is written.

    This is read off the statements, not looked up by line: the `i`-th lambda of a tuple
    assignment is `f<i>`, and `fd`'s one default is its `k`.
    """
    own = {}

    def visit(statements, owner):
        for statement in statements:
            match statement:
                case ast.Assign(targets=[ast.Tuple(elts=targets)], value=ast.Tuple(elts=values)):
                    own.update({(owner, target.id): value for target, value in zip(targets, values, strict=True)})
                case ast.ClassDef(name=name, body=body):
                    visit(body, name)
                case ast.FunctionDef(name=name, args=args):
                    own[(owner, name)] = statement
                    own[(name, "k")] = args.defaults[0]

    visit(ast.parse(source).body, None)
    for (owner, name), node in list(own.items()):
        if name == "m":
            own[("made", name if owner is None else f"{owner}.{name}")] = node.body
    return own


def _load(directory, source):
    name = f"lookup_fuzz_{next(_names)}"
    path = directory / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    linecache.checkcache(str(path))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def _get(namespace, where):
    owner, attribute = where
    get = namespace.get if isinstance(namespace, dict) else lambda key: getattr(namespace, key)
    if owner is None:
        return get(attribute)
    if owner == "fd":
        return get("fd").__defaults__[0]
    if owner == "made":
        return _get(namespace, tuple(attribute.split(".")) if "." in attribute else (None, attribute))(0)
    return getattr(get(owner), attribute)


@contextmanager
def _columns(present):
    real = cpnx.sandbox._body_anchor
    if not present:
        cpnx.sandbox._body_anchor = lambda func: None
    cpnx.sandbox._SOURCE_CACHE.clear()
    try:
        yield
    finally:
        cpnx.sandbox._body_anchor = real
        cpnx.sandbox._SOURCE_CACHE.clear()


def _is_pure(func):
    try:
        verify_callable_purity(func)
    except PermissionError:
        return False
    return True


def _verdicts(func):
    return _is_pure(func), certify(func).certified, sorted({finding.symbol for finding in lint_callable(func)})


def _forbidden_inside(node):
    return any(
        isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id in _FORBIDDEN
        for child in ast.walk(node)
    )


@contextmanager
def _found_as(node):
    """Make every gate find `node` and nothing else, whatever callable it is asked about."""
    real = cpnx.sandbox._callable_nodes
    cpnx.sandbox._callable_nodes = lambda func: (node,)
    try:
        yield
    finally:
        cpnx.sandbox._callable_nodes = real


def _truth(func, node):
    """Return the verdicts `func` gets when the gates read its own node."""
    with _found_as(node):
        return _verdicts(func)


def _place(node):
    return node.lineno, node.col_offset


_SETTINGS = settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@pytest.fixture(scope="module")
def directory(tmp_path_factory):
    return tmp_path_factory.mktemp("lookup_fuzz")


@_SETTINGS
@given(module=_modules(), columns=st.booleans())
def test_each_callable_is_found_and_judged_as_itself(directory, module, columns):
    source, names = module
    shared, own = _load(directory, source), _own_nodes(source)
    with _columns(columns):
        for where in names:
            func, node = _get(shared, where), own[where]
            found = _callable_nodes(func)
            here, truth = _verdicts(func), _truth(func, node)
            assert _place(node) in [_place(candidate) for candidate in found], (where, source)
            if columns and _body_anchor(func) is not None:
                assert len(found) == 1, (where, source)
                assert here == truth, (where, source)
            else:
                # Several candidates: never more lenient than the callable's own node.
                pure, certified, symbols = here
                assert pure <= truth[0], (where, source)
                assert certified <= truth[1], (where, source)
                assert set(truth[2]) <= set(symbols), (where, source)
            if _forbidden_inside(node):
                assert not here[0], (where, source)
                assert not here[1], (where, source)


@_SETTINGS
@given(module=_modules(), columns=st.booleans())
def test_a_notebook_cell_is_judged_as_its_file_and_no_source_is_rejected(directory, module, columns):
    source, names = module
    shared = _load(directory, source)
    filename = str(directory / "kernel" / f"cell_{next(_names)}.py")  # never written to disk
    linecache.cache[filename] = (len(source), None, source.splitlines(True), filename)
    cell = {"__name__": "__main__"}
    exec(compile(source, filename, "exec"), cell)  # noqa: S102 - a notebook cell is exec'd
    sourceless = {"__name__": "__main__"}
    exec(compile(source, "<string>", "exec"), sourceless)  # noqa: S102 - code with no source
    try:
        with _columns(columns):
            for where in names:
                assert _verdicts(_get(cell, where)) == _verdicts(_get(shared, where)), (where, source)
                orphan = _get(sourceless, where)
                with pytest.raises(PermissionError, match="source unavailable"):
                    verify_callable_purity(orphan)
                assert not certify(orphan).certified
    finally:
        linecache.cache.pop(filename, None)
