import ast
import inspect
import linecache
import types
from typing import Callable


class SandboxEvaluator:
    """The shared closed-world vocabulary for guard/arc-expression callables.

    Historically this class also *evaluated* string expressions in a sandbox; that
    surface was removed when callables became the only expression form. What
    remains is the whitelist itself — the fixed set of builtins and methods a
    callable may use and still certify for inline execution (see
    :mod:`cpnx.certification`, which reads these tables).
    """

    ALLOWED_BUILTINS = {
        "abs": abs,
        "round": round,
        "min": min,
        "max": max,
        "sum": sum,
        "len": len,
        "bool": bool,
        "int": int,
        "float": float,
        "str": str,
    }

    ALLOWED_METHODS = {
        "get",
        "keys",
        "values",
        "items",
        "startswith",
        "endswith",
        "lower",
        "upper",
        "split",
        "join",
    }


_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)

#: Where a callable's own code lies in its file: `(line, end_line, column, end_column)`, as
#: `code.co_positions()` gives them. Columns are UTF-8 byte offsets, as in the AST.
_Position = tuple[int, int, int, int]


def _own_code(func: Callable) -> types.CodeType | None:
    """Return the code object `func` runs, or `None` if it has none of its own."""
    func = inspect.unwrap(func)
    return getattr(getattr(func, "__func__", func), "__code__", None)


def _body_anchor(func: Callable) -> _Position | None:
    """Return the position of one instruction of `func`'s own body, or `None` if none is known.

    Nested functions compile to code objects of their own, so every positioned instruction
    of `func` lies in its body and outside the body of any function nested in it. The last
    one is taken: the leading instructions (`RESUME` and the like) carry the function's first
    line with no real span.

    Returns:
        The position, or `None` when the interpreter records no columns: before Python
        3.11, under `-X no_debug_ranges`, or for an object with no code of its own.
    """
    positions = getattr(_own_code(func), "co_positions", None)
    if positions is None:
        return None
    anchor = None
    for position in positions():
        line, end_line, column, end_column = position
        if None in position or (line, column) == (end_line, end_column):
            continue
        anchor = position
    return anchor


def _body_span(node: ast.AST) -> tuple[tuple[int, int], tuple[int, int]]:
    """Return where the body of a `def` or `lambda` node starts and ends, as `(line, column)` pairs."""
    body = node.body if isinstance(node.body, list) else [node.body]
    first, last = body[0], body[-1]
    # A decorated statement starts at its first decorator; the AST gives the `def` line.
    starts = [first, *getattr(first, "decorator_list", [])]
    start = min((item.lineno, item.col_offset) for item in starts)
    return start, (last.end_lineno, last.end_col_offset)


def _body_holds(node: ast.AST, anchor: _Position) -> bool:
    line, end_line, column, end_column = anchor
    start, end = _body_span(node)
    return start <= (line, column) and (end_line, end_column) <= end


def _node_holding(candidates: tuple[ast.AST, ...], anchor: _Position) -> tuple[ast.AST, ...]:
    """Return the candidate whose body `anchor` lies in, or nothing if it lies in none.

    Of a lambda and a lambda inside it on one line, both hold the inner one's code; its own
    node is the innermost, the one that starts last. If none holds it, the callable's code
    lies in none of the candidates: reading one of them anyway would judge some other
    function's body.
    """
    holding = [node for node in candidates if _body_holds(node, anchor)]
    return (max(holding, key=lambda node: (node.lineno, node.col_offset)),) if holding else ()


#: The expressions that compile to a code object of their own. In a branch the compiler drops
#: as unreachable (`x if 1 else (y for y in z)`, `0 and (lambda: z)`), no code object is made
#: for them, so the names inside them reach no code object at all.
_NESTED_SCOPES = (ast.Lambda, ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.DictComp)


def _names_in(node: ast.AST, *, nested: bool) -> frozenset[str]:
    """Return the variable and attribute names used under `node`, with or without nested scopes.

    Without them (`nested=False`), what lies inside a lambda, generator expression or
    comprehension below `node` is left out.
    """
    names = set()
    pending = [node]
    while pending:
        child = pending.pop()
        match child:
            case ast.Name(id=name) | ast.Attribute(attr=name):
                names.add(name)
        if nested or not isinstance(child, _NESTED_SCOPES):
            pending.extend(ast.iter_child_nodes(child))
    return frozenset(names)


