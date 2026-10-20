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
blocklist in :func:`cpnx.sandbox.verify_callable_purity`, nor the best-effort
advisory scanner in :mod:`cpnx.linting` (which *warns* about network/database/
clock/randomness trouble spots rather than deciding inline-safety). Side effects cannot be
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
import builtins
import inspect
import types
from collections.abc import Callable
from dataclasses import dataclass

from .sandbox import SandboxEvaluator, _find_target_node
from .tokens import FrozenDict

#: Builtins that iterate/reduce a finite iterable. Added on top of the string
#: sandbox's arithmetic/coercion set because the real guard corpus needs them
#: (``sorted``/``any``/``all`` over a token list). Each is O(n) over a finite,
#: engine-built argument, so it preserves the termination proof.
_ITERATION_BUILTINS = frozenset({"sorted", "next", "any", "all"})

#: Names permitted in call position when the callee is a bare ``Name``.
_ALLOWED_CALL_NAMES = frozenset(SandboxEvaluator.ALLOWED_BUILTINS) | _ITERATION_BUILTINS

#: Immutable leaf types a closure cell (free variable) may hold — types with no contents to
#: inspect, so membership alone settles it. ``bool`` is a subclass of ``int`` and needs no
#: separate entry. Containers are *not* leaves: tuples, frozensets and frozen mappings are
#: walked recursively instead (see :func:`_is_immutable`).
_IMMUTABLE_LEAVES = (int, float, str, bytes, bool, type(None))

#: Frozen mapping types whose *contents* decide the answer: [`FrozenDict`][cpnx.FrozenDict]
#: and, on 3.15+, :pep:`814`'s builtin ``frozendict``. A frozen lookup table read once at
#: construction time is the idiom the arc/transition docs recommend for a certifiable
#: callable, so rejecting it pushed users off the inline path (and arcs off the key index)
#: for doing exactly what they were told.
#:
#: Neither type is trusted blanketly, because neither is *deeply* immutable on its own.
#: ``frozendict`` is shallow by design — ``frozendict({"a": {"b": 1}})`` still holds a live
#: ``dict`` — and ``FrozenDict``, though it recursively freezes nested ``dict``/``list``
#: values, admits any *hashable* value, so a custom object with mutable attributes passes
#: straight through. So both recurse through :func:`_is_immutable` on keys *and* values.
_FROZEN_MAPPINGS = tuple(t for t in (FrozenDict, getattr(builtins, "frozendict", None)) if t is not None)

