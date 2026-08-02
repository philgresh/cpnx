"""Best-effort static linting for side-effecting / non-deterministic callables.

This is the third and most permissive of cpnx's three confidence layers for the
Turing-complete callables users embed in guards, arc keys, and filters:

* :mod:`cpnx.certification` — a **whitelist** proof that a callable is
  closed-world and terminating, used to decide *how* it runs (inline vs. the
  timeout-bounded executor pool). Never raises.
* :func:`cpnx.sandbox.verify_callable_purity` — a small **blocklist** of
  unambiguous escapes (``open``/``eval``/``exec``/imports/``global`` …) that is a
  hard gate: it **raises** ``PermissionError`` at construction.
* **This module** — a **best-effort advisory linter** that flags the specific
  *trouble spots* most corrosive to a Petri net's analysability: network I/O,
  database access, and reads of the clock / randomness. These are not illegal
  (an occasional ``time.time()`` in a guard is legal Python), but each makes a
  transition's enabling condition **non-deterministic or environment-dependent**,
  which defeats reasoning about liveness, boundedness, and reachability. So this
  layer only **warns** (see :class:`CpnxLintWarning`), unless the caller opts in
  to strict mode.

Why advisory, not a hard gate
-----------------------------
Sound static detection of side effects is impossible in a language as dynamic as
Python — dynamic attribute access, ``getattr``, monkeypatching and late binding
all route around any AST rule (this is precisely the gap that motivates *dynamic*
linting: Eghbali, Burk & Pradel, "DyLin: A Dynamic Linter for Python," *Proc.
ACM Softw. Eng.* 2 (FSE 2025), Art. 92, doi:10.1145/3729395). We therefore make
no soundness claim: this linter catches the *explicit, legible* trouble spots and
is paired with runtime invariant fuzzing (see ``tests/test_invariants_fuzz.py``)
that empirically validates convergence to quiescence — the same static-then-
dynamic pairing DyLin advocates. Anything the linter cannot see, fuzzing is meant
to surface.

Grounding
---------
High-level Petri nets (ISO/IEC 15909-1:2019, *Systems and software engineering —
High-level Petri nets — Part 1: Concepts, definitions and graphical notation*)
define transition enabling in terms of data-dependent inscriptions over token
colours. cpnx deliberately departs from that decidable core by admitting
arbitrary Python in those inscriptions; this module is the best-effort tripwire
for the cost of that departure.

Public API
----------
:func:`lint_callable` returns a list of :class:`LintFinding` and never emits or
raises (pure inspection — useful for CI gating). :func:`lint_and_warn` is the
engine-facing wrapper that emits one :class:`CpnxLintWarning` per finding, or
raises :class:`SideEffectLintError` when strict mode is on (opt in via
:func:`set_strict` or the ``CPNX_LINT_STRICT`` environment variable).

Known limitations (best-effort by construction)
------------------------------------------------
* Only the callable's **own body** is scanned, not helpers it calls. A risky call
  hidden one function deep is invisible here (certification *is* transitive; this
  is not). Uncertified helpers already fall back to the sandboxed pool.
* Detection is by **name resolution + a small distinctive-attribute table**, so
  fully dynamic dispatch (``getattr(mod, "get")(...)``) is not seen.
* Like certification, this reflects binding state at inspection time; later
  rebinding of a referenced name is undefined behaviour.
"""

import ast
import inspect
import os
import warnings
from collections.abc import Callable
from dataclasses import dataclass

from .certification import _UNRESOLVED, _get_ast_node, _resolve_name, _root_name

#: Category label for findings whose root symbol is a network client/transport.
NETWORK = "network"
#: Category label for findings that reach a database driver / ORM.
DATABASE = "database"
#: Category label for findings that read the clock or a randomness source.
NONDETERMINISM = "nondeterminism"

