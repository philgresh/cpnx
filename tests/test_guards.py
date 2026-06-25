import time

from petriq.engine import PetriNet
from petriq.places import Place
from petriq.tokens import Token
from petriq.transitions import InputArc, OutputArc, Transition


def test_transition_guard():
    net = PetriNet(max_workers=2)
    net.add_place(Place("input"))
    net.add_place(Place("output"))

    guard_value = False

    def check_guard():
        return guard_value

    def action(tokens):
        return tokens

    net.add_transition(
        Transition(
            name="t1",
            inputs=[InputArc("input")],
            outputs=[OutputArc("output")],
            action=action,
            guard=check_guard,
        )
    )

    # Deposit a token
    net.deposit("input", Token())

    # With guard = False, transition should not fire
    fired = net.step()
    assert not fired
    assert len(net.places["output"].tokens) == 0

    # Toggle guard to True
    guard_value = True

    # Now it should fire
    fired = net.step()
    assert fired

    net.run(deadline=time.monotonic() + 1.0)
    assert len(net.places["output"].tokens) == 1
