import __future__

import ast
import inspect
import linecache
import sys
import threading
import types
import warnings
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

    Returns:
        The position (`_code_anchor`), or `None` when the interpreter records no columns:
        before Python 3.11, under `-X no_debug_ranges`, or for an object with no code of its own.
    """
    code = _own_code(func)
    return None if code is None else _code_anchor(code)


def _code_anchor(code: types.CodeType) -> _Position | None:
    """Return the position of one instruction of `code`'s own body, or `None` if none is recorded.

    Nested functions compile to code objects of their own, so every positioned instruction
    of `code` lies in its body and outside the body of any function nested in it. The last
    one is taken: the leading instructions (`RESUME` and the like) carry the function's first
    line with no real span.
    """
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
#: for them, so the names inside them reach no code object at all. From Python 3.12 (PEP 709)
#: a list, set or dict comprehension in a function is inlined into it instead.
_INLINED_SCOPES = (ast.ListComp, ast.SetComp, ast.DictComp) if sys.version_info >= (3, 12) else ()
_NESTED_SCOPES = tuple(
    scope
    for scope in (ast.Lambda, ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.DictComp)
    if scope not in _INLINED_SCOPES
)


def _names_in(node: ast.AST, *, nested: bool) -> frozenset[str]:
    """Return the variable and attribute names used in `node` and the nodes under it.

    With `nested=False`, a nested scope (`_NESTED_SCOPES`), `node` included, is not entered.
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


def _nested_code_count(code: types.CodeType) -> int:
    """Return how many code objects are nested in `code`, at any depth."""
    nested = [const for const in code.co_consts if isinstance(const, types.CodeType)]
    return len(nested) + sum(_nested_code_count(each) for each in nested)


