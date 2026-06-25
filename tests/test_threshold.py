import time

from petriq.engine import PetriNet
from petriq.places import Place, ThresholdPlace
from petriq.tokens import Token
from petriq.transitions import InputArc, OutputArc, Transition


def test_threshold_place_firing():
    net = PetriNet(max_workers=2)
    # Threshold is 5
    net.add_place(ThresholdPlace("input", threshold=5))
    net.add_place(Place("output"))

    def action(tokens):
        return tokens

    net.add_transition(
        Transition(
            name="t1",
            inputs=[InputArc("input", count=5)],
            outputs=[OutputArc("output", count=5)],
            action=action,
        )
    )

    # Deposit 4 tokens
    for _ in range(4):
        net.deposit("input", Token())

    # Step the net, should return False because the threshold is not met
    fired = net.step()
    assert not fired
    assert len(net.places["output"].tokens) == 0

    # Deposit 1 more token to meet threshold
    net.deposit("input", Token())

    # Step the net, should fire now
    fired = net.step()
    assert fired

    # Wait for the transition to finish
    net.run(deadline=time.monotonic() + 1.0)
    assert len(net.places["output"].tokens) == 5
