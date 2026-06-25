import time

from petriq.engine import PetriNet
from petriq.places import Place
from petriq.tokens import Token
from petriq.transitions import InputArc, OutputArc, Transition


def test_batch_settling():
    net = PetriNet(max_workers=2)
    net.add_place(Place("input"))
    net.add_place(Place("output"))

    received_tokens = []

    def action(tokens):
        nonlocal received_tokens
        received_tokens = list(tokens)
        return tokens

    net.add_transition(
        Transition(
            name="batch_t",
            inputs=[InputArc("input", consume_all=True, settle_secs=0.2)],
            outputs=[OutputArc("output")],
            action=action,
        )
    )

    # Deposit 7 tokens with small delays, less than settle_secs (0.2s)
    for i in range(7):
        net.deposit("input", Token(payload={"idx": i}))
        time.sleep(0.04)

    # Immediately after the last deposit, it should not have settled yet
    fired = net.step()
    assert not fired
    assert len(received_tokens) == 0

    # Wait for the settling time to elapse (0.2s)
    time.sleep(0.22)

    # Now it should be settled and fire
    fired = net.step()
    assert fired

    net.run(deadline=time.monotonic() + 1.0)
    assert len(received_tokens) == 7
    assert [t.payload["idx"] for t in received_tokens] == list(range(7))
