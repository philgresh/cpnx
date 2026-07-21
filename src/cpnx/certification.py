"""Closed-world certification for callable guards and arc expressions.

The engine can run a callable guard/expression one of two ways:

* **inline** — called directly, under the engine lock, with **no timeout**; or
* **sandboxed** — dispatched to a ``ThreadPoolExecutor`` and bounded by
  ``expr_timeout_secs`` (see ``engine._call_expr``).

The executor round-trip costs ~90x the inline call, so we want the inline path
for anything we can *prove* is safe. This module supplies that proof.

"Safe" here means **closed-world and provably terminating**: the callable draws
only on a fixed, library-controlled vocabulary (a small whitelist of builtins
and methods), calls only user helpers that themselves certify, iterates only
over its own (finite, engine-built) argument, and closes over nothing mutable.
A callable meeting all of these has **its own control flow bounded and
closed-world** — so running it inline without a timeout is sound. One residual
assumption remains, and is *not* proven here: whitelisted method calls
(``.get``, ``.startswith``, …) and every comparison (``==``, ``<``, and the
``sorted(..., key=...)`` comparisons) dispatch on arbitrary user-supplied token
*payload* objects, so termination still assumes those payload methods and
comparisons themselves terminate and are side-effect-free. This is the same
assumption the string sandbox always made (it, too, ran ``.get``/``==`` inline
over payloads), so certification adds no risk relative to strings — but a
payload whose ``__eq__``/``__lt__``/``.get`` diverges would do so inline, under
the lock, with no timeout.

This is deliberately a **whitelist** (closed-world), not the effect-detecting
blocklist in :func:`cpnx.sandbox.verify_callable_purity`. Side effects cannot be
reliably prevented in Python (see the module docstring of ``sandbox``); the
timeout the executor provides bounds *duration*, not *effects*. Certification
therefore does not try to prove purity — it proves the callable is built solely
from a vocabulary that has no way to escape or to diverge. Anything it cannot
prove, it rejects; a rejected callable simply keeps running via the executor,
exactly as before. Certification never changes *whether* a callable is allowed,
only *how* it runs.

Public API: :func:`certify` returns a :class:`Verdict`; :func:`is_inline_safe`
is the boolean shortcut. Neither raises for an un-certifiable callable — an
un-verifiable callable is reported as ``certified=False`` with a reason.

**Known limitation — late binding.** Python resolves global and free names at
*call* time. A certified callable that references a helper by name can, in
principle, have that name rebound after construction (e.g. ``unittest.mock``)
to point at uncertified code. AST analysis at construction time cannot see this.
Rebinding anything a certified callable references is therefore **undefined
behaviour** — symmetric with the closure-cell immutability rule below and with
a compiled string expression, both of which are frozen at construction.
"""

import ast
import inspect
from collections.abc import Callable
from dataclasses import dataclass

from .sandbox import SandboxEvaluator, _find_target_node

#: Builtins that iterate/reduce a finite iterable. Added on top of the string
#: sandbox's arithmetic/coercion set because the real guard corpus needs them
#: (``sorted``/``any``/``all`` over a token list). Each is O(n) over a finite,
#: engine-built argument, so it preserves the termination proof.
_ITERATION_BUILTINS = frozenset({"sorted", "next", "any", "all"})

#: Names permitted in call position when the callee is a bare ``Name``.
_ALLOWED_CALL_NAMES = frozenset(SandboxEvaluator.ALLOWED_BUILTINS) | _ITERATION_BUILTINS

#: Immutable leaf types a closure cell (free variable) may hold. ``bool`` is a
#: subclass of ``int`` and needs no separate entry. Tuples/frozensets are checked
#: recursively (see :func:`_is_immutable`).
_IMMUTABLE_LEAVES = (int, float, str, bytes, bool, type(None), frozenset)