def _lambda_names(node: ast.Lambda, code: types.CodeType) -> frozenset[str]:
    """Return the names the body of a lambda uses that `code` must carry if it is that lambda's code.

    The compiler records each name of the lambda's own scope in its code object, also for a
    branch it then drops as unreachable. Each nested scope becomes a code object of its own,
    nested in `code` and holding the names inside it, except in a dropped branch, where none
    is made, for it or for the scopes inside it. So if `code` has at least as many nested code objects,
    at every depth, as the body has nested scopes, none was dropped and every name counts. If
    it has fewer, the names inside nested scopes are left out: counting them could rule out
    the lambda's own node.

    A name with a leading `__` is left out as well: inside a class the compiler stores `__x`
    as `_Class__x`, and `__debug__` is a constant, so neither can be looked for by its spelling.
    """
    scopes = sum(isinstance(each, _NESTED_SCOPES) for each in ast.walk(node.body))
    nested = scopes <= _nested_code_count(code)
    return frozenset(name for name in _names_in(node.body, nested=nested) if not name.startswith("__"))


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
    name settles it. A lambda compiles to `<lambda>`, and several may share a line. The
    names its body uses (`_lambda_names`) the compiler records in its code object or in the
    code nested in it (`co_names` and the variable tables). So a lambda whose body uses such a
    name that `code` does not carry is some other lambda. The converse does not hold: `code`
    may carry more names than a lambda uses and still not rule it out.
    """
    if isinstance(node, ast.Lambda):
        return code.co_name == "<lambda>" and _lambda_names(node, code) <= code_names
    return node.name == code.co_name


def _global_names(code: types.CodeType) -> frozenset[str]:
    """Return the global and attribute names (`co_names`) of `code` and the code nested in it."""
    names = set(code.co_names)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            names |= _global_names(const)
    return frozenset(names)


def _demanglings(name: str) -> tuple[str, ...]:
    """Return the private names `__x` that the compiler could have stored as `name` in a class.

    In a class `C`, a name `__x` (not ending in `__`) is stored as `_C__x`, `C` without its
    leading underscores. A class name may itself hold `__` (`A__B` gives `_A__B__x`), so every
    split after `_C` is a reading.
    """
    if not name.startswith("_") or name.startswith("__"):
        return ()
    splits = (index for index in range(2, len(name) - 2) if name.startswith("__", index))
    return tuple(private for private in (name[index:] for index in splits) if not private.endswith("__"))


def _accounts_for(node: ast.AST, needed: frozenset[str]) -> bool:
    """Say whether the source under `node` uses every name in `needed`, mangled names included."""
    names = _names_in(node, nested=True)
    return all(name in names or any(private in names for private in _demanglings(name)) for name in needed)


def _plausible(
    survivors: tuple[ast.AST, ...], code: types.CodeType, candidates: tuple[ast.AST, ...]
) -> tuple[ast.AST, ...]:
    """Return `survivors`, or the `candidates` that can be the lambda `code` is if none of them can.

    `survivors` are the `candidates` that `_could_be` left. Every global and attribute name in
    a lambda's code comes from its own source, so its own node uses them all. If no survivor
    does, `_could_be` ruled out the lambda's own node, and the lookup falls back to every
    candidate that uses them all: never more lenient, since the lambda is one of them. If no
    candidate does, none is the lambda, and nothing is returned. A `def` is told by its name
    alone, which `_could_be` cannot get wrong.
    """
    if code.co_name != "<lambda>":
        return survivors
    needed = _global_names(code)
    if any(_accounts_for(node, needed) for node in survivors):
        return survivors
    return tuple(node for node in candidates if _accounts_for(node, needed))


def _first_line(node: ast.AST) -> int:
    """Return the line a function node's source starts on: its first decorator's, if it has one.

    The AST gives a decorated `def` the line of the `def` keyword, but its code object starts
    at its first decorator (`co_firstlineno`).
    """
    return min([node.lineno, *(decorator.lineno for decorator in getattr(node, "decorator_list", ()))])


#: The `__future__` compiler flags. They are kept in a code object's `co_flags`, and code
#: compiled from the same source matches only under the same ones. `nested_scopes` is left
#: out: its flag is `CO_NESTED`, which the compiler sets on every nested function by itself,
#: so passing it back changes nothing but would compile the file a second time.
_FUTURE_FLAGS = 0
for _feature in __future__.all_feature_names:
    if _feature != "nested_scopes":
        _FUTURE_FLAGS |= getattr(__future__, _feature).compiler_flag

#: Held while a source is compiled with its warnings silenced: before Python 3.14
#: `warnings.catch_warnings` swaps a process-wide filter list, so two at once would interleave.
#: The swap is still process-wide there: a warning another thread raises during the compile
#: (a few milliseconds per file, once per file) is silenced with it.
_COMPILE_LOCK = threading.Lock()


class _ParsedSource:
    """One source file's parse, plus the function nodes found for each lookup made in it.

    A lookup is a start line and, where the interpreter records columns, the position of the
    callable's own code (`_body_anchor`): callables that share a start line are told apart by it.
    """

    __slots__ = ("source", "path", "tree", "starting", "targets", "compiled", "held")

    def __init__(self, source: bytes | str, path: str, tree: ast.AST) -> None:
        self.source = source
        self.path = path
        self.tree = tree
        # The function nodes by the line each starts on, in the order `ast.walk` meets them.
        self.starting: dict[int, list[ast.AST]] = {}
        for node in ast.walk(tree):
            if isinstance(node, _FUNCTION_NODES):
                self.starting.setdefault(_first_line(node), []).append(node)
        self.targets: dict[tuple[int, _Position | None], tuple[ast.AST, ...]] = {}
        self.compiled: dict[int, dict[tuple[int, str], list[tuple[types.CodeType, types.CodeType]]]] = {}
        self.held: dict[types.CodeType, tuple[ast.AST, ...] | None] = {}

    def locate(self, start_line: int, anchor: _Position | None) -> tuple[ast.AST, ...]:
        """Return the function nodes starting on `start_line` that `anchor` allows: all, if it is `None`."""
        key = (start_line, anchor)
        if key not in self.targets:
            starting = tuple(self.starting.get(start_line, ()))
            self.targets[key] = starting if anchor is None else _node_holding(starting, anchor)
        return self.targets[key]

    def compiled_like(self, code: types.CodeType) -> list[tuple[types.CodeType, types.CodeType]]:
        """Return the code objects this source compiles to that start where `code` does, with its name.

        Each comes with itself without positions (`_without_positions`).

        The source is compiled once per set of `__future__` flags, from its text rather
        than the shared tree, so that it compiles whatever the import compiled. Top-level
        `await` is allowed, as IPython and `python -m asyncio` allow it in a cell; it changes
        nothing for a file, which could not have been imported with one. The compiler's
        warnings (`x is 1`, `return` in `finally`) are silenced: the import already gave
        them, and under an "error" filter one would otherwise refuse every callable in the file.

        Raises:
            SyntaxError: If the source parses but does not compile.
        """
        flags = code.co_flags & _FUTURE_FLAGS
        if flags not in self.compiled:
            with _COMPILE_LOCK, warnings.catch_warnings():
                warnings.simplefilter("ignore")
                module = compile(
                    self.source,
                    self.path,
                    "exec",
                    flags=flags | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
                    dont_inherit=True,
                )
            index: dict[tuple[int, str], list[tuple[types.CodeType, types.CodeType]]] = {}
            pending = [module]
            while pending:
                each = pending.pop()
                index.setdefault((each.co_firstlineno, each.co_name), []).append((each, _without_positions(each)))
                pending.extend(const for const in each.co_consts if isinstance(const, types.CodeType))
            self.compiled[flags] = index
        return self.compiled[flags].get((code.co_firstlineno, code.co_name), [])


def _without_positions(code: types.CodeType) -> types.CodeType:
    """Return `code`, and the code nested in it, without line and column positions.

    Code objects compare equal regardless of their file name; with positions dropped they
    compare equal when their instructions, constants, names and flags are. Positions are
    left out because they are not what runs, and because one Python release (3.12.3 and
    3.12.13, say) can record them differently for the same source, in a `.pyc` that the
    other still loads.
    """
    consts = tuple(
        _without_positions(const) if isinstance(const, types.CodeType) else const for const in code.co_consts
    )
    return code.replace(co_consts=consts, co_linetable=b"")


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

#: What "the source cannot be found or parsed" looks like: no file (``OSError``), source that
#: does not parse or compile (``SyntaxError``), undecodable bytes (``UnicodeDecodeError``, a
#: ``ValueError``; also a null byte, or a cycle of ``__wrapped__``), or a code object whose
#: file name is not a string (``TypeError``).
_SOURCE_UNAVAILABLE = (OSError, TypeError, SyntaxError, ValueError)


def _read_source(path: str, func: Callable, code: types.CodeType) -> bytes | str:
    """Return the source `code` was compiled from, as its file's bytes or as the lines held for it.

    Code with no file on disk can still have source that `inspect` would find: a notebook
    cell (registered in `linecache`), a REPL entry (Python 3.13+), or a module loaded from a
    zip archive (read through its loader). A module's loader is asked only for its own file:
    `linecache` would otherwise file another module's source under this name. Whatever is
    read must still compile to `code` (`_callable_nodes`) before any node of it is used.
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
        cached = _SOURCE_CACHE[path] = _ParsedSource(source, path, ast.parse(source, filename=path))
    return cached