#: Top-level package name -> risk category. Detection classifies a called symbol
#: by the module that *defines* it (a module's own ``__name__``, or another
#: object's ``__module__``), so both ``import requests; requests.get(...)`` and
#: ``from requests import get; get(...)`` resolve to the same "network" verdict,
#: and aliasing (``import requests as r``) is transparent because we resolve the
#: bound object, not the source text. Kept deliberately conservative — packages
#: with a broad benign surface (e.g. ``os``, whose ``os.path`` is harmless) are
#: intentionally absent and handled by the distinctive-attribute table instead.
_TOP_PACKAGE_CATEGORY: dict[str, str] = {
    # Network I/O.
    "requests": NETWORK,
    "httpx": NETWORK,
    "aiohttp": NETWORK,
    "urllib": NETWORK,
    "urllib2": NETWORK,
    "urllib3": NETWORK,
    "http": NETWORK,
    "socket": NETWORK,
    "ssl": NETWORK,
    "ftplib": NETWORK,
    "smtplib": NETWORK,
    "poplib": NETWORK,
    "imaplib": NETWORK,
    "telnetlib": NETWORK,
    "websocket": NETWORK,
    "websockets": NETWORK,
    # Database drivers / ORMs.
    "sqlite3": DATABASE,
    "psycopg2": DATABASE,
    "psycopg": DATABASE,
    "sqlalchemy": DATABASE,
    "pymysql": DATABASE,
    "MySQLdb": DATABASE,
    "mysql": DATABASE,
    "asyncpg": DATABASE,
    "aiomysql": DATABASE,
    "pymongo": DATABASE,
    "redis": DATABASE,
    "cassandra": DATABASE,
    "pyodbc": DATABASE,
    "cx_Oracle": DATABASE,
    # Clock / randomness (non-determinism).
    "random": NONDETERMINISM,
    "time": NONDETERMINISM,
    "datetime": NONDETERMINISM,
    "secrets": NONDETERMINISM,
    "uuid": NONDETERMINISM,
}

#: Attribute names distinctive enough to flag by name alone, for the case where
#: the receiver does not resolve to a risky module (``from datetime import
#: datetime; datetime.now()`` — root ``datetime`` is a *class*, not the module).
#: Deliberately excludes ambiguous names that collide with token-payload / dict
#: methods (``.get``, ``.post``, ``.connect``, ``.execute``, ``.cursor``); those
#: rely on module resolution, which does not misfire on a plain dict.
_DISTINCTIVE_ATTRS: dict[str, str] = {
    "now": NONDETERMINISM,
    "utcnow": NONDETERMINISM,
    "today": NONDETERMINISM,
    "monotonic": NONDETERMINISM,
    "monotonic_ns": NONDETERMINISM,
    "perf_counter": NONDETERMINISM,
    "perf_counter_ns": NONDETERMINISM,
    "process_time": NONDETERMINISM,
    "gettimeofday": NONDETERMINISM,
    "urandom": NONDETERMINISM,
    "getrandbits": NONDETERMINISM,
    "getpid": NONDETERMINISM,
    "uuid1": NONDETERMINISM,
    "uuid4": NONDETERMINISM,
    "urlopen": NETWORK,
}

#: Human-readable rationale per category, appended to every finding message.
_WHY: dict[str, str] = {
    NETWORK: "network I/O makes the enabling condition depend on a remote service, "
    "so it is neither deterministic nor reproducible",
    DATABASE: "a database read makes the enabling condition depend on external mutable state, "
    "so reachability/liveness cannot be reasoned about statically",
    NONDETERMINISM: "reading the clock or a randomness source makes firing non-deterministic, "
    "so repeated runs need not converge to the same marking",
}


class CpnxLintWarning(UserWarning):
    """Advisory warning that a guard/key/filter contains a decidability trouble spot.

    A dedicated category so users can ``warnings.filterwarnings`` it independently —
    silence it, or escalate it to an error via ``filterwarnings("error", ...)`` for
    CI — without touching cpnx's other warnings.
    """


class SideEffectLintError(Exception):
    """Raised instead of warning when strict linting is enabled (see :func:`set_strict`)."""


@dataclass(frozen=True)
class LintFinding:
    """One flagged trouble spot in a callable's body.

    ``category`` is one of :data:`NETWORK`/:data:`DATABASE`/:data:`NONDETERMINISM`;
    ``symbol`` is the offending call as written (e.g. ``"requests.get"``,
    ``"datetime.now"``); ``lineno`` is the source line; ``message`` is the
    actionable, human-readable explanation.
    """

    category: str
    symbol: str
    lineno: int
    message: str


def _risk_category(obj: object) -> str | None:
    """Classify a resolved object by the top-level package that defines it.

    Uses a module's own ``__name__`` (``import requests`` -> module ``requests``)
    or any other object's ``__module__`` (``datetime.datetime`` -> ``"datetime"``),
    then maps the top-level package via :data:`_TOP_PACKAGE_CATEGORY`.
    """
    if obj is _UNRESOLVED or obj is None:
        return None
    modname = obj.__name__ if inspect.ismodule(obj) else getattr(obj, "__module__", None)
    if not isinstance(modname, str) or not modname:
        return None
    return _TOP_PACKAGE_CATEGORY.get(modname.split(".")[0])


def _symbol_text(callee: ast.expr) -> str:
    """Render a call's callee (``requests.get``, ``datetime.now``, ``time``) as source text."""
    try:
        return ast.unparse(callee)
    except Exception:  # pragma: no cover - unparse is total on valid nodes
        return _root_name(callee) or "<call>"


