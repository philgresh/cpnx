"""Unit tests for the best-effort side-effect linter (:mod:`cpnx.linting`).

Covers the three trouble-spot categories (network / database / non-determinism),
resolution robustness (import aliases, class-vs-module receivers, bare names),
absence of false positives on legitimate token-payload access, and the
warn-vs-strict-raise wiring at construction time.

Only the standard library is used for the "risky" symbols (``urllib``, ``socket``,
``sqlite3``, ``time``, ``datetime``, ``random``, ``secrets``, ``uuid``) so the
suite needs no third-party network/DB drivers installed.
"""

import datetime as datetime_mod
import random
import secrets
import socket
import sqlite3
import time
import urllib.request
import uuid
import warnings
from datetime import datetime as DatetimeClass

import pytest

from cpnx import InputArc, OutputArc, Token, Transition
from cpnx.linting import (
    DATABASE,
    NETWORK,
    NONDETERMINISM,
    CpnxLintWarning,
    SideEffectLintError,
    is_strict,
    lint_and_warn,
    lint_callable,
    set_strict,
)

_aliased_socket = socket  # exercises alias resolution (bound name != module source text)


# --- module-level callables: the linter needs a recoverable source file --------------


def guard_network_urllib(tokens):
    return bool(urllib.request.urlopen("http://example.com"))


def guard_network_socket_alias(tokens):
    return bool(_aliased_socket.socket())


def guard_database_sqlite(tokens):
    return bool(sqlite3.connect(":memory:").execute("SELECT 1"))


def guard_clock_time(tokens):
    return time.time() > 0


def guard_clock_datetime_module(tokens):
    return datetime_mod.datetime.now().hour > 0


def guard_clock_datetime_class(tokens):
    # Root name `DatetimeClass` is a *class*, not a module: exercises the
    # distinctive-attribute (`.now`) fallback path.
    return DatetimeClass.now().hour > 0


def guard_random(tokens):
    return random.random() > 0.5


def guard_uuid_random(tokens):
    return uuid.uuid4().int % 2 == 0


def guard_secrets_random(tokens):
    return secrets.randbelow(100) > 50


def key_clean(tokens):
    return tokens[0].payload.get("priority", 0)


def guard_clean(tokens):
    return tokens[0].payload.get("weight", 0) > 5 and len(tokens) > 0


# Deterministic members of otherwise "risky" packages — MUST NOT be flagged. Each is the
# kind of thing a legitimate guard does: compare a token timestamp against a fixed window,
# parse a token's UUID, order by a fixed date (regression for the strict-mode false
# positive — see tests/test_linting.py history and PR #49 review).


def guard_timedelta(tokens):
    return tokens[0].created_at < datetime_mod.timedelta(hours=1).total_seconds()


def guard_fixed_uuid(tokens):
    return uuid.UUID("12345678-1234-5678-1234-567812345678").version == 4


def guard_fixed_date(tokens):
    return datetime_mod.date(2020, 1, 1) < datetime_mod.date(2021, 1, 1)


def guard_strftime_of_arg(tokens):
    # localtime/strftime of a *token-supplied* timestamp is deterministic (no clock read).
    return time.strftime("%Y", time.localtime(tokens[0].created_at)).startswith("20")


# --- lint_callable: detection --------------------------------------------------------


@pytest.mark.parametrize(
    ("func", "category", "symbol_contains"),
    [
        (guard_network_urllib, NETWORK, "urlopen"),
        (guard_network_socket_alias, NETWORK, "socket"),
        (guard_database_sqlite, DATABASE, "sqlite3.connect"),
        (guard_clock_time, NONDETERMINISM, "time.time"),
        (guard_clock_datetime_module, NONDETERMINISM, "now"),
        (guard_clock_datetime_class, NONDETERMINISM, "now"),
        (guard_random, NONDETERMINISM, "random"),
        (guard_uuid_random, NONDETERMINISM, "uuid4"),
        (guard_secrets_random, NONDETERMINISM, "randbelow"),
    ],
)
def test_flags_trouble_spot(func, category, symbol_contains):
    findings = lint_callable(func)
    assert findings, f"expected {func.__name__} to be flagged"
    assert findings[0].category == category
    assert symbol_contains in findings[0].symbol
    assert findings[0].lineno > 0
    assert category in findings[0].message


