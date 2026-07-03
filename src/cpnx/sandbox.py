import ast
import functools
import inspect
from typing import Any, Callable


class SandboxEvaluator:
    """A safe evaluator for string-based guard/arc expressions.

    Validates expressions via static AST analysis before execution:

    - Only whitelisted built-in functions and methods are permitted.
    - All iteration constructs are forbidden (``while``, ``for``, list/dict/set
      comprehensions, generator expressions) to prevent lock-hogging inside the
      engine's critical section.
    - Imports, ``global``/``nonlocal`` statements, and private/dunder attribute
      access are blocked.
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

    @classmethod
    def compile_expression(cls, expression_str: str):
        """Validate and compile *expression_str*, returning an ``eval``-mode code object.

        Runs the static AST security walk (imports, forbidden calls, private attribute
        access, unbounded iteration) before compiling. Raises :exc:`PermissionError` on
        any violation. Results are cached by source text, so an identical expression is
        parsed and compiled at most once -- callers may invoke this in a hot loop.
        """
        return _compile_cached(expression_str)

    @classmethod
    def maybe_compile(cls, value):
        """Compile *value* if it is a string expression, else return ``None``.

        Strings are validated and compiled via :meth:`compile_expression` (cached); any
        other value (a callable or ``None``) yields ``None``. Centralizes the
        string-vs-callable rule so arc/guard constructors don't each re-implement it.
        """
        return cls.compile_expression(value) if isinstance(value, str) else None

    @classmethod
    def evaluate_compiled(cls, compiled_code, context_dict: dict[str, Any]) -> Any:
        """Evaluate a pre-compiled code object (from :meth:`compile_expression`) safely."""
        safe_globals = {"__builtins__": cls.ALLOWED_BUILTINS}
        return eval(compiled_code, safe_globals, context_dict)

    @classmethod
    def evaluate(cls, expression_str: str, context_dict: dict[str, Any]) -> Any:
        """Parse and evaluate expression_str safely without access to dangerous builtins.

        Thin wrapper over :meth:`compile_expression` + :meth:`evaluate_compiled`; the
        compilation step is cached, so repeated calls with the same expression skip
        re-parsing and re-compiling.
        """
        return cls.evaluate_compiled(cls.compile_expression(expression_str), context_dict)


def _check_expression_node_call(node: ast.Call) -> None:
    if isinstance(node.func, ast.Name):
        if node.func.id not in SandboxEvaluator.ALLOWED_BUILTINS:
            raise PermissionError(f"Forbidden call to '{node.func.id}' in sandbox.")
    elif isinstance(node.func, ast.Attribute):
        if node.func.attr not in SandboxEvaluator.ALLOWED_METHODS:
            raise PermissionError(f"Forbidden call to method '{node.func.attr}' in sandbox.")
    else:
        raise PermissionError("Forbidden complex call in sandbox.")


def _check_expression_node(node: ast.AST) -> None:
    match node:
        case ast.Call():
            _check_expression_node_call(node)
        case ast.Import() | ast.ImportFrom():
            raise PermissionError("Imports are forbidden in sandbox.")
        case ast.Attribute(attr=attr) if attr.startswith("_"):
            raise PermissionError(f"Access to private/dunder attribute '{attr}' is forbidden in sandbox.")
        case ast.Global() | ast.Nonlocal():
            raise PermissionError("Global/nonlocal mutations are forbidden in sandbox.")
        case (
            ast.While()
            | ast.For()
            | ast.AsyncFor()
            | ast.ListComp()
            | ast.DictComp()
            | ast.SetComp()
            | ast.GeneratorExp()
        ):
            raise PermissionError("Unbounded iteration is forbidden in sandbox expressions.")


@functools.lru_cache(maxsize=256)
def _compile_cached(expression_str: str):
    """Validate (exec-mode security walk) and compile (eval-mode) an expression once.

    Cached by source text. Kept module-level so the cache is shared process-wide across
    all transitions/arcs that reference the same expression string.
    """
    # 1. Parse in exec mode first to ensure safety against statement-level imports or assignments
    exec_tree = ast.parse(expression_str, mode="exec")
    for node in ast.walk(exec_tree):
        _check_expression_node(node)

    # 2. Parse and compile in eval mode for execution
    tree = ast.parse(expression_str, mode="eval")
    return compile(tree, "<sandbox>", "eval")


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


_FORBIDDEN_FUNCS = frozenset({"open", "print", "eval", "exec", "__import__", "sleep"})
_FORBIDDEN_ATTRS = frozenset({"sleep", "system", "popen", "urlopen"})
_FALLBACK_FORBIDDEN_ATTRS = frozenset({"sleep", "system", "popen", "urlopen", "get", "post", "request", "connect"})


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
        file_path = inspect.getsourcefile(func)
        lines, start_line = inspect.getsourcelines(func)
        if file_path:
            with open(file_path, "r", encoding="utf-8") as f:
                file_content = f.read()
            tree = ast.parse(file_content, filename=file_path)

            target_node = _find_target_node(tree, start_line)
            if target_node is not None:
                _verify_ast_purity(target_node)
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
        if not parts[0].strip().endswith((">=", "<=", "!=", "==")):
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
        pass


def verify_callable_purity(func: Callable) -> None:
    """Verify that a callable is pure by inspecting its AST for disallowed patterns.

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
