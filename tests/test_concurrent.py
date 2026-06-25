import time

from petriq.engine import PetriNet
from petriq.places import Place
from petriq.tokens import Token
from petriq.transitions import InputArc, OutputArc, Transition


def test_concurrent_stress():
    # 8 workers, 100 tokens
    net = PetriNet(max_workers=8)
    net.add_place(Place("input"))
    net.add_place(Place("output"))

    def action(tokens):
        # Simulate some quick work
        time.sleep(0.002)
        return tokens

    net.add_transition(
        Transition(
            name="process",
            inputs=[InputArc("input")],
            outputs=[OutputArc("output")],
            action=action,
        )
    )

    # Deposit 100 tokens
    for i in range(100):
        net.deposit("input", Token(payload={"idx": i}))

    # Run the net
    net.run(deadline=time.monotonic() + 10.0)

    # Validate that all 100 tokens reached the output
    assert len(net.places["input"].tokens) == 0
    assert len(net.places["output"].tokens) == 100
