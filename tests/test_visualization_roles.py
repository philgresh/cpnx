"""``to_dot`` renders place *roles* distinctly: resource pools, external sources,
and implicit permit-returns.

These are the roles whose tokens do NOT arrive through a drawn input arc — resource
pools are seeded at construction and cycle permits; external sources (arc in-degree
0) are fed by ``deposit(...)``; and a resource borrow without a matching output arc
is returned implicitly by the engine. Each gets a distinct rendering so a reader is
not left wondering how a "sourceless" place fills.
"""

from cpnx import InputArc, OutputArc, PetriNet, Place, ResourcePlace, Transition


def _passthrough(tokens):
    return list(tokens)


def _node_line(dot: str, name: str) -> str:
    lines = [ln for ln in dot.splitlines() if ln.strip().startswith(f'"{name}" [shape=')]
    assert len(lines) == 1, f"expected one node line for {name!r}, got {len(lines)}"
    return lines[0]


def test_resource_pool_is_double_circle_and_filled():
    net = PetriNet()
    net.add_place(ResourcePlace("pool", capacity=2))
    net.add_place(Place("out"))
    # A proper self-loop: borrow a permit and return it.
    net.add_place(Place("in"))
    net.add_transition(
        Transition(
            "use",
            [InputArc("in"), InputArc("pool")],
            [OutputArc("out"), OutputArc("pool")],
            action=_passthrough,
        )
    )
    line = _node_line(net.to_dot(), "pool")
    assert "shape=doublecircle" in line
    assert 'fillcolor="#dbe9ff"' in line
    assert "double circle = resource pool" in net.to_dot()  # legend present


def test_external_source_place_is_dashed():
    net = PetriNet()
    net.add_place(Place("entry"))  # nothing produces into it → external source
    net.add_place(Place("done"))
    net.add_transition(Transition("t", [InputArc("entry")], [OutputArc("done")], action=_passthrough))
    dot = net.to_dot()
    entry_line = _node_line(dot, "entry")
    done_line = _node_line(dot, "done")
    assert "dashed" in entry_line  # in-degree 0 → dashed
    assert "dashed" not in done_line  # produced by t → ordinary circle
    assert "dashed circle = external source" in dot


def test_implicit_resource_return_drawn_as_dashed_edge():
    """A borrow left on the implicit path (synthesis opted out) gets a dashed 'return (implicit)' edge.

    By default the engine now synthesizes the return arc (a real self-loop), so the dashed
    edge appears only when a transition opts out via ``auto_return_resources=False`` (or uses
    a ``consume_all`` borrow). See ADR 0009.
    """
    net = PetriNet()
    net.add_place(ResourcePlace("permits", capacity=1))
    net.add_place(Place("in"))
    net.add_place(Place("out"))
    net.add_transition(
        # Borrows 'permits' but never outputs it back, and opts out of synthesis → the engine
        # returns it implicitly (off-arc).
        Transition(
            "borrow",
            [InputArc("in"), InputArc("permits")],
            [OutputArc("out")],
            action=_passthrough,
            auto_return_resources=False,
        )
    )
    dot = net.to_dot()
    assert '"borrow" -> "permits" [label="return (implicit)", style=dashed];' in dot
    assert "dashed edge = implicit permit return" in dot


def test_self_loop_has_no_implicit_return_edge():
    """When the return IS a structural self-loop, no implicit-return edge is drawn."""
    net = PetriNet()
    net.add_place(ResourcePlace("permits", capacity=1))
    net.add_place(Place("in"))
    net.add_place(Place("out"))
    net.add_transition(
        Transition(
            "borrow",
            [InputArc("in"), InputArc("permits")],
            [OutputArc("out"), OutputArc("permits")],  # explicit return
            action=_passthrough,
        )
    )
    dot = net.to_dot()
    assert "return (implicit)" not in dot
    assert "implicit permit return" not in dot  # legend omits the absent encoding