def _lambda_names(node: ast.Lambda) -> frozenset[str]:
    """Return the names the body of a lambda uses in its own scope, which its code always carries.

    The compiler records each such name in the lambda's code object, also for a branch it
    then drops as unreachable. It does not for the names inside a nested scope in such a
    branch (`_NESTED_SCOPES`), so those are left out: counting them could rule out the
    lambda's own node. A name with a leading `__` is left out as well: inside a class the
    compiler stores `__x` as `_Class__x`, and `__debug__` is a constant, so neither can be
    looked for by its spelling.
    """
    return frozenset(name for name in _names_in(node.body, nested=False) if not name.startswith("__"))


def _code_names(code: types.CodeType) -> frozenset[str]:
    """Return every name `code` and the code objects nested in it refer to."""
    names = {*code.co_names, *code.co_varnames, *code.co_freevars, *code.co_cellvars}
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            names |= _code_names(const)
    return frozenset(names)


def _could_be(node: ast.AST, code: types.CodeType, code_names: frozenset[str]) -> bool:
    """Say whether `node` can be the source of `code`, going by its kind, its name and its names.

    A `def` compiles to code of its own name, and no two `def`s start on one line, so the
    name settles it. A lambda compiles to `<lambda>`, and several may share a line. Whatever
    name a lambda's body uses in its own scope (`_lambda_names`), the compiler records in its
    code object (`co_names` and the variable tables), also for a branch it then drops as
    unreachable. So a lambda whose body uses such a name that `code` does not carry is some
    other lambda. The converse does not hold: `code` may carry more names than a lambda uses
    and still not rule it out.
    """
    if isinstance(node, ast.Lambda):
        return code.co_name == "<lambda>" and _lambda_names(node) <= code_names
    return node.name == code.co_name


def _global_names(code: types.CodeType) -> frozenset[str]:
    """Return the global and attribute names (`co_names`) of `code` and the code nested in it."""
    names = set(code.co_names)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            names |= _global_names(const)
    return frozenset(names)


def _accounts_for(node: ast.AST, needed: frozenset[str]) -> bool:
    """Say whether the source under `node` uses every name in `needed`.

    Inside a class the compiler stores a name `__x` as `_Class__x`: that counts as `__x`.
    """
    names = _names_in(node, nested=True)
    return all(name in names or f"__{name.partition('__')[2]}" in names for name in needed)


def _plausible(nodes: tuple[ast.AST, ...], code: types.CodeType, starting: tuple[ast.AST, ...]) -> tuple[ast.AST, ...]:
    """Return `nodes`, or every node in `starting` if none of `nodes` can be the lambda `code` is.

    Every global and attribute name in a lambda's code comes from its own source, so its own
    node uses them all. If no node left by `_could_be` does, `_could_be` ruled out the
    lambda's own node, and the lookup falls back to every function that starts on its line.
    That is never more lenient: the lambda is one of them. A `def` is told by its name alone,
    which `_could_be` cannot get wrong.
    """
    if code.co_name != "<lambda>":
        return nodes
    needed = _global_names(code)
    if any(_accounts_for(node, needed) for node in nodes):
        return nodes
    return starting


def _first_line(node: ast.AST) -> int:
    """Return the line a function node's source starts on: its first decorator's, if it has one.

    The AST gives a decorated `def` the line of the `def` keyword, but its code object and
    `inspect.getsourcelines` start at its first decorator.
    """
    return min([node.lineno, *(decorator.lineno for decorator in getattr(node, "decorator_list", ()))])


class _ParsedSource:
    """One source file's parse, plus the function nodes found for each lookup made in it.

    A lookup is a start line and, where the interpreter records columns, the position of the
    callable's own code (`_body_anchor`): callables that share a start line are told apart by it.
    """

    __slots__ = ("source", "tree", "functions", "targets")

    def __init__(self, source: bytes | str, tree: ast.AST) -> None:
        self.source = source
        self.tree = tree
        self.functions = [node for node in ast.walk(tree) if isinstance(node, _FUNCTION_NODES)]
        self.targets: dict[tuple[int, _Position | None], tuple[ast.AST, ...]] = {}

    def locate(self, start_line: int, anchor: _Position | None) -> tuple[ast.AST, ...]:
        """Return the function nodes starting on `start_line` that `anchor` allows: all, if it is `None`."""
        key = (start_line, anchor)
        if key not in self.targets:
            starting = tuple(node for node in self.functions if _first_line(node) == start_line)
            self.targets[key] = starting if anchor is None else _node_holding(starting, anchor)
        return self.targets[key]