@dataclass(frozen=True)
class Verdict:
    """Outcome of certifying a callable.

    ``certified`` is the decision; ``reason`` is empty when certified and
    otherwise a short human-readable explanation of the first disqualifying
    property found (useful in warnings and tests).
    """

    certified: bool
    reason: str = ""


def certify(func: Callable) -> Verdict:
    """Return a :class:`Verdict` on whether *func* is safe to run inline.

    Never raises for an un-certifiable callable; an unverifiable one yields
    ``Verdict(False, <reason>)``. A fresh memo/cycle-stack is used per top-level
    call, so the result reflects the current call graph and closure state.
    """
    if not callable(func):
        return Verdict(False, "not callable")
    return _Certifier().certify(func, frozenset())


def is_inline_safe(func: Callable) -> bool:
    """Boolean shortcut for :func:`certify`."""
    return certify(func).certified


def _is_immutable(value: object) -> bool:
    """True if *value* is deeply immutable (safe to close over)."""
    if isinstance(value, _IMMUTABLE_LEAVES):
        return True
    if isinstance(value, tuple):
        return all(_is_immutable(item) for item in value)
    return False


def _get_ast_node(func: Callable) -> ast.AST | None:
    """Recover the ``Lambda``/``FunctionDef`` AST node for *func*, or ``None``.

    Returns ``None`` when the source cannot be located (compiled builtins, REPL
    lambdas, ``exec``-created code) — which certification treats as *not
    certified*, never as *allowed*.
    """
    try:
        _, start_line = inspect.getsourcelines(func)
        source_file = inspect.getsourcefile(func)
        if not source_file:
            return None
        with open(source_file, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=source_file)
    except (OSError, TypeError, SyntaxError):
        return None
    return _find_target_node(tree, start_line)


def _param_names(node: ast.AST) -> frozenset[str]:
    """Parameter names bound by a lambda/def node (all positional/kw forms)."""
    args = getattr(node, "args", None)
    if not isinstance(args, ast.arguments):
        return frozenset()
    collected = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg:
        collected.append(args.vararg)
    if args.kwarg:
        collected.append(args.kwarg)
    return frozenset(arg.arg for arg in collected)


def _root_name(expr: ast.AST) -> str | None:
    """Root ``Name`` of a subscript/attribute chain, e.g. ``toks[0].x`` -> ``toks``."""
    while isinstance(expr, (ast.Subscript, ast.Attribute)):
        expr = expr.value
    return expr.id if isinstance(expr, ast.Name) else None


def _resolve_name(func: Callable, name: str) -> object:
    """Resolve *name* as *func* would at call time: free vars first, then globals.

    Returns the sentinel :data:`_UNRESOLVED` if the name is neither a closure
    cell nor a module global (e.g. a builtin, or a genuinely undefined name).
    """
    code = func.__code__
    if name in code.co_freevars and func.__closure__ is not None:
        cell = func.__closure__[code.co_freevars.index(name)]
        try:
            return cell.cell_contents
        except ValueError:  # empty cell (recursive def not yet bound)
            return _UNRESOLVED
    globals_ = getattr(func, "__globals__", {})
    return globals_.get(name, _UNRESOLVED)


_UNRESOLVED = object()


