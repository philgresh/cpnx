import time

from petriq.engine import PetriNet
from petriq.places import Place
from petriq.tokens import Token
from petriq.transitions import InputArc, OutputArc, Transition


def test_engine_setup_and_callbacks():
    net = PetriNet(max_workers=2)
    net.add_place(Place("input"))
    net.add_place(Place("output"))

    fired_transition = None
    fired_duration = 0.0
    deposited_place = None
    deposited_token = None

    def on_fired(name, duration):
        nonlocal fired_transition, fired_duration
        fired_transition = name
        fired_duration = duration

    def on_deposited(place_name, token):
        nonlocal deposited_place, deposited_token
        deposited_place = place_name
        deposited_token = token

    net.on_transition_fired = on_fired
    net.on_token_deposited = on_deposited

    net.add_transition(
        Transition(
            name="test_trans",
            inputs=[InputArc("input")],
            outputs=[OutputArc("output")],
            action=lambda tokens: tokens,
        )
    )

    # Deposit
    t = Token(payload={"key": "val"})
    net.deposit("input", t)

    assert deposited_place == "input"
    assert deposited_token == t

    # Run
    net.run(deadline=time.monotonic() + 1.0)

    # Check output
    assert len(net.places["output"].tokens) == 1
    assert fired_transition == "test_trans"
    assert fired_duration > 0.0
