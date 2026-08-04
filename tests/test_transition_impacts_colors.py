"""Unit tests for the ``Transition.impacts_colors`` field and ``coerce_color_domain``.

The field is coerced in ``Transition.__setattr__`` via
:func:`cpnx.analysis.coerce_color_domain`, so both are exercised for the same
shapes: an iterable of str becomes a ``frozenset``; ``None`` stays ``None``; a bare
``str`` raises; a non-``str`` member raises; and an empty iterable yields an empty
``frozenset`` (a *narrowing* declaration, distinct from ``None``).
"""

import pytest

from cpnx import InputArc, OutputArc, Transition
from cpnx.analysis import coerce_color_domain


def _passthrough(tokens):
    return list(tokens)


def _make(impacts_colors):
    return Transition(
        name="t",
        inputs=[InputArc("in")],
        outputs=[OutputArc("out")],
        action=_passthrough,
        impacts_colors=impacts_colors,
    )


# --- Transition.impacts_colors field validation --------------------------------------


def test_set_is_stored_as_frozenset():
    t = _make({"a", "b"})
    assert t.impacts_colors == frozenset({"a", "b"})
    assert isinstance(t.impacts_colors, frozenset)


def test_default_is_none():
    t = Transition(name="t", inputs=[InputArc("in")], outputs=[OutputArc("out")], action=_passthrough)
    assert t.impacts_colors is None


def test_explicit_none_stays_none():
    t = _make(None)
    assert t.impacts_colors is None


def test_empty_iterable_becomes_empty_frozenset():
    t = _make([])
    assert t.impacts_colors == frozenset()
    assert isinstance(t.impacts_colors, frozenset)
    # Distinct from None: an empty declaration is a narrowing, not "unspecified".
    assert t.impacts_colors is not None


def test_bare_string_raises_type_error():
    with pytest.raises(TypeError):
        _make("data")


def test_non_str_member_raises_type_error():
    with pytest.raises(TypeError):
        _make({1})


def test_other_iterables_are_accepted():
    # A list, tuple, or generator of str all coerce to the same frozenset.
    assert _make(["a", "b"]).impacts_colors == frozenset({"a", "b"})
    assert _make(("a", "b")).impacts_colors == frozenset({"a", "b"})
    assert _make(iter(["a", "b"])).impacts_colors == frozenset({"a", "b"})


def test_post_construction_reassignment_is_coerced():
    t = _make({"a"})
    t.impacts_colors = ["b", "c"]
    assert t.impacts_colors == frozenset({"b", "c"})
    with pytest.raises(TypeError):
        t.impacts_colors = "b"  # bare string still rejected on reassignment


# --- coerce_color_domain directly ----------------------------------------------------


def test_coerce_none_returns_none():
    assert coerce_color_domain(None) is None


def test_coerce_iterable_returns_frozenset():
    result = coerce_color_domain({"x", "y"})
    assert result == frozenset({"x", "y"})
    assert isinstance(result, frozenset)


def test_coerce_empty_iterable_is_empty_frozenset():
    result = coerce_color_domain([])
    assert result == frozenset()
    assert result is not None


def test_coerce_bare_string_raises():
    with pytest.raises(TypeError):
        coerce_color_domain("data")


def test_coerce_non_str_member_raises():
    with pytest.raises(TypeError):
        coerce_color_domain({1})


def test_coerce_non_iterable_raises():
    with pytest.raises(TypeError):
        coerce_color_domain(42)


def test_coerce_field_name_appears_in_error_message():
    with pytest.raises(TypeError, match="my_field"):
        coerce_color_domain("data", field="my_field")