class _Certifier:
    """One certification run: owns the memo and the in-progress cycle stack.

    A single instance certifies one top-level callable plus, transitively, every
    user helper it calls. Memoising by function object keeps a diamond-shaped
    call graph linear; the cycle stack turns any recursion (direct or mutual)
    into a clean rejection, which preserves the termination guarantee.
    """

    def __init__(self) -> None:
        self._memo: dict[Callable, Verdict] = {}

    def certify(self, func: Callable, stack: frozenset[Callable]) -> Verdict:
        if func in stack:
            return Verdict(False, "cyclic call graph")
        if func in self._memo:
            return self._memo[func]
        verdict = self._certify_uncached(func, stack | {func})
        self._memo[func] = verdict
        return verdict

    def _certify_uncached(self, func: Callable, stack: frozenset[Callable]) -> Verdict:
        if getattr(func, "__wrapped__", None) is not None:
            return Verdict(False, "decorated callable (wrapper hides certified body)")
        node = _get_ast_node(func)
        if node is None:
            return Verdict(False, "source unavailable")
        if isinstance(node, ast.AsyncFunctionDef):
            return Verdict(False, "async callable")
        if getattr(node, "decorator_list", None):
            return Verdict(False, "decorated callable (wrapper hides certified body)")
        return self._check_body(func, node, stack)

    def _check_body(self, func: Callable, node: ast.AST, stack: frozenset[Callable]) -> Verdict:
        """Walk every node under *node* and reject the first disqualifying one.

        ``params`` bounds iteration; ``bound`` (params + everything assigned or
        captured as a target anywhere in the body) is the set of names a ``Name``
        read need not resolve — anything outside it is external state and must be
        immutable or a certifying helper (see :meth:`_check_name`). This unified
        read check covers both closure cells and module globals.
        """
        _strip_annotations(node)
        params = _param_names(node)
        bound = params | _bound_names(node)
        for child in ast.walk(node):
            verdict = self._check_node(child, func, params, bound, stack)
            if not verdict.certified:
                return verdict
        return Verdict(True)

    def _check_node(
        self,
        node: ast.AST,
        func: Callable,
        params: frozenset[str],
        bound: frozenset[str],
        stack: frozenset[Callable],
    ) -> Verdict:
        structural = _check_structural(node)
        if structural is not None:
            return structural
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            return self._check_comprehension(node, params)
        if isinstance(node, ast.Call):
            return self._check_call(node, func, stack)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            return self._check_name(node, func, bound, stack)
        return Verdict(True)

    def _check_name(
        self, node: ast.Name, func: Callable, bound: frozenset[str], stack: frozenset[Callable]
    ) -> Verdict:
        """A free/global name read must resolve to an immutable value or a certifying helper.

        Locally bound names pass unconditionally. Unresolved names pass too: they
        are builtins (safe) or would raise ``NameError`` at call time (which
        terminates) — neither escapes the closed world. Only a *resolved* mutable
        external value is a problem: that is a guard closing over live state.
        """
        if node.id in bound:
            return Verdict(True)
        value = _resolve_name(func, node.id)
        if value is _UNRESOLVED or _is_immutable(value):
            return Verdict(True)
        if callable(value) and self.certify(value, stack).certified:
            return Verdict(True)
        return Verdict(False, f"reads mutable/uncertified external '{node.id}'")

    @staticmethod
    def _check_comprehension(node: ast.AST, params: frozenset[str]) -> Verdict:
        """Each generator must iterate something rooted at a parameter or an in-comp target.

        This is what bounds iteration: the only finite iterables we admit are the
        engine-built argument(s) and things derived from them within the same
        comprehension. Iterating anything else is rejected.
        """
        targets = {name for gen in node.generators for name in _comp_target_names(gen.target)}
        allowed_roots = params | targets
        for generator in node.generators:
            root = _root_name(generator.iter)
            if root not in allowed_roots:
                return Verdict(False, "iteration not bounded by the argument")
        return Verdict(True)

    def _check_call(self, node: ast.Call, func: Callable, stack: frozenset[Callable]) -> Verdict:
        """A call is allowed to a whitelisted builtin/method, or a certifying helper."""
        callee = node.func
        if isinstance(callee, ast.Attribute):
            if callee.attr in SandboxEvaluator.ALLOWED_METHODS:
                return Verdict(True)
            return Verdict(False, f"method call '.{callee.attr}' not whitelisted")
        if not isinstance(callee, ast.Name):
            return Verdict(False, "non-name call (needs runtime resolution)")
        if callee.id in _ALLOWED_CALL_NAMES:
            return Verdict(True)
        return self._check_user_call(callee.id, func, stack)

    def _check_user_call(self, name: str, func: Callable, stack: frozenset[Callable]) -> Verdict:
        """Transitively certify a call to a user function resolved from *func*'s scope."""
        target = _resolve_name(func, name)
        if target is _UNRESOLVED:
            return Verdict(False, f"call to unresolved name '{name}'")
        if not (inspect.isfunction(target) or inspect.ismethod(target)):
            return Verdict(False, f"call to non-function '{name}'")
        inner = self.certify(target, stack)
        if inner.certified:
            return Verdict(True)
        return Verdict(False, f"call to uncertified '{name}': {inner.reason}")