#: ``path -> _ParsedSource``, keyed on the file's bytes. The purity check, the linter
#: and the certifier each need the node of every guard, key and action they see, and all three
#: used to parse the whole defining file and walk its whole tree once per callable, per
#: transition built. Building nets in a loop (a test module of a few thousand lines, a
#: generated model) then cost ~0.1 s per callable. The trees are shared, so nothing may
#: mutate them: every consumer only walks its node (the certifier skips annotations as it
#: walks instead of stripping them). The file is re-read on every lookup and compared with the
#: bytes that were parsed: the read is cheap, the parse is the cost. An mtime-and-size stamp is
#: not enough, since a same-size edit within one mtime tick would then be checked against the old
#: code while the new code runs (with bytecode writing off, nothing else catches it).
_SOURCE_CACHE: dict[str, _ParsedSource] = {}

#: What "the source cannot be found or parsed" looks like: no file (``OSError``), a builtin or
#: C function (``TypeError``), unparsable source (``SyntaxError``), or undecodable bytes
#: (``UnicodeDecodeError``, a ``ValueError``).
_SOURCE_UNAVAILABLE = (OSError, TypeError, SyntaxError, ValueError)


def _read_source(path: str, func: Callable, code: types.CodeType) -> bytes | str:
    """Return the source `func` was defined in: its file's bytes, or the lines `inspect` holds for it.

    Code with no file on disk can still have source that `inspect` finds, and gives the start
    line from: a notebook cell (registered in `linecache`), a REPL entry (Python 3.13+), or a
    module loaded from a zip archive (read through its loader). Those lines are read the way
    `inspect.findsource` reads them, except that a module's loader is asked only for its own
    file: `linecache` would otherwise file another module's source under this name.
    """
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        module = inspect.getmodule(inspect.unwrap(func), path)
        lines = linecache.getlines(path, module.__dict__ if getattr(module, "__file__", None) == path else None)
        from_code = getattr(linecache, "_getlines_from_code", None)
        if not lines and path.startswith("<") and from_code is not None:
            lines = from_code(code)
        if not lines:
            raise
        return "".join(lines)


def _parsed_source(path: str, func: Callable, code: types.CodeType) -> _ParsedSource:
    source = _read_source(path, func, code)
    cached = _SOURCE_CACHE.get(path)
    if cached is None or cached.source != source:
        # Bytes where there is a file, so ast.parse honours a PEP 263 coding cookie.
        cached = _SOURCE_CACHE[path] = _ParsedSource(source, ast.parse(source, filename=path))
    return cached


def _callable_nodes(func: Callable) -> tuple[ast.AST, ...]:
    """Return the `def`/`lambda` nodes `func` may be, from a shared parse: one, when it can be told.

    The candidates are the function nodes that start on the line `func` starts on, a
    decorated `def` counting from its first decorator (`_first_line`), as its code does. So a
    decorated `def` is found as itself, at any depth: neither a lambda among its decorator's
    arguments nor the function around it is taken for it. Certification then refuses it for
    its decorator, and the purity check and the lint read its own body.

    - With column positions (`_body_anchor`), the result is the candidate that `func`'s own
      code lies in, so two lambdas on one line, a lambda and the lambda it returns, a `def`
      with a lambda default on its first line, and a `def` with a lambda in its decorator are
      each their own node.
    - Without them, the candidates that cannot be `func` are dropped (`_could_be`): a `def`
      of another name, a lambda where `func` is a `def`, and a lambda whose body uses a name
      that `func`'s code does not carry in its own scope (`_lambda_names`). Should that
      leave no node that uses every name `func`'s code does, all candidates are kept
      (`_plausible`). What is left is usually one node. Where it is
      several (lambdas on one line, none using a name the others' code lacks), they cannot be
      told apart and all are returned: `func` is one of them, so a caller that holds `func`
      to what any of them does is never more lenient than the truth.

    The nodes belong to a cached tree: read them, never mutate them.

    Args:
        func: The callable to locate.

    Returns:
        The candidate nodes; empty when `func` has no code of its own or no function node is it.

    Raises:
        OSError: If the source cannot be found or read.
        TypeError: If `func` is a builtin or C function.
        SyntaxError: If the source cannot be parsed.
        ValueError: If the source cannot be decoded.
    """
    code = _own_code(func)
    if code is None:
        return ()
    # `getsourcelines` reads the function a decorator wraps (`__wrapped__`), as `_own_code`
    # does, so the file is that function's too. A REPL entry has no source file, only lines
    # that `linecache` holds under the code's own file name.
    _, start_line = inspect.getsourcelines(func)
    path = inspect.getsourcefile(inspect.unwrap(func)) or code.co_filename
    anchor = _body_anchor(func)
    nodes = _parsed_source(path, func, code).locate(start_line, anchor)
    if anchor is not None:
        return nodes
    code_names = _code_names(code)
    return _plausible(tuple(node for node in nodes if _could_be(node, code, code_names)), code, nodes)


