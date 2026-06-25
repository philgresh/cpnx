import time

from petriq.engine import PetriNet
from petriq.places import Place
from petriq.tokens import Token
from petriq.transitions import InputArc, OutputArc, Transition


def test_priority_selection():
    # Use max_workers=1 for strict sequencing
    net = PetriNet(max_workers=1)
    net.add_place(Place("input"))
    net.add_place(Place("output_high"))
    net.add_place(Place("output_low"))

    # Both transitions consume from "input". Only one token is deposited, so only one can fire.
    net.add_transition(
        Transition(
            name="low_priority",
            inputs=[InputArc("input")],
            outputs=[OutputArc("output_low")],
            action=lambda tokens: tokens,
            priority=20,
        )
    )

    net.add_transition(
        Transition(
            name="high_priority",
            inputs=[InputArc("input")],
            outputs=[OutputArc("output_high")],
            action=lambda tokens: tokens,
            priority=5,
        )
    )

    net.deposit("input", Token())

    # Trigger a step - the high priority transition should be chosen
    fired = net.step()
    assert fired

    net.run(deadline=time.monotonic() + 1.0)

    assert len(net.places["output_high"].tokens) == 1
    assert len(net.places["output_low"].tokens) == 0
