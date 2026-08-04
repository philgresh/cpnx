"""Unit tests for forward blast-radius tracing (:func:`cpnx.trace_impact`).

Covers the cone-of-influence forward walk over a net's static topology: linear
radius, fan-out, colour pruning (including the "consuming transition is impacted
even when its own outputs are all pruned" semantic), universal vs. overridden
colour domains, cycle safety, isolated seeds, unknown-name errors, routing
freshness across calls, edge de-duplication, and method/free-function parity.

Nets are built purely from structure (no scheduler is started) since the tracer
reads only the arc references.
"""

import pytest

from cpnx import (
    ImpactMap,
    InputArc,
    OutputArc,
    PetriNet,
    Place,
    SinkPlace,
    Transition,
    trace_impact,
)

# --- helpers -------------------------------------------------------------------------


def _passthrough(tokens):
    return list(tokens)


def _linear_chain():
    """P_A -> T1 -> P_B -> T2 -> P_C -> T3 -> P_D (all places colour-agnostic)."""
    net = PetriNet()
    for p in ("P_A", "P_B", "P_C", "P_D"):
        net.add_place(Place(p))
    net.add_transition(Transition("T1", [InputArc("P_A")], [OutputArc("P_B")], action=_passthrough))
    net.add_transition(Transition("T2", [InputArc("P_B")], [OutputArc("P_C")], action=_passthrough))
    net.add_transition(Transition("T3", [InputArc("P_C")], [OutputArc("P_D")], action=_passthrough))
    return net


# --- linear radius -------------------------------------------------------------------


def test_linear_chain_full_forward_radius():
    net = _linear_chain()
    impact = net.trace_impact("T1")
    # Every downstream place and consuming transition is reached from T1's output P_B.
    assert impact.places == {"P_B", "P_C", "P_D"}
    assert impact.transitions == {"T2", "T3"}


def test_origin_excluded_from_transitions():
    net = _linear_chain()
    impact = net.trace_impact("T1")
    assert impact.origin == "T1"
    assert "T1" not in impact.transitions


def test_mid_chain_seed_only_reaches_downstream():
    net = _linear_chain()
    impact = net.trace_impact("T2")
    # From T2's output P_C onward — upstream P_B / T1 are not in the forward cone.
    assert impact.places == {"P_C", "P_D"}
    assert impact.transitions == {"T3"}


# --- fan-out -------------------------------------------------------------------------


def test_branch_fanout_captures_both_consumers():
    net = PetriNet()
    for p in ("start", "hub", "left", "right"):
        net.add_place(Place(p))
    net.add_transition(Transition("seed", [InputArc("start")], [OutputArc("hub")], action=_passthrough))
    # Two transitions both consume from the hub place.
    net.add_transition(Transition("T_left", [InputArc("hub")], [OutputArc("left")], action=_passthrough))
    net.add_transition(Transition("T_right", [InputArc("hub")], [OutputArc("right")], action=_passthrough))

    impact = net.trace_impact("seed")
    assert impact.transitions == {"T_left", "T_right"}
    assert impact.places == {"hub", "left", "right"}


# --- colour pruning ------------------------------------------------------------------


def test_disjoint_place_and_its_subgraph_are_pruned():
    """A downstream place whose color_set is disjoint from the trace is excluded,
    and anything reachable ONLY through it is excluded too."""
    net = PetriNet()
    net.add_place(Place("src"))
    net.add_place(Place("keep", color_set={"order"}))
    net.add_place(Place("drop", color_set={"refund"}))
    net.add_place(Place("beyond_drop"))  # reachable only through the pruned place
    # Seed declares it impacts only the "order" colour.
    net.add_transition(
        Transition(
            "seed",
            [InputArc("src")],
            [OutputArc("keep"), OutputArc("drop")],
            action=_passthrough,
            impacts_colors={"order"},
        )
    )
    net.add_transition(Transition("T_drop", [InputArc("drop")], [OutputArc("beyond_drop")], action=_passthrough))

    impact = net.trace_impact("seed")
    assert "keep" in impact.places
    assert "drop" not in impact.places  # disjoint color_set -> pruned
    assert "beyond_drop" not in impact.places  # only reachable via the pruned place
    assert "T_drop" not in impact.transitions