def test_error_place_is_a_salmon_well_not_a_source():
    """The engine's dead-letter ``error_place`` is a salmon radial well, not a source."""
    net = PetriNet(error_place="failed")  # auto-created plain Place, arc-sourceless
    net.add_place(Place("a"))
    net.add_place(Place("b"))
    net.add_transition(Transition("t", [InputArc("b")], [OutputArc("a")], action=_passthrough))
    net.add_transition(Transition("t2", [InputArc("a")], [OutputArc("b")], action=_passthrough))
    dot = net.to_dot()
    failed_line = _node_line(dot, "failed")
    assert "#ffe0e0" in failed_line and 'style="radial"' in failed_line  # salmon inset well
    assert "dashed" not in failed_line  # NOT flagged as an external source
    assert "salmon well = error place (dead-letter target)" in dot


def test_dead_letter_path_connects_the_error_place():
    """A finite-max_retries transition draws a dashed 'dead-letter' edge to the error place."""
    net = PetriNet(error_place="failed")
    net.add_place(Place("a"))
    net.add_place(Place("b"))
    # Default max_retries is finite (5), so both transitions can dead-letter.
    net.add_transition(Transition("t", [InputArc("b")], [OutputArc("a")], action=_passthrough))
    net.add_transition(Transition("t2", [InputArc("a")], [OutputArc("b")], action=_passthrough))
    dot = net.to_dot()
    # Pale, thin, unlabelled side channel; named once in the legend.
    assert '"t" -> "failed" [style=dashed, color="#e08a8a"' in dot
    assert "constraint=true, weight=0" in dot  # ranked (so the bin sinks) but weightless (no warp)
    assert "dashed red = dead-letter path" in dot


def _rank_line(dot: str) -> str:
    return next((line for line in dot.splitlines() if "rank=sink" in line), "")


def test_terminal_place_pinned_to_sink_rank():
    """A place with incoming tokens but no outgoing arc is pinned to the sink rank (far right)."""
    net = PetriNet()
    net.add_place(Place("in"))
    net.add_place(Place("out"))  # produced by t, consumed by nobody → terminal
    net.add_transition(Transition("t", [InputArc("in")], [OutputArc("out")], action=_passthrough))
    rank = _rank_line(net.to_dot())
    assert '"out";' in rank


def test_dead_letter_bin_is_ranked_terminal():
    """The error place is a terminal (nothing consumes from it), so it is pinned to the sink
    rank and lands at the far right rather than floating at rank 0 beside the sources."""
    net = PetriNet(error_place="failed")
    net.add_place(Place("a"))
    net.add_place(Place("b"))
    net.add_transition(Transition("t", [InputArc("a")], [OutputArc("b")], action=_passthrough))
    rank = _rank_line(net.to_dot())
    assert '"failed";' in rank


def test_source_and_intermediate_places_are_not_sunk():
    """Only true terminals are sunk: a never-produced source and a consumed intermediate stay put."""
    net = PetriNet()
    net.add_place(Place("src"))
    net.add_place(Place("mid"))
    net.add_place(Place("dst"))
    net.add_transition(Transition("t1", [InputArc("src")], [OutputArc("mid")], action=_passthrough))
    net.add_transition(Transition("t2", [InputArc("mid")], [OutputArc("dst")], action=_passthrough))
    rank = _rank_line(net.to_dot())
    assert '"dst";' in rank  # terminal
    assert '"mid";' not in rank  # consumed by t2 → not terminal
    assert '"src";' not in rank  # never produced → a source, not terminal


def test_infinite_retry_transition_has_no_dead_letter_edge():
    """A transition with max_retries=None never dead-letters, so no edge is drawn from it."""
    net = PetriNet(error_place="failed")
    net.add_place(Place("a"))
    net.add_place(Place("b"))
    net.add_transition(Transition("t", [InputArc("b")], [OutputArc("a")], action=_passthrough, max_retries=None))
    dot = net.to_dot()
    assert '"t" -> "failed"' not in dot


def test_sink_place_is_a_radial_well():
    """A non-error ``SinkPlace`` renders as a neutral radial well (terminal drain)."""
    from cpnx import SinkPlace

    net = PetriNet(error_place="failed")
    net.add_place(Place("src"))
    net.add_place(SinkPlace("done"))
    net.add_transition(Transition("t", [InputArc("src")], [OutputArc("done")], action=_passthrough))
    dot = net.to_dot()
    done_line = _node_line(dot, "done")
    assert 'style="radial"' in done_line
    assert "#f5f5f5" in done_line  # neutral well gradient
    assert "well (radial fill) = sink / terminal" in dot