def _classify_call(node: ast.Call, func: Callable) -> tuple[str, str] | None:
    """Return ``(category, symbol)`` if this call is a trouble spot, else ``None``.

    Three strategies, cheapest-first, all best-effort:

    1. **Module resolution** — resolve the callee's root name against *func*'s
       closure/globals and classify by the defining package. Catches
       ``requests.get``, ``socket.socket``, ``sqlite3.connect``, ``random.random``,
       ``time.time`` regardless of import alias.
    2. **Distinctive attribute** — a ``.now``/``.urlopen``/``.urandom``-style name
       that is risky even when its receiver is a class rather than a module
       (``from datetime import datetime; datetime.now()``).
    3. **Bare-name resolution** — a plain ``Name`` callee (``get()`` after
       ``from requests import get``) resolved and classified like strategy 1.
    """
    callee = node.func

    # Strategy 1 + 3: resolve the root name and classify by defining package.
    root = _root_name(callee)
    if root is not None:
        category = _risk_category(_resolve_name(func, root))
        if category is not None:
            return category, _symbol_text(callee)

    # Strategy 2: distinctive attribute name (receiver need not resolve).
    if isinstance(callee, ast.Attribute):
        category = _DISTINCTIVE_ATTRS.get(callee.attr)
        if category is not None:
            return category, _symbol_text(callee)

    return None


def lint_callable(func: Callable) -> list[LintFinding]:
    """Scan *func*'s body for network / database / clock / randomness trouble spots.

    Pure inspection: never warns, never raises, never mutates. Returns findings in
    source order, de-duplicated by ``(category, symbol, lineno)``. Returns ``[]``
    when *func* is not callable or its source cannot be recovered (a compiled
    builtin, a REPL lambda) — an unseeable body is reported as "nothing found",
    never as a false positive, consistent with the best-effort contract.
    """
    if not callable(func):
        return []
    node = _get_ast_node(func)
    if node is None:
        return []

    findings: list[LintFinding] = []
    seen: set[tuple[str, str, int]] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        hit = _classify_call(child, func)
        if hit is None:
            continue
        category, symbol = hit
        lineno = getattr(child, "lineno", 0)
        key = (category, symbol, lineno)
        if key in seen:
            continue
        seen.add(key)
        findings.append(
            LintFinding(
                category=category,
                symbol=symbol,
                lineno=lineno,
                message=f"'{symbol}' looks like {category} ({_WHY[category]})",
            )
        )
    return findings


#: Process-wide strict flag. Seeded from ``CPNX_LINT_STRICT`` (``1``/``true``/``yes``,
#: case-insensitive) and overridable at runtime via :func:`set_strict`.
_STRICT: bool = os.environ.get("CPNX_LINT_STRICT", "").strip().lower() in {"1", "true", "yes", "on"}


def set_strict(strict: bool) -> None:
    """Promote lint findings from warnings to :class:`SideEffectLintError` (or back).

    Off by default (the honest severity for a best-effort heuristic — see the module
    docstring). Opt in per process for CI gating, or set ``CPNX_LINT_STRICT=1``.
    """
    global _STRICT
    _STRICT = strict


def is_strict() -> bool:
    """Whether strict linting is currently enabled."""
    return _STRICT


def lint_and_warn(func: Callable, role: str, *, stacklevel: int = 2) -> list[LintFinding]:
    """Lint *func* and surface any findings; the engine-facing entry point.

    Emits one :class:`CpnxLintWarning` per finding, prefixed with *role* (e.g.
    ``"guard"``, ``"InputArc.filter"``) so the message names the offending arc. In
    strict mode (:func:`set_strict`) the first finding raises
    :class:`SideEffectLintError` instead. Returns the findings so callers can also
    inspect them programmatically. Non-callables and unseeable bodies yield ``[]``.
    """
    findings = lint_callable(func)
    if not findings:
        return findings
    if _STRICT:
        first = findings[0]
        raise SideEffectLintError(
            f"{role}: {first.message} at line {first.lineno}. "
            "Guards/keys/filters must be deterministic and side-effect-free; move I/O into the "
            "transition's action, or close over a value read once at construction time."
        )
    for finding in findings:
        warnings.warn(
            f"{role}: {finding.message} at line {finding.lineno}. "
            "Guards/keys/filters should be deterministic and side-effect-free — this threatens "
            "the net's analysability. Move I/O into the transition's action if possible.",
            CpnxLintWarning,
            stacklevel=stacklevel,
        )
    return findings