def test_place_with_none_color_set_always_included_under_narrow_trace():
    net = PetriNet()
    net.add_place(Place("src"))
    net.add_place(Place("anycolor", color_set=None))  # accepts any colour
    net.add_transition(
        Transition(
            "seed",
            [InputArc("src")],
            [OutputArc("anycolor")],
            action=_passthrough,
            impacts_colors={"order"},  # narrow trace
        )
    )
    impact = net.trace_impact("seed")
    assert "anycolor" in impact.places  # color_set=None is never pruned


def test_consuming_transition_impacted_even_when_its_outputs_all_pruned():
    """Pin the semantic: a transition that consumes an impacted token is itself
    impacted, even if every one of its own output places is colour-pruned."""
    net = PetriNet()
    net.add_place(Place("src"))
    net.add_place(Place("mid", color_set={"order"}))  # in-domain, so impacted
    net.add_place(Place("out_pruned", color_set={"refund"}))  # disjoint -> pruned
    net.add_transition(
        Transition(
            "seed",
            [InputArc("src")],
            [OutputArc("mid")],
            action=_passthrough,
            impacts_colors={"order"},
        )
    )
    # Consumes the impacted "mid" but only outputs to a colour-pruned place.
    net.add_transition(Transition("consumer", [InputArc("mid")], [OutputArc("out_pruned")], action=_passthrough))

    impact = net.trace_impact("seed")
    assert "consumer" in impact.transitions  # consumed an impacted token
    assert "out_pruned" not in impact.places  # its own output is still pruned


# --- universal vs. explicit override -------------------------------------------------


def test_none_declaration_is_universal_no_pruning():
    net = PetriNet()
    net.add_place(Place("src"))
    net.add_place(Place("a", color_set={"order"}))
    net.add_place(Place("b", color_set={"refund"}))
    # impacts_colors defaults to None -> universal, nothing pruned.
    net.add_transition(
        Transition("seed", [InputArc("src")], [OutputArc("a"), OutputArc("b")], action=_passthrough)
    )
    impact = net.trace_impact("seed")
    assert impact.colors is None
    assert impact.places == {"a", "b"}


def test_explicit_colors_narrows_relative_to_declaration():
    net = PetriNet()
    net.add_place(Place("src"))
    net.add_place(Place("order_p", color_set={"order"}))
    net.add_place(Place("refund_p", color_set={"refund"}))
    net.add_transition(
        Transition(
            "seed",
            [InputArc("src")],
            [OutputArc("order_p"), OutputArc("refund_p")],
            action=_passthrough,
            impacts_colors={"order", "refund"},  # declared wide
        )
    )
    # Override narrows to just "order".
    impact = net.trace_impact("seed", colors={"order"})
    assert impact.colors == frozenset({"order"})
    assert impact.places == {"order_p"}
    assert "refund_p" not in impact.places


def test_explicit_colors_widens_relative_to_declaration():
    net = PetriNet()
    net.add_place(Place("src"))
    net.add_place(Place("order_p", color_set={"order"}))
    net.add_place(Place("refund_p", color_set={"refund"}))
    net.add_transition(
        Transition(
            "seed",
            [InputArc("src")],
            [OutputArc("order_p"), OutputArc("refund_p")],
            action=_passthrough,
            impacts_colors={"order"},  # declared narrow
        )
    )
    # Override widens to include refund too.
    impact = net.trace_impact("seed", colors={"order", "refund"})
    assert impact.places == {"order_p", "refund_p"}


def test_empty_colors_override_admits_only_none_color_set_places():
    net = PetriNet()
    net.add_place(Place("src"))
    net.add_place(Place("typed", color_set={"order"}))
    net.add_place(Place("anycolor", color_set=None))
    net.add_transition(
        Transition(
            "seed",
            [InputArc("src")],
            [OutputArc("typed"), OutputArc("anycolor")],
            action=_passthrough,
            impacts_colors={"order"},
        )
    )
    impact = net.trace_impact("seed", colors=set())
    assert impact.colors == frozenset()
    # Empty domain is disjoint from every typed place, but color_set=None still passes.
    assert impact.places == {"anycolor"}
    assert "typed" not in impact.places


