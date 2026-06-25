import time

from petriq.engine import PetriNet
from petriq.places import Place
from petriq.tokens import Token
from petriq.transitions import InputArc, OutputArc, SubstitutionTransition, Transition


def test_substitution_transition_execution():
    # 1. Create a child subnet that simply passes tokens through
    child = PetriNet()
    child.add_place(Place("child_port_in"))
    child.add_place(Place("child_port_out"))
    child.add_transition(
        Transition(
            "child_t",
            [InputArc("child_port_in")],
            [OutputArc("child_port_out")],
            action=lambda tokens: tokens,
        )
    )

    # 2. Create parent net with SubstitutionTransition
    parent = PetriNet()
    parent.add_place(Place("parent_socket_in"))
    parent.add_place(Place("parent_socket_out"))

    sub_t = SubstitutionTransition(
        name="sub_net_transition",
        inputs=[InputArc("parent_socket_in")],
        outputs=[OutputArc("parent_socket_out")],
        action=None,  # type: ignore[assignment]
        subnet=child,
        port_socket_map={
            "child_port_in": "parent_socket_in",
            "child_port_out": "parent_socket_out",
        },
    )
    parent.add_transition(sub_t)

    # 3. Deposit a token in parent, step, and verify child runs and token reaches parent output
    parent.deposit("parent_socket_in", Token(payload={"val": 42}))
    assert parent.step() is True

    # Wait for the thread pool to execute the parent transition
    parent.run(deadline=time.monotonic() + 2.0)

    # Verify that the token traversed the child net and returned to the parent socket
    out_tokens = parent.places["parent_socket_out"].tokens
    assert len(out_tokens) == 1
    assert out_tokens[0].payload["val"] == 42


def test_strict_port_socket_boundary_violation():
    child = PetriNet()
    # Mapped ports
    child.add_place(Place("child_port_in"))

    parent = PetriNet()
    parent.add_place(Place("parent_socket_in"))
    parent.add_place(Place("unmapped_place"))

    # We map child_port_in -> parent_socket_in. But parent input arc reads from unmapped_place!
    sub_t = SubstitutionTransition(
        name="sub_net_transition",
        inputs=[InputArc("unmapped_place")],
        outputs=[],
        action=None,  # type: ignore[assignment]
        subnet=child,
        port_socket_map={
            "child_port_in": "parent_socket_in",
        },
    )
    parent.add_transition(sub_t)

    parent.deposit("unmapped_place", Token())

    # Try to step, which will consume tokens and run _execute_substitution_transition
    assert parent.step() is True

    # Wait for parent execution. The transition execution should fail and route the token to error place
    parent.run(deadline=time.monotonic() + 1.0)

    # Token should be routed to failed (error_place) due to boundary violation
    failed_tokens = parent.places["failed"].tokens
    assert len(failed_tokens) == 1


def test_substitution_transition_requires_predeclared_ports():
    import pytest

    child = PetriNet()
    # child_port_in is missing from child places!

    with pytest.raises(ValueError, match="subnet has no places for ports"):
        SubstitutionTransition(
            name="sub_net_transition",
            inputs=[InputArc("parent_socket_in")],
            outputs=[],
            action=None,  # type: ignore[assignment]
            subnet=child,
            port_socket_map={
                "child_port_in": "parent_socket_in",
            },
        )
