import ast
import inspect
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


def _nodes_spanning(tree: ast.AST, start_line: int) -> list[ast.AST]:
    """Return the `def`, `async def` and `lambda` nodes whose lines include `start_line`."""
    return [node for node in ast.walk(tree) if _node_contains_line(node, start_line)]


def _most_specific_node(candidates: list[ast.AST]) -> ast.AST | None:
    target_node = None
    for node in candidates:
        if target_node is None or _is_more_specific_node(node, target_node):
            target_node = node
    return target_node


def _find_target_node(tree: ast.AST, start_line: int) -> ast.AST | None:
    return _most_specific_node(_nodes_spanning(tree, start_line))


#: Where a callable's own code lies in its file: `(line, end_line, column, end_column)`, as
#: `code.co_positions()` gives them. Columns are UTF-8 byte offsets, as in the AST.
_Position = tuple[int, int, int, int]


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
    func = inspect.unwrap(func)
    code = getattr(getattr(func, "__func__", func), "__code__", None)
    positions = getattr(code, "co_positions", None)
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


def _node_holding(candidates: list[ast.AST], anchor: _Position) -> tuple[ast.AST, ...]:
    """Return the candidate whose body holds `anchor`, innermost first, or nothing if none does.

    Every function around a callable holds its code too; its own node is the one that starts
    last. If none holds it, the callable's code lies in none of the candidates: reading one
    of them anyway would judge some other function's body.
    """
    holding = [node for node in candidates if _body_holds(node, anchor)]
    return (max(holding, key=lambda node: (node.lineno, node.col_offset)),) if holding else ()


def _nodes_by_line(candidates: list[ast.AST], start_line: int) -> tuple[ast.AST, ...]:
    """Return what the line alone tells: every candidate starting on it if several do, else the most specific."""
    starting = [node for node in candidates if node.lineno == start_line]
    if len(starting) > 1:
        return tuple(starting)
    node = _most_specific_node(candidates)
    return () if node is None else (node,)


class _ParsedSource:
    """One source file's parse, plus the callable nodes found for each lookup made in it.

    A lookup is a start line and, where the interpreter records columns, the position of the
    callable's own code (`_body_anchor`): callables that share a start line are told apart by it.
    """

    __slots__ = ("source", "tree", "targets")

    def __init__(self, source: bytes, tree: ast.AST) -> None:
        self.source = source
        self.tree = tree
        self.targets: dict[tuple[int, _Position | None], tuple[ast.AST, ...]] = {}

    def locate(self, start_line: int, anchor: _Position | None) -> tuple[ast.AST, ...]:
        """Return the nodes a callable starting on `start_line` may be; see `_callable_nodes`."""
        key = (start_line, anchor)
        if key not in self.targets:
            candidates = _nodes_spanning(self.tree, start_line)
            if anchor is None:
                self.targets[key] = _nodes_by_line(candidates, start_line)
            else:
                self.targets[key] = _node_holding(candidates, anchor)
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

    Of the function nodes whose lines include the line `func` starts on:

    - With column positions (`_body_anchor`), the result is the one node whose body holds
      `func`'s own code, so two lambdas on one line, a lambda and the lambda it returns, and
      a `def` with a lambda default on its first line are each their own node. If no body
      holds it, there is no result: `func` is none of these nodes. That is a decorated `def`
      outside any function, whose first line is its decorator's (a lambda among the
      decorator's arguments is not it), or a file changed since import.
    - Without them the line is all there is. If several function nodes start on it they
      cannot be told apart and all are returned: `func` is one of them, so a caller that
      holds `func` to what any of them does is never more lenient than the truth. Otherwise
      the result is the most specific node around the line (`_find_target_node`), as it
      always was.

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
    if not path:
        return ()
    return _parsed_source(path).locate(start_line, _body_anchor(func))


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
    try:
        nodes = _callable_nodes(func)
        for node in nodes:
            _verify_ast_purity(node)
        if nodes:
            return True
    except PermissionError:
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