def test_bare_string_colors_raises_type_error():
    net = _linear_chain()
    with pytest.raises(TypeError):
        net.trace_impact("T1", colors="order")


# --- cycles --------------------------------------------------------------------------


def test_cycle_terminates_and_lists_each_node_once():
    net = PetriNet()
    net.add_place(Place("P1"))
    net.add_place(Place("P2"))
    # T_a: P1 -> P2 ; T_b: P2 -> P1 (a 2-cycle).
    net.add_transition(Transition("T_a", [InputArc("P1")], [OutputArc("P2")], action=_passthrough))
    net.add_transition(Transition("T_b", [InputArc("P2")], [OutputArc("P1")], action=_passthrough))

    impact = net.trace_impact("T_a")
    # Walk terminates; the cycle brings us back to P1 and to T_a, but origin is excluded.
    assert impact.places == {"P1", "P2"}
    assert impact.transitions == {"T_b"}
    assert "T_a" not in impact.transitions


# --- isolated seeds ------------------------------------------------------------------


def test_seed_with_no_outputs_has_empty_radius():
    net = PetriNet()
    net.add_place(Place("only_in"))
    net.add_transition(Transition("seed", [InputArc("only_in")], [], action=_passthrough))
    impact = net.trace_impact("seed")
    assert impact.places == frozenset()
    assert impact.transitions == frozenset()
    assert impact.edges == ()


def test_seed_outputs_to_sink_with_no_consumers():
    net = PetriNet()
    net.add_place(Place("src"))
    net.add_place(SinkPlace("terminal"))
    net.add_transition(Transition("seed", [InputArc("src")], [OutputArc("terminal")], action=_passthrough))
    impact = net.trace_impact("seed")
    # The sink place itself is reached, but nothing consumes from it.
    assert impact.places == {"terminal"}
    assert impact.transitions == frozenset()


# --- error handling ------------------------------------------------------------------


def test_unknown_transition_raises_key_error():
    net = _linear_chain()
    with pytest.raises(KeyError):
        net.trace_impact("does_not_exist")


# --- routing freshness ---------------------------------------------------------------


def test_routing_rebuilt_each_call_picks_up_new_consumers():
    net = _linear_chain()
    first = net.trace_impact("T1")
    assert "T_new" not in first.transitions

    # Add a brand-new transition that consumes from a downstream place (P_D).
    net.add_place(Place("P_E"))
    net.add_transition(Transition("T_new", [InputArc("P_D")], [OutputArc("P_E")], action=_passthrough))

    second = net.trace_impact("T1")
    assert "T_new" in second.transitions
    assert "P_E" in second.places


# --- edges ---------------------------------------------------------------------------


def test_edges_contain_expected_hops_and_are_deduplicated():
    net = _linear_chain()
    impact = net.trace_impact("T1")
    edges = impact.edges
    # Expected place->transition and transition->place hops.
    assert ("T1", "P_B") in edges
    assert ("P_B", "T2") in edges
    assert ("T2", "P_C") in edges
    assert ("P_C", "T3") in edges
    assert ("T3", "P_D") in edges
    # No duplicate hops.
    assert len(edges) == len(set(edges))


def test_edges_deduplicated_when_two_transitions_feed_one_place():
    net = PetriNet()
    net.add_place(Place("src"))
    net.add_place(Place("hub"))
    net.add_place(Place("shared"))
    net.add_transition(Transition("seed", [InputArc("src")], [OutputArc("hub")], action=_passthrough))
    # Two consumers of hub, both producing into the same "shared" place.
    net.add_transition(Transition("c1", [InputArc("hub")], [OutputArc("shared")], action=_passthrough))
    net.add_transition(Transition("c2", [InputArc("hub")], [OutputArc("shared")], action=_passthrough))
    impact = net.trace_impact("seed")
    assert ("c1", "shared") in impact.edges
    assert ("c2", "shared") in impact.edges
    assert len(impact.edges) == len(set(impact.edges))


# --- parity: method vs. free function ------------------------------------------------


def test_method_and_module_function_agree():
    net = _linear_chain()
    via_method = net.trace_impact("T1")
    via_function = trace_impact(net, "T1")
    assert isinstance(via_method, ImpactMap)
    assert isinstance(via_function, ImpactMap)
    assert via_method == via_function
