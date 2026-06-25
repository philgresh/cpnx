import time

import pytest

from cpnx.engine import PetriNet
from cpnx.places import PacedResourcePlace, Place
from cpnx.tokens import Token
from cpnx.transitions import InputArc, OutputArc, Transition


def test_monotonic_clock_advance():
    net = PetriNet()
    # Initially defaults to monotonic time (real-time)
    t0 = net.model_time
    assert t0 > 0.0

    # Advance logical time to 100.0
    net.advance_time(100.0)
    assert net.model_time == 100.0

    # Advance further
    net.advance_time(105.5)
    assert net.model_time == 105.5

    # Decrement backward raises ValueError
    with pytest.raises(ValueError, match="Clock mutability violation"):
        net.advance_time(90.0)

    # Equal timestamp also raises ValueError
    with pytest.raises(ValueError, match="Clock mutability violation"):
        net.advance_time(105.5)


def test_token_availability_time_gating():
    net = PetriNet()
    net.advance_time(10.0)

    p_in = Place("in")
    p_out = Place("out")
    net.add_place(p_in)
    net.add_place(p_out)

    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("in")],
            outputs=[OutputArc("out")],
            action=lambda tokens: tokens,
        )
    )

    # Deposit a token available in the future (t=15.0)
    tok = Token(available_at=15.0)
    net.deposit("in", tok)

    # At t=10.0, step should fail/return False because token is not available yet
    assert net.step() is False
    assert len(net.places["in"].tokens) == 1

    # Advance time to t=14.9, still not available
    net.advance_time(14.9)
    assert net.step() is False

    # Advance to t=15.0, now it can fire!
    net.advance_time(15.0)
    assert net.step() is True
    # Wait for the async worker to complete
    net.run(deadline=time.monotonic() + 1.0)
    assert len(net.places["in"].tokens) == 0
    assert len(net.places["out"].tokens) == 1


def test_paced_resource_logical_cooldown():
    # Cooldown of 5.0 seconds
    net = PetriNet()
    net.advance_time(10.0)

    paced = PacedResourcePlace("paced", capacity=1, pacing_secs=5.0)
    p_out = Place("out")
    net.add_place(paced)
    net.add_place(p_out)

    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("paced")],
            outputs=[OutputArc("paced"), OutputArc("out")],
            action=lambda tokens: [tokens[0], Token()],
        )
    )

    # Initial token is available at 0.0, so at t=10.0 it's ready
    assert net.step() is True
    net.run(deadline=time.monotonic() + 1.0)

    # The token is returned to "paced", and its available_at becomes 10.0 + 5.0 = 15.0
    tokens = paced.tokens
    assert len(tokens) == 1
    assert tokens[0].available_at == 15.0

    # At t=10.0, it is in cooldown
    assert net.step() is False

    # Advance logical clock to t=14.0, still in cooldown
    net.advance_time(14.0)
    assert net.step() is False

    # Advance logical clock to t=15.0, now it is available!
    net.advance_time(15.0)
    assert net.step() is True


def test_settle_secs_respects_model_time():
    with PetriNet() as net:
        net.advance_time(100.0)

        p_in = Place("in")
        p_out = Place("out")
        net.add_place(p_in)
        net.add_place(p_out)

        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("in", settle_secs=10.0)],
                outputs=[OutputArc("out")],
                action=lambda tokens: tokens,
            )
        )

        # Deposit at t=100.0
        net.deposit("in", Token())
        # Settle time is 100.0 + 10.0 = 110.0
        # At t=105.0, it should not be enabled
        net.advance_time(105.0)
        assert net.step() is False

        # At t=110.0, it should be enabled and fire
        net.advance_time(110.0)
        assert net.step() is True
