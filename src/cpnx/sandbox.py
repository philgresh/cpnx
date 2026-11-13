import ast
import inspect
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


def _node_contains_line(node: ast.AST, start_line: int) -> bool:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        return False
    if not hasattr(node, "lineno"):
        return False
    end_lineno = getattr(node, "end_lineno", node.lineno)
    return node.lineno <= start_line <= end_lineno


def _is_more_specific_node(new_node: ast.AST, current_target: ast.AST) -> bool:
    target_start = current_target.lineno
    target_end = getattr(current_target, "end_lineno", target_start)
    new_end = getattr(new_node, "end_lineno", new_node.lineno)
    return new_node.lineno >= target_start and new_end <= target_end


def _find_target_node(tree: ast.AST, start_line: int) -> ast.AST | None:
    target_node = None
    for node in ast.walk(tree):
        if not _node_contains_line(node, start_line):
            continue
        if target_node is None or _is_more_specific_node(node, target_node):
            target_node = node
    return target_node


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


def _lambda_names(node: ast.Lambda) -> frozenset[str]:
    """Return the variable and attribute names the body of a lambda uses, nested lambdas included.

    A name with a leading `__` is left out: inside a class the compiler stores `__x` as
    `_Class__x`, and `__debug__` is a constant, so neither can be looked for by its spelling.
    """
    names = set()
    for child in ast.walk(node.body):
        match child:
            case ast.Name(id=name) | ast.Attribute(attr=name) if not name.startswith("__"):
                names.add(name)
    return frozenset(names)


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
    name a lambda's body uses, the compiler records in its code object or in one nested in
    it (`co_names` and the variable tables), also for a branch it then drops as unreachable.
    So a lambda whose body uses a name that `code` does not carry is some other lambda. The
    converse does not hold: `code` may carry more names than a lambda uses and still not
    rule it out.
    """
    if isinstance(node, ast.Lambda):
        return code.co_name == "<lambda>" and _lambda_names(node) <= code_names
    return node.name == code.co_name


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

    def __init__(self, source: bytes, tree: ast.AST) -> None:
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


def _parsed_source(path: str) -> _ParsedSource:
    with open(path, "rb") as handle:
        source = handle.read()
    cached = _SOURCE_CACHE.get(path)
    if cached is None or cached.source != source:
        # Bytes, so ast.parse honours a PEP 263 coding cookie.
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
      that `func`'s code does not carry. What is left is usually one node. Where it is
      several (lambdas on one line, none using a name the others' code lacks), they cannot be
      told apart and all are returned: `func` is one of them, so a caller that holds `func`
      to what any of them does is never more lenient than the truth.

    The nodes belong to a cached tree: read them, never mutate them.

    Args:
        func: The callable to locate.

    Returns:
        The candidate nodes; empty when `func` has no source file or no function node is it.

    Raises:
        OSError: If the source cannot be found or read.
        TypeError: If `func` is a builtin or C function.
        SyntaxError: If the source cannot be parsed.
        ValueError: If the source cannot be decoded.
    """
    path = inspect.getsourcefile(func)
    _, start_line = inspect.getsourcelines(func)
    code = _own_code(func)
    if not path or code is None:
        return ()
    anchor = _body_anchor(func)
    nodes = _parsed_source(path).locate(start_line, anchor)
    if anchor is not None:
        return nodes
    code_names = _code_names(code)
    return tuple(node for node in nodes if _could_be(node, code, code_names))


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

# Two deliberately different attribute denylists, keyed on how precisely we could
# locate the callable's source:
#   * _FORBIDDEN_ATTRS — used when we parsed the whole source file and isolated the
#     exact function/lambda node (_try_verify_via_sourcefile). Precise context, so a
#     tight denylist suffices.
#   * _FALLBACK_FORBIDDEN_ATTRS — used when we could only recover a mangled source
#     snippet via inspect.getsource (_try_verify_via_getsource). Less certainty about
#     what we're looking at, so we additionally ban common network I/O method names
#     (get/post/request/connect). Keep this a SUPERSET of _FORBIDDEN_ATTRS; do not
#     collapse the two — the fallback is intentionally stricter.
_FORBIDDEN_ATTRS = frozenset({"sleep", "system", "popen", "urlopen"})
_FALLBACK_FORBIDDEN_ATTRS = _FORBIDDEN_ATTRS | frozenset({"get", "post", "request", "connect"})


def _verify_function_defaults(args: ast.arguments) -> None:
    for default in args.defaults + args.kw_defaults:
        if default is not None and isinstance(default, (ast.List, ast.Dict, ast.Set)):
            raise PermissionError("Mutable default argument in CPN callable introduces hidden state between firings.")


def _verify_node_purity(node: ast.AST, forbidden_attrs: frozenset[str]) -> None:
    match node:
        case ast.Call(func=ast.Name(id=name)) if name in _FORBIDDEN_FUNCS:
            raise PermissionError(f"Forbidden function call '{name}' inside CPN callable.")
        case ast.Call(func=ast.Attribute(attr=attr)) if attr in forbidden_attrs:
            raise PermissionError(f"Forbidden attribute call '.{attr}' inside CPN callable.")
        case ast.Import() | ast.ImportFrom():
            raise PermissionError("Imports are forbidden inside CPN callables.")
        case ast.Global() | ast.Nonlocal():
            raise PermissionError("Global/nonlocal mutations are forbidden inside CPN callables.")
        case ast.FunctionDef(args=args) | ast.AsyncFunctionDef(args=args):
            _verify_function_defaults(args)


def _verify_ast_purity(tree: ast.AST, is_fallback: bool = False) -> None:
    forbidden_attrs = _FALLBACK_FORBIDDEN_ATTRS if is_fallback else _FORBIDDEN_ATTRS
    for node in ast.walk(tree):
        _verify_node_purity(node, forbidden_attrs)


def _try_verify_via_sourcefile(func: Callable) -> bool:
    nodes: tuple[ast.AST, ...] = ()
    try:
        nodes = _callable_nodes(func)
        for node in nodes:
            _verify_ast_purity(node)
        if nodes:
            return True
    except PermissionError as exc:
        note = _alike_note(nodes)
        if note:
            raise PermissionError(f"{exc}{note}") from None
        raise
    except Exception:
        pass
    return False


def _clean_fallback_source(source: str) -> str:
    if source.endswith(","):
        source = source[:-1]
    if "=" in source and not source.startswith("def "):
        parts = source.split("=", 1)
        # After splitting on the first "=", the comparison-operator char (>, <, !)
        # is the last char of parts[0].  A bare "=" (for "==") also stays.
        if not parts[0].rstrip().endswith((">", "<", "!", "=")):
            source = parts[1].strip()
    return source


def _try_verify_via_getsource(func: Callable) -> None:
    try:
        import textwrap

        source = textwrap.dedent(inspect.getsource(func)).strip()
        source = _clean_fallback_source(source)
        tree = ast.parse(source)
        _verify_ast_purity(tree, is_fallback=True)
    except PermissionError:
        raise
    except (OSError, TypeError) as exc:
        raise PermissionError(
            f"Cannot verify purity of {func!r}: source unavailable. "
            "Use a plain lambda or def-statement function instead."
        ) from exc
    except Exception:
        # Source cannot be retrieved or parsed (e.g. compiled built-in, REPL lambda) — allow with caution.
        pass


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
    """
    if _try_verify_via_sourcefile(func):
        return
    _try_verify_via_getsource(func)