def _comp_target_names(target: ast.AST) -> list[str]:
    """Names bound by a comprehension target (handles tuple unpacking)."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _comp_target_names(element)]
    return []


def _strip_annotations(node: ast.AST) -> None:
    """Blank out type annotations in-place before the certification walk.

    Parameter/return/variable annotations (``tokens: list[Token]``, ``-> bool``)
    reference *types* that are never evaluated when the callable is *called*, so
    they must not disqualify it — otherwise every type-annotated guard would fail
    on the class name in its own signature. The node comes from a fresh per-call
    parse (:func:`_get_ast_node`), so mutating it is safe.
    """
    for child in ast.walk(node):
        if isinstance(child, ast.arg):
            child.annotation = None
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            child.returns = None
        elif isinstance(child, ast.AnnAssign):
            child.annotation = ast.Constant(value=None)


def _check_structural(node: ast.AST) -> Verdict | None:
    """Reject nodes that break the closed-world/termination guarantee outright.

    Returns a failing :class:`Verdict` for a disqualifying node, or ``None`` if
    the node is structurally fine (call/name/comprehension checks happen in the
    certifier, which needs scope and closure context).
    """
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return Verdict(False, "import")
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return Verdict(False, "global/nonlocal declaration")
    if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
        return Verdict(False, "unbounded iteration (for/while)")
    if isinstance(node, (ast.Await, ast.Yield, ast.YieldFrom)):
        return Verdict(False, "await/yield")
    if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
        return Verdict(False, f"private/dunder attribute '{node.attr}'")
    return None


def _bound_names(node: ast.AST) -> frozenset[str]:
    """Names bound in *node*'s **own** scope — assignments/targets plus the names
    of nested functions defined here.

    A ``Name`` read whose id is in this set refers to something the callable
    itself binds, so it is not external state. Store-context ``Name`` nodes cover
    assignments, augmented assignments, ``for``/comprehension targets and walrus
    bindings; a nested ``def`` also binds its own name in this scope.

    Crucially this does **not** descend into nested ``Lambda``/``def`` bodies:
    names bound only inside a nested scope (its parameters, its internal
    assignments) are *not* bindings of this scope. Unioning them would let a
    nested binding mask an outer read of an external — possibly mutable — name,
    a false *accept* of exactly what :meth:`_Certifier._check_name` exists to
    reject. A read of a genuine nested-scope local instead resolves to
    :data:`_UNRESOLVED` in ``_check_name`` and passes safely, so leaving those
    names out costs nothing; the only theoretical effect is that a nested local
    *shadowing* a mutable global is conservatively rejected — the safe direction.
    """
    names: set[str] = set()
    for child in ast.iter_child_nodes(node):
        _collect_scope_bindings(child, names)
    return frozenset(names)


def _collect_scope_bindings(node: ast.AST, names: set[str]) -> None:
    """Accumulate the names *node* binds in the current scope, without recursing
    into nested function/lambda bodies (whose bindings belong to *their* scope).

    A nested ``def``'s own name is bound in this scope, so record it and stop; a
    ``Lambda`` binds no name here, so stop without recording. Everything else
    (including comprehensions, whose targets bind in this scope as before) is
    walked through.
    """
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
        names.add(node.id)
        return
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        names.add(node.name)
        return
    if isinstance(node, ast.Lambda):
        return
    for child in ast.iter_child_nodes(node):
        _collect_scope_bindings(child, names)
