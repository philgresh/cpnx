import ast
import inspect
from typing import Any, Callable


class SandboxEvaluator:
    """A safe evaluator for string-based guard/arc expressions.

    Strictly validates allowed callables using an AST allowlist.
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
    def evaluate(cls, expression_str: str, context_dict: dict[str, Any]) -> Any:
        """Parse and evaluate expression_str safely without access to dangerous builtins."""
        # 1. Parse in exec mode first to ensure safety against statement-level imports or assignments
        exec_tree = ast.parse(expression_str, mode="exec")
        for node in ast.walk(exec_tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id not in cls.ALLOWED_BUILTINS:
                        raise PermissionError(f"Forbidden call to '{node.func.id}' in sandbox.")
                elif isinstance(node.func, ast.Attribute):
                    if node.func.attr not in cls.ALLOWED_METHODS:
                        raise PermissionError(f"Forbidden call to method '{node.func.attr}' in sandbox.")
                else:
                    raise PermissionError("Forbidden complex call in sandbox.")
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                raise PermissionError("Imports are forbidden in sandbox.")
            elif isinstance(node, ast.Attribute):
                if node.attr.startswith("_"):
                    raise PermissionError(f"Access to private/dunder attribute '{node.attr}' is forbidden in sandbox.")
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                raise PermissionError("Global/nonlocal mutations are forbidden in sandbox.")

        # 2. Parse and compile in eval mode for execution
        tree = ast.parse(expression_str, mode="eval")
        code = compile(tree, "<sandbox>", "eval")
        safe_globals = {"__builtins__": cls.ALLOWED_BUILTINS}
        return eval(code, safe_globals, context_dict)


def verify_callable_purity(func: Callable) -> None:
    """Verify that a callable is pure by inspecting its AST for disallowed nodes (I/O, global mutations)."""
    # 1. Try file-level AST parsing to find the exact node (very robust for nested lambdas/functions)
    try:
        file_path = inspect.getsourcefile(func)
        lines, start_line = inspect.getsourcelines(func)
        if file_path:
            with open(file_path, "r", encoding="utf-8") as f:
                file_content = f.read()
            tree = ast.parse(file_content, filename=file_path)

            # Find the most specific (innermost) FunctionDef, AsyncFunctionDef, or Lambda
            # at start_line
            target_node = None
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    if hasattr(node, "lineno"):
                        end_lineno = getattr(node, "end_lineno", node.lineno)
                        if node.lineno <= start_line <= end_lineno:
                            if target_node is None:
                                target_node = node
                            else:
                                # Use the smaller/nested node if multiple contain start_line
                                target_start = target_node.lineno
                                target_end = getattr(target_node, "end_lineno", target_start)
                                if node.lineno >= target_start and end_lineno <= target_end:
                                    target_node = node

            if target_node is not None:
                for node in ast.walk(target_node):
                    if isinstance(node, ast.Call):
                        if isinstance(node.func, ast.Name) and node.func.id in {
                            "open",
                            "print",
                            "eval",
                            "exec",
                            "__import__",
                            "sleep",
                        }:
                            raise PermissionError(f"Forbidden function call '{node.func.id}' inside CPN callable.")
                        elif isinstance(node.func, ast.Attribute) and node.func.attr in {
                            "sleep",
                            "system",
                            "popen",
                            "urlopen",
                        }:
                            raise PermissionError(
                                f"Forbidden attribute call '.{node.func.attr}' inside CPN callable."
                            )
                    elif isinstance(node, (ast.Import, ast.ImportFrom)):
                        raise PermissionError("Imports are forbidden inside CPN callables.")
                    elif isinstance(node, (ast.Global, ast.Nonlocal)):
                        raise PermissionError("Global/nonlocal mutations are forbidden inside CPN callables.")
                return
    except PermissionError:
        raise
    except Exception:
        pass

    # 2. Fall back to parsing inspect.getsource directly with cleanups
    try:
        import textwrap

        source = textwrap.dedent(inspect.getsource(func)).strip()
        if source.endswith(","):
            source = source[:-1]
        if "=" in source and not source.startswith("def "):
            parts = source.split("=", 1)
            if not parts[0].strip().endswith((">=", "<=", "!=", "==")):
                source = parts[1].strip()

        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in {
                    "open",
                    "print",
                    "eval",
                    "exec",
                    "__import__",
                    "sleep",
                }:
                    raise PermissionError(f"Forbidden function call '{node.func.id}' inside CPN callable.")
                elif isinstance(node.func, ast.Attribute) and node.func.attr in {
                    "sleep",
                    "system",
                    "popen",
                    "get",
                    "post",
                    "request",
                    "urlopen",
                    "connect",
                }:
                    raise PermissionError(
                        f"Forbidden attribute call '.{node.func.attr}' inside CPN callable."
                    )
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                raise PermissionError("Imports are forbidden inside CPN callables.")
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                raise PermissionError("Global/nonlocal mutations are forbidden inside CPN callables.")
    except PermissionError:
        raise
    except (OSError, TypeError) as exc:
        raise PermissionError(
            f"Cannot verify purity of {func!r}: source unavailable. "
            "Use a plain lambda or def-statement function instead."
        ) from exc
    except Exception:
        # If we absolutely cannot retrieve or parse source (e.g. compiled built-in or REPL), allow with caution
        pass