def _alike_note(nodes: tuple[ast.AST, ...]) -> str:
    """Return what to add to a complaint about a callable that was not told from its neighbours.

    Empty for a callable that was found alone. Otherwise the complaint may be about a
    neighbour's body, and the way out is the same either way.
    """
    if len(nodes) < 2:
        return ""
    return (
        f" ({len(nodes)} functions start on line {nodes[0].lineno} and cannot be told apart "
        "without column positions; put them on separate lines)"
    )


def _callable_nodes_or_empty(func: Callable) -> tuple[ast.AST, ...]:
    """Return `_callable_nodes(func)`, with unavailable source reported as no nodes."""
    try:
        return _callable_nodes(func)
    except _SOURCE_UNAVAILABLE:
        return ()


_FORBIDDEN_FUNCS = frozenset({"open", "print", "eval", "exec", "__import__", "sleep"})

_FORBIDDEN_ATTRS = frozenset({"sleep", "system", "popen", "urlopen"})


def _verify_function_defaults(args: ast.arguments) -> None:
    for default in args.defaults + args.kw_defaults:
        if default is not None and isinstance(default, (ast.List, ast.Dict, ast.Set)):
            raise PermissionError("Mutable default argument in CPN callable introduces hidden state between firings.")


def _verify_node_purity(node: ast.AST) -> None:
    match node:
        case ast.Call(func=ast.Name(id=name)) if name in _FORBIDDEN_FUNCS:
            raise PermissionError(f"Forbidden function call '{name}' inside CPN callable.")
        case ast.Call(func=ast.Attribute(attr=attr)) if attr in _FORBIDDEN_ATTRS:
            raise PermissionError(f"Forbidden attribute call '.{attr}' inside CPN callable.")
        case ast.Import() | ast.ImportFrom():
            raise PermissionError("Imports are forbidden inside CPN callables.")
        case ast.Global() | ast.Nonlocal():
            raise PermissionError("Global/nonlocal mutations are forbidden inside CPN callables.")
        case ast.FunctionDef(args=args) | ast.AsyncFunctionDef(args=args):
            _verify_function_defaults(args)


def _verify_ast_purity(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        _verify_node_purity(node)


def verify_callable_purity(func: Callable) -> None:
    """Verify that a callable is pure by inspecting its AST for disallowed patterns.

    This is the **hard** blocklist gate — a small set of unambiguous escapes that
    raise. For the softer, best-effort scan of side-effecting/non-deterministic
    *trouble spots* (network, database, clock, randomness) that only warns, see
    :mod:`cpnx.linting`; for the whitelist inline-safety proof, :mod:`cpnx.certification`.

    Raises :exc:`PermissionError` if the callable contains any of:

    - I/O calls: ``open``, ``print``, ``eval``, ``exec``, ``__import__``,
      ``time.sleep``, ``os.system``, ``os.popen``, ``urllib.urlopen``.
    - Import statements.
    - ``global`` or ``nonlocal`` mutations.
    - Mutable default arguments (``list``, ``dict``, or ``set`` literals as
      parameter defaults), which would introduce hidden persistent state between
      transition firings.

    It also raises when the callable's own ``def`` or ``lambda`` cannot be found in its
    source: a builtin, a ``functools.partial``, a class or other callable object, code with
    no source (``exec`` of a string, ``python -c``), or a source file edited since it was
    loaded so that no function starts on the callable's line any more. A check that cannot
    see the body passes nothing.
    """
    try:
        nodes = _callable_nodes(func)
    except Exception as exc:
        raise _source_unavailable(func) from exc
    if not nodes:
        raise _source_unavailable(func)
    try:
        for node in nodes:
            _verify_ast_purity(node)
    except PermissionError as exc:
        raise PermissionError(f"{exc}{_alike_note(nodes)}") from None


def _source_unavailable(func: Callable) -> PermissionError:
    return PermissionError(
        f"Cannot verify purity of {func!r}: source unavailable. "
        "Use a plain lambda or def-statement function, in a file or a notebook cell, instead."
    )
