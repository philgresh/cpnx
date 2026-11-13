"""Fixtures shared by the callable-lookup tests."""

import pytest

import cpnx.sandbox


def forget_columns(monkeypatch):
    """Make the lookup see code as an interpreter without column positions does (3.10, `-X no_debug_ranges`).

    Both the running callable's position (`_body_anchor`) and those of the code its source
    compiles to (`_code_anchor`) go, so the lookup takes the same path it does there.
    """
    monkeypatch.setattr(cpnx.sandbox, "_body_anchor", lambda func: None)
    monkeypatch.setattr(cpnx.sandbox, "_code_anchor", lambda code: None)
    cpnx.sandbox._SOURCE_CACHE.clear()


@pytest.fixture
def no_columns(monkeypatch):
    """Check as an interpreter without column positions does."""
    forget_columns(monkeypatch)
    yield
    cpnx.sandbox._SOURCE_CACHE.clear()


@pytest.fixture(params=["columns as the interpreter records them", "no columns"])
def either_way(request, monkeypatch):
    """Run a test on both lookups: what must hold whether or not there are columns."""
    cpnx.sandbox._SOURCE_CACHE.clear()
    if request.param == "no columns":
        forget_columns(monkeypatch)
    yield
    cpnx.sandbox._SOURCE_CACHE.clear()
