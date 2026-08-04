"""Unit tests for blast-radius highlighting in ``PetriNet.to_dot``.

A NEW file (the existing ``tests/test_visualization.py`` is left untouched). Pins
that ``to_dot(highlight_impact_from=...)`` fills impacted place/transition nodes,
gives the origin the origin fill (``#ff8080``), leaves a plain ``to_dot()`` free of
any ``fillcolor`` (backward compatible), and raises ``KeyError`` on an unknown seed.
"""

import pytest

from cpnx import InputArc, OutputArc, PetriNet, Place, Transition

_IMPACT_FILL = "#ffd9d9"
_ORIGIN_FILL = "#ff8080"


def _passthrough(tokens):
    return list(tokens)


def _chain_net():
    """P_A -> T1 -> P_B -> T2 -> P_C."""
    net = PetriNet()
    for p in ("P_A", "P_B", "P_C"):
        net.add_place(Place(p))
    net.add_transition(Transition("T1", [InputArc("P_A")], [OutputArc("P_B")], action=_passthrough))
    net.add_transition(Transition("T2", [InputArc("P_B")], [OutputArc("P_C")], action=_passthrough))
    return net


def test_highlight_fills_impacted_place_and_transition_nodes():
    net = _chain_net()
    dot = net.to_dot(highlight_impact_from="T1")
    assert "fillcolor" in dot
    assert "style=filled" in dot
    # The impacted (non-origin) place/transition get the standard impact fill.
    assert _IMPACT_FILL in dot


def _node_line(dot, name):
    """The single DOT node-definition line for ``name`` (excludes edge lines)."""
    lines = [ln for ln in dot.splitlines() if ln.strip().startswith(f'"{name}" [shape=')]
    assert len(lines) == 1
    return lines[0]


def test_highlight_gives_origin_the_origin_fill():
    net = _chain_net()
    dot = net.to_dot(highlight_impact_from="T1")
    # The seed transition's node line carries the origin fill and heavier border.
    origin_lines = [_node_line(dot, "T1")]
    assert _ORIGIN_FILL in origin_lines[0]
    assert "penwidth=2" in origin_lines[0]


def test_downstream_nodes_get_impact_fill_not_origin_fill():
    net = _chain_net()
    dot = net.to_dot(highlight_impact_from="T1")
    # An impacted downstream place (P_B) is filled with the impact colour, not origin.
    pb_line = _node_line(dot, "P_B")
    assert _IMPACT_FILL in pb_line
    assert _ORIGIN_FILL not in pb_line


def test_plain_to_dot_has_no_impact_shading():
    net = _chain_net()
    dot = net.to_dot()
    # Backward compatible: no *blast-radius* shading is added when highlighting is off.
    # (Role styling — resource/source/error fills — is independent of the impact overlay.)
    assert _IMPACT_FILL not in dot
    assert _ORIGIN_FILL not in dot
    assert "penwidth=2" not in dot


def test_unknown_seed_raises_key_error():
    net = _chain_net()
    with pytest.raises(KeyError):
        net.to_dot(highlight_impact_from="nope")


def test_pruned_place_not_filled():
    """A colour-pruned place stays unshaded under a narrow highlight."""
    net = PetriNet()
    net.add_place(Place("src"))
    net.add_place(Place("kept", color_set={"order"}))
    net.add_place(Place("dropped", color_set={"refund"}))
    net.add_transition(
        Transition(
            "seed",
            [InputArc("src")],
            [OutputArc("kept"), OutputArc("dropped")],
            action=_passthrough,
            impacts_colors={"order"},
        )
    )
    dot = net.to_dot(highlight_impact_from="seed")
    kept_line = _node_line(dot, "kept")
    dropped_line = _node_line(dot, "dropped")
    assert "fillcolor" in kept_line
    assert "fillcolor" not in dropped_line