#: ``types.LazyImportType`` on 3.15+ (:pep:`810`), else ``None``.
_LAZY_IMPORT_TYPE = getattr(types, "LazyImportType", None)


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
    """True if *value* is deeply immutable (safe to close over).

    Every immutable *container* is walked rather than trusted, because being frozen bounds
    what the container does, not what its members do. A ``frozenset`` requires only that its
    members be **hashable**, and a user-defined class is hashable by identity by default even
    while holding mutable attributes — so ``frozenset({thing})`` was admitted while
    ``thing.items`` stayed live and observable from a guard proven to be closed-world. The
    same argument already applies to ``tuple`` and to the frozen mappings, which is why all
    three recurse through here.
    """
    if isinstance(value, _IMMUTABLE_LEAVES):
        return True
    if isinstance(value, (tuple, frozenset)):
        return all(_is_immutable(item) for item in value)
    if isinstance(value, _FROZEN_MAPPINGS):
        return all(_is_immutable(key) and _is_immutable(item) for key, item in value.items())
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

    "As *func* would at call time" is load-bearing on 3.15+, where a name can be a deferred
    import that has not happened yet — see :func:`_force_lazy_import`.
    """
    code = func.__code__
    if name in code.co_freevars and func.__closure__ is not None:
        cell = func.__closure__[code.co_freevars.index(name)]
        try:
            return _force_lazy_import(cell.cell_contents)
        except ValueError:  # empty cell (recursive def not yet bound)
            return _UNRESOLVED
    globals_ = getattr(func, "__globals__", {})
    return _force_lazy_import(globals_.get(name, _UNRESOLVED))


def _force_lazy_import(value: object) -> object:
    """Resolve a :pep:`810` deferred import, so a name resolves to what it will *be* when called.

    On 3.15+, ``lazy import m`` / ``lazy from m import sym`` leaves a
    :class:`types.LazyImportType` proxy in ``__globals__``. CPython resolves it when
    ``LOAD_GLOBAL`` executes — *not* when a dict lookup hands it to Python — so inspecting at
    construction time otherwise sees the proxy rather than the module, function or constant it
    stands for. Three things went wrong because of that, all of them silent:

    * the certifier rejected a genuinely certifiable helper as ``call to non-function``, and an
      immutable constant as ``reads mutable/uncertified external``, dropping the callable off
      the inline path (and an arc ``key``/``filter`` off its place's key index);
    * :mod:`cpnx.linting` missed every hazard reached through a lazy import, because the proxy
      resolves to no package — ``lazy import sqlite3`` then hid ``sqlite3.connect`` in a guard
      that an eager ``import sqlite3`` flags; and
    * *calling* the guard once resolved the proxy, so a later verdict flipped. A certification
      that depends on whether anything happened to invoke the callable first is the worst of
      the three, and the reason this resolves eagerly rather than reporting "unresolved".

    There is no way to *test* for a proxy: it is deliberately transparent, so ``isinstance``
    and ``type()`` report the eventual object, and any attribute access resolves and forwards
    (``proxy.resolve()`` raises :exc:`AttributeError` from the *resolved* object). So this
    calls the unbound ``resolve`` descriptor, which bypasses that forwarding. A
    :exc:`TypeError` means the value was never a proxy — the overwhelmingly common case.

    Note this does defeat the deferral for names a guard actually mentions. That is the narrow,
    deliberate price: ``_resolve_name`` is only reached for names in the callable's own AST, and
    a guard that references a module needs it imported when it fires anyway.
    """
    if _LAZY_IMPORT_TYPE is None or value is _UNRESOLVED:
        return value
    try:
        return _LAZY_IMPORT_TYPE.resolve(value)
    except TypeError:
        return value  # not a lazy proxy
    except Exception:
        # A deferred import that cannot be satisfied raises here instead of at first use.
        # Report it as unresolved: that is what the name is, and this path must never raise.
        return _UNRESOLVED


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

    def _check_name(self, node: ast.Name, func: Callable, bound: frozenset[str], stack: frozenset[Callable]) -> Verdict:
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
        """Everything the comprehension *iterates* must be rooted at a parameter or an in-comp target.

        This is what bounds iteration: the only finite iterables we admit are the
        engine-built argument(s) and things derived from them within the same
        comprehension. Iterating anything else is rejected.

        Two positions iterate, not one. Every generator's ``iter`` does, and so does the
        expression a :pep:`798` comprehension unpacks (:func:`_unpacked_element`) — which the
        AST puts outside ``generators``, so it has to be checked separately or it is admitted
        unchecked.
        """
        targets = {name for gen in node.generators for name in _comp_target_names(gen.target)}
        allowed_roots = params | targets
        for generator in node.generators:
            root = _root_name(generator.iter)
            if root not in allowed_roots:
                return Verdict(False, "iteration not bounded by the argument")
        unpacked = _unpacked_element(node)
        if unpacked is not None and _root_name(unpacked) not in allowed_roots:
            return Verdict(False, "unpacked iteration not bounded by the argument")
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


def _unpacked_element(node: ast.AST) -> ast.AST | None:
    """The expression a :pep:`798` comprehension *unpacks*, or ``None`` if it unpacks nothing.

    ``[*xs for t in ts]`` (3.15+) is sugar for ``[x for t in ts for x in xs]``: the unpacked
    ``xs`` is iterated exactly like a ``for``-clause's ``iter``, and its length multiplies the
    result. But the AST stores it in ``elt`` (wrapped in :class:`ast.Starred`) rather than in
    ``generators``, so :meth:`_Certifier._check_comprehension` does not see it there and would
    admit it unchecked — letting ``[*anything for t in ts]`` iterate a module global or a
    function local that the equivalent ``for x in anything`` clause is rejected for.

    ``{**m for t in ts}`` is the dict spelling, and puts the unpacked mapping in ``key`` with
    ``value`` left as ``None`` — the opposite side from :class:`ast.Dict`, where ``{**m}`` is
    ``keys=[None]``.

    A comprehension that unpacks nothing returns ``None``: a plain ``elt`` is *evaluated* once
    per iteration rather than iterated, so it must not be bounds-checked. On ≤3.14 the syntax
    is a :exc:`SyntaxError`, so neither branch can fire and this is always ``None``.
    """
    if isinstance(node, ast.DictComp):
        return node.key if getattr(node, "value", None) is None else None
    element = getattr(node, "elt", None)
    return element.value if isinstance(element, ast.Starred) else None


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
    itself binds, so it is not external state. It covers function-scope
    assignments, augmented assignments and walrus (``:=``) bindings, and a nested
    ``def``'s own name — but **not** bindings that belong to a nested scope: a
    nested ``Lambda``/``def`` body, or a comprehension's ``for``-targets (Python 3
    gives comprehensions their own scope). See :func:`_collect_scope_bindings`.

    This over-inclusion is the *unsound* direction: a name wrongly treated as
    locally bound lets :meth:`_Certifier._check_name` short-circuit before it can
    reject an outer read of a same-named mutable external. So we err the other
    way — a read of a genuine nested/comprehension-scope local instead resolves to
    :data:`_UNRESOLVED` in ``_check_name`` and passes safely, and the only
    residual cost is that a local *shadowing* a mutable global of the same name is
    conservatively rejected (the safe direction). Note ``bound`` is a
    false-*reject*-avoidance heuristic, not a soundness mechanism; other Python
    scope-introducers not reachable in a certifiable body today (e.g. a nested
    ``class``) would share the same shadowing trait if they became reachable.
    """
    names: set[str] = set()
    for child in ast.iter_child_nodes(node):
        _collect_scope_bindings(child, names)
    return frozenset(names)


#: Comprehension node types. In Python 3 each introduces its **own** scope, so
#: its ``for``-targets do not bind in the enclosing scope — see
#: :func:`_collect_scope_bindings`.
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _collect_scope_bindings(node: ast.AST, names: set[str]) -> None:
    """Accumulate the names *node* binds in the current scope, without recursing
    into scopes whose bindings do not belong to it.

    A nested ``def``'s own name is bound in this scope, so record it and stop; a
    ``Lambda`` binds no name here, so stop without recording. A **comprehension**
    likewise has its own scope: its ``for``-targets do *not* bind here (collecting
    them would let a target shadowing a mutable global mask an outer read of it —
    the same false accept as a nested-``def`` binding), but a walrus (``:=``)
    binding *does* leak out to the enclosing function, so those are collected
    (see :func:`_leaked_walrus_targets`). Everything else is walked through.
    """
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
        names.add(node.id)
        return
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        names.add(node.name)
        return
    if isinstance(node, ast.Lambda):
        return
    if isinstance(node, _COMPREHENSIONS):
        _leaked_walrus_targets(node, names)
        return
    for child in ast.iter_child_nodes(node):
        _collect_scope_bindings(child, names)


def _leaked_walrus_targets(node: ast.AST, names: set[str]) -> None:
    """Collect walrus (``NamedExpr``) target names that leak from *node* into the
    enclosing function scope.

    A walrus inside a comprehension binds in the nearest enclosing *function*
    scope (PEP 572), transparently through any number of comprehension layers —
    so descend through comprehensions, but stop at a nested ``Lambda``/``def``
    boundary, whose walruses are scoped to *it*, not to us.
    """
    if isinstance(node, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)):
        return
    if isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
        names.add(node.target.id)
    for child in ast.iter_child_nodes(node):
        _leaked_walrus_targets(child, names)