def _callable_nodes(func: Callable) -> tuple[ast.AST, ...]:
    """Return the `def`/`lambda` nodes `func` may be, from a shared parse: one, when it can be told.

    The candidates are the function nodes that start on the line `func` starts on, a
    decorated `def` counting from its first decorator (`_first_line`), as its code does. So a
    decorated `def` is found as itself, at any depth: neither a lambda among its decorator's
    arguments nor the function around it is taken for it. Certification then refuses it for
    its decorator, and the purity check and the lint read its own body.

    The source must compile to code equal to `func`'s, positions aside (`_without_positions`),
    starting on its line. A file edited since it was loaded, a stale `.pyc`, or source that
    `linecache` holds for another module under `func`'s file name finds nothing.

    - With column positions (`_body_anchor`), each matching code object of the source lies
      in the body of its own node, by the source's own positions (`_code_anchor`): those
      nodes are the result, narrowed to the one that `func`'s own code lies in where that is
      one of them. So two lambdas on one line, a lambda and the lambda it returns, a `def`
      with a lambda default on its first line, and a `def` with a lambda in its decorator are
      each their own node. Several are returned only for code that compiles alike.
    - Without them, the candidates that cannot be `func` are dropped (`_could_be`): a `def`
      of another name, a lambda where `func` is a `def`, and a lambda whose body uses a name
      that `func`'s code does not carry (`_lambda_names`). Should that leave no node that
      uses every global and attribute name `func`'s code does, the candidates that do are
      kept instead (`_plausible`). What is left is usually one node. Where it is several
      (lambdas on one line, none using a name the others' code lacks), they cannot be told
      apart and all are returned: `func` is one of them, so a caller that holds `func` to
      what any of them does is never more lenient than the truth.

    The source is read from `func`'s code: its file (`co_filename`) and first line
    (`co_firstlineno`). `inspect.getsourcelines` is not asked, since it would file the
    source of `func`'s module under a file name that has none.

    The nodes belong to a cached tree: read them, never mutate them.

    Args:
        func: The callable to locate.

    Returns:
        The candidate nodes; empty when `func` has no code of its own or no function node is it.

    Raises:
        OSError: If the source cannot be found or read.
        SyntaxError: If the source cannot be parsed or compiled.
        ValueError: If the source cannot be decoded.
    """
    # The code a decorator wraps (`__wrapped__`), if any: the body the gates read.
    code = _own_code(func)
    if code is None:
        return ()
    parsed = _parsed_source(code.co_filename, func, code)
    held = _held_nodes(parsed, code)
    if held == ():
        return ()
    candidates = parsed.locate(code.co_firstlineno, None)
    anchor = _body_anchor(func)
    if anchor is not None and held is not None:
        chosen = tuple(node for node in parsed.locate(code.co_firstlineno, anchor) if node in held)
        return chosen or held
    code_names = _code_names(code)
    survivors = tuple(node for node in candidates if _could_be(node, code, code_names))
    return _plausible(survivors, code, candidates)


