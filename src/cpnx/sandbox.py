import ast
import inspect
import os
import threading
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


def _function_nodes(tree: ast.AST) -> list[ast.AST]:
    """Every `def`, `async def` and `lambda` node of `tree`, in `ast.walk` order."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and hasattr(node, "lineno")
    ]


def _most_specific_node(nodes: list[ast.AST], start_line: int) -> ast.AST | None:
    target_node = None
    for node in nodes:
        if not _node_contains_line(node, start_line):
            continue
        if target_node is None or _is_more_specific_node(node, target_node):
            target_node = node
    return target_node


def _find_target_node(tree: ast.AST, start_line: int) -> ast.AST | None:
    return _most_specific_node(_function_nodes(tree), start_line)


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


class _ParsedSource:
    """One parsed source file: its function nodes, and the lookups made among them so far.

    Attributes:
        stamp: The file's `(st_mtime_ns, st_size)` when it was read.
        functions: Its `def`, `async def` and `lambda` nodes, from one walk of the tree.
        found: The nodes `locate` gave for each `(start_line, anchor)` asked for so far.
    """

    __slots__ = ("stamp", "functions", "found")

    def __init__(self, stamp: tuple[int, int], tree: ast.AST) -> None:
        self.stamp = stamp
        self.functions = _function_nodes(tree)
        self.found: dict[tuple[int, _Position | None], tuple[ast.AST, ...]] = {}

    def locate(self, start_line: int, anchor: _Position | None) -> tuple[ast.AST, ...]:
        """Return the nodes a callable starting on `start_line` may be; see `_source_nodes`."""
        key = (start_line, anchor)
        if key not in self.found:
            self.found[key] = self._locate(start_line, anchor)
        return self.found[key]

    def _locate(self, start_line: int, anchor: _Position | None) -> tuple[ast.AST, ...]:
        candidates = [node for node in self.functions if _node_contains_line(node, start_line)]
        if anchor is not None:
            # Every function around the callable holds the anchor too; its own node starts last.
            holding = [node for node in candidates if _body_holds(node, anchor)]
            if holding:
                return (max(holding, key=lambda node: (node.lineno, node.col_offset)),)
        starting = [node for node in candidates if node.lineno == start_line]
        if len(starting) > 1:
            return tuple(starting)
        node = _most_specific_node(candidates, start_line)
        return () if node is None else (node,)


#: Parsed source files by path. Building many arcs and transitions from the same functions
#: checks each callable again on every construction, three times over (the purity blocklist
#: here, `cpnx.linting`, `cpnx.certification`), and each check needs the callable's node. A
#: file is parsed and walked once, until its modification time or size changes. It holds
#: syntax trees only, never the callables, so it keeps no function alive; it grows by one
#: entry per source file that defines a checked callable.
_SOURCE_CACHE: dict[str, _ParsedSource] = {}
_SOURCE_CACHE_LOCK = threading.Lock()


def _source_nodes(func: Callable) -> tuple[ast.AST, ...]:
    """Return the `def`/`lambda` nodes `func` may be in its source file: one, when it can be told.

    The file is read as it is on disk now. Of the function nodes whose lines include the line
    `func` starts on:

    - With column positions (`_body_anchor`), the result is the one node whose body holds
      `func`'s own code, so two lambdas on one line, a lambda and the lambda it returns, and
      a `def` with a lambda default on its first line are each their own node.
    - Without them, or when no body holds that position (the file changed since import), the
      line is all there is. If several function nodes start on it they cannot be told apart
      and all are returned: `func` is one of them, so a caller that holds `func` to what any
      of them does is never more lenient than the truth. Otherwise the result is the most
      specific node around the line (`_find_target_node`), as it always was.

    The nodes are shared between callers and threads: read them, and copy one before
    changing it.

    Args:
        func: The callable to locate.

    Returns:
        The candidate nodes; empty when `func` has no source file or no function node spans
        its first line.

    Raises:
        OSError: If the source cannot be found or read.
        TypeError: If `func` is not an object `inspect` can locate (a builtin, a partial).
        SyntaxError: If the file does not parse.
    """
    _, start_line = inspect.getsourcelines(func)
    source_file = inspect.getsourcefile(func)
    if not source_file:
        return ()
    anchor = _body_anchor(func)
    status = os.stat(source_file)
    stamp = (status.st_mtime_ns, status.st_size)
    with _SOURCE_CACHE_LOCK:
        parsed = _SOURCE_CACHE.get(source_file)
        if parsed is None or parsed.stamp != stamp:
            with open(source_file, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=source_file)
            parsed = _SOURCE_CACHE[source_file] = _ParsedSource(stamp, tree)
        return parsed.locate(start_line, anchor)


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
        nodes = _source_nodes(func)
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