@pytest.mark.parametrize(
    "func",
    [
        key_clean,
        guard_clean,
        # Deterministic members of "risky" packages — the strict-mode false-positive fix.
        guard_timedelta,
        guard_fixed_uuid,
        guard_fixed_date,
        guard_strftime_of_arg,
    ],
)
def test_clean_callable_has_no_findings(func):
    assert lint_callable(func) == []


def test_deterministic_members_do_not_raise_in_strict_mode():
    """A deterministic ``datetime``/``uuid`` member must not break construction under strict.

    Regression for the review finding: blanket top-package classification used to flag
    ``datetime.timedelta``/``uuid.UUID(...)`` and, with CPNX_LINT_STRICT, raise on valid nets.
    """
    set_strict(True)
    try:
        assert lint_and_warn(guard_timedelta, "guard") == []
        assert lint_and_warn(guard_fixed_uuid, "key") == []
    finally:
        set_strict(False)


def test_dict_get_is_not_a_false_positive():
    # `.get` on a token payload must never be mistaken for `requests.get`.
    assert lint_callable(lambda tokens: tokens[0].payload.get("x")) == []


def test_findings_are_deduplicated():
    def repeats(tokens):
        return time.time() > 0 and time.time() < 1e12  # noqa: PLR2004 - same call, one line

    findings = lint_callable(repeats)
    # Two calls on one line collapse to a single (category, symbol, lineno) finding.
    assert len(findings) == 1


def test_non_callable_and_unseeable_source_return_empty():
    assert lint_callable(None) == []  # type: ignore[arg-type]
    assert lint_callable(len) == []  # builtin: no recoverable source


# --- lint_and_warn: warn vs strict ---------------------------------------------------


def test_lint_and_warn_emits_one_warning_per_finding():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        lint_and_warn(guard_clock_time, "guard")
    assert len(caught) == 1
    assert issubclass(caught[0].category, CpnxLintWarning)
    assert "guard" in str(caught[0].message)


def test_lint_and_warn_is_silent_for_clean_callable():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = lint_and_warn(guard_clean, "guard")
    assert caught == []
    assert result == []


def test_strict_mode_raises_instead_of_warning():
    set_strict(True)
    try:
        assert is_strict() is True
        with pytest.raises(SideEffectLintError, match="guard"):
            lint_and_warn(guard_random, "guard")
    finally:
        set_strict(False)
    assert is_strict() is False


# --- construction-time wiring --------------------------------------------------------


def _action(tokens):
    return list(tokens)


def test_transition_guard_warns_at_construction():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        Transition(
            name="t",
            inputs=[InputArc("in")],
            outputs=[OutputArc("out")],
            action=_action,
            guard=guard_clock_time,
        )
    assert any(issubclass(w.category, CpnxLintWarning) for w in caught)


def test_input_arc_filter_and_key_warn_at_construction():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        InputArc("in", filter=guard_random)
    assert any(issubclass(w.category, CpnxLintWarning) for w in caught)


def test_clean_construction_emits_no_lint_warning():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        Transition(
            name="t",
            inputs=[InputArc("in", key=key_clean)],
            outputs=[OutputArc("out")],
            action=_action,
            guard=guard_clean,
        )
    assert [w for w in caught if issubclass(w.category, CpnxLintWarning)] == []


def test_lint_warning_is_independently_filterable():
    # Users can silence just the lint category without affecting engine behaviour.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        warnings.filterwarnings("ignore", category=CpnxLintWarning)
        # `time.time` is flagged by the linter but NOT hard-blocked by the sandbox
        # purity gate — exactly the coverage this advisory layer adds on top.
        t = Transition(
            name="t",
            inputs=[InputArc("in")],
            outputs=[OutputArc("out")],
            action=_action,
            guard=guard_clock_time,
        )
    assert [w for w in caught if issubclass(w.category, CpnxLintWarning)] == []
    # Guard still installed and functional despite being flagged.
    assert t.guard is guard_clock_time


def test_token_payload_reference_is_unused_but_keeps_imports_live():
    # Guard against lint tooling pruning the Token import; also a trivial smoke check.
    assert Token(color="x").color == "x"