def _held_nodes(parsed: _ParsedSource, code: types.CodeType) -> tuple[ast.AST, ...] | None:
    """Return the nodes of `parsed` whose compiled code equals `code`, positions aside.

    Returns:
        Those nodes, each found by where its compiled code lies by the source's own
        positions; `()` if the source compiles to no such code on `code`'s line; `None` if
        it does but records no positions to place it by (Python 3.10, `-X no_debug_ranges`).
    """
    if code not in parsed.held:
        own = _without_positions(code)
        anchors = [_code_anchor(each) for each, bare in parsed.compiled_like(code) if bare == own]
        if not anchors:
            held: tuple[ast.AST, ...] | None = ()
        elif None in anchors:
            held = None
        else:
            candidates = parsed.locate(code.co_firstlineno, None)
            held = tuple(dict.fromkeys(node for anchor in anchors for node in _node_holding(candidates, anchor)))
        parsed.held[code] = held
    return parsed.held[code]


def _alike_note(nodes: tuple[ast.AST, ...]) -> str:
    """Return what to add to a complaint about a callable that was not told from its neighbours.

    Empty for a callable that was found alone. Otherwise the complaint may be about a
    neighbour's body, and the way out is the same either way.
    """
    if len(nodes) < 2:
        return ""
    return (
        f" ({len(nodes)} functions start on line {nodes[0].lineno} and cannot be told apart; "
        "put them on separate lines)"
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
    - A decorated callable (one with ``__wrapped__``): the wrapper is what runs, and
      only the body it wraps can be read, so neither is vouched for.

    The default values the callable was defined with are checked as they are, not as its
    source spells them (the source may have been edited since): a ``list``, ``dict`` or
    ``set`` is rejected, also when the default names one (``s=STATE``), and a Python function
    whose source can be read is checked as a callable in its own right (and rejected if that
    source no longer compiles to it). A default function with no source to read at all
    (``os.path.join`` from a frozen module, a ``.pyc``-only install) is left to
    :mod:`cpnx.certification`, which holds every default to its own rule.

    It also raises when the callable's own ``def`` or ``lambda`` cannot be found in its
    source: a builtin, a ``functools.partial``, a class or other callable object, code with
    no source (``exec`` of a string, ``python -c``), or a source file that no longer compiles
    to the code that runs (edited since it was loaded, or rewritten as it was loaded, as
    pytest's assertion rewriting and some import hooks do). A check that cannot see the body
    passes nothing.
    """
    _verify_purity(func, frozenset())


def _verify_purity(func: Callable, seen: frozenset[int]) -> None:
    """`verify_callable_purity`, with the functions already being checked (by `id`) in `seen`."""
    if getattr(func, "__wrapped__", None) is not None:
        raise PermissionError(
            f"Cannot verify purity of {func!r}: it is decorated, and the wrapper that runs is not "
            "the body that can be read. Pass the undecorated function."
        )
    try:
        nodes = _callable_nodes(func)
    except _SOURCE_UNAVAILABLE as exc:
        raise _source_unavailable(func) from exc
    if not nodes:
        raise _source_unavailable(func)
    try:
        for node in nodes:
            _verify_ast_purity(node)
    except PermissionError as exc:
        raise PermissionError(f"{exc}{_alike_note(nodes)}") from None
    _verify_default_values(func, seen | {id(func)})


def _verify_default_values(func: Callable, seen: frozenset[int]) -> None:
    """Check the default values `func` holds, as `verify_callable_purity` describes."""
    for name, value in _default_values(func):
        if isinstance(value, (list, dict, set)):
            raise PermissionError(
                f"Mutable default argument '{name}' in CPN callable introduces hidden state between firings."
            )
        if isinstance(value, types.FunctionType) and id(value) not in seen and _has_source(value):
            try:
                _verify_purity(value, seen)
            except PermissionError as exc:
                raise PermissionError(f"Default '{name}' of {_label(func)}: {exc}") from None


def _has_source(func: Callable) -> bool:
    """Say whether `func`'s source can be read at all, whether or not it still compiles to its code."""
    try:
        _callable_nodes(func)
    except _SOURCE_UNAVAILABLE:
        return False
    return True


def _label(func: Callable) -> str:
    return getattr(func, "__qualname__", None) or repr(func)


def _default_values(func: Callable) -> list[tuple[str, object]]:
    """Return the default argument values `func` runs with, by parameter: positional ones, then keyword-only ones.

    They ran once, where the function was defined, so they are not in its code and cannot
    be checked through it: only the values themselves say what the callable was given.
    """
    target = getattr(func, "__func__", func)
    positional = getattr(target, "__defaults__", None) or ()
    code = getattr(target, "__code__", None)
    parameters = code.co_varnames[: code.co_argcount] if code is not None else ()
    names = parameters[len(parameters) - len(positional) :] if len(parameters) >= len(positional) else ()
    keywords = getattr(target, "__kwdefaults__", None) or {}
    return [
        *((names[index] if names else f"#{index}", value) for index, value in enumerate(positional)),
        *keywords.items(),
    ]


def _source_unavailable(func: Callable) -> PermissionError:
    return PermissionError(
        f"Cannot verify purity of {func!r}: source unavailable{_why_unseen(func)}. "
        "Use a plain lambda or def-statement function, in a file or a notebook cell, instead."
    )


def _why_unseen(func: Callable) -> str:
    """Say why a callable whose file still has a function on its line was not found there.

    Its source compiles to other code: the file was edited since it was loaded, or the code
    was rewritten as it was loaded (pytest's assertion rewriting, an import hook). Rewriting
    the callable as a plain function does not help then, so the message says what to look at.
    """
    code = _own_code(func)
    if code is None:
        return ""
    try:
        parsed = _parsed_source(code.co_filename, func, code)
    except _SOURCE_UNAVAILABLE:
        return ""
    if not parsed.locate(code.co_firstlineno, None) or _held_nodes(parsed, code) != ():
        return ""
    return (
        " (its source no longer compiles to the code that runs: the file was edited since it was "
        "loaded, or an import hook such as pytest's assertion rewriting changed the code)"
    )
