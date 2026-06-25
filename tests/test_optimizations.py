import time
from unittest.mock import MagicMock

import pytest

from cpnx.engine import PetriNet
from cpnx.places import PacedResourcePlace, Place, ResourcePlace
from cpnx.tokens import Token
from cpnx.transitions import InputArc, OutputArc, Transition


def test_token_leak_on_submission_failure():
    """Ensure consumed tokens are restored to their source places if ThreadPoolExecutor submission fails."""
    net = PetriNet()
    net.add_place(Place("input"))
    net.add_place(Place("output"))

    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("input")],
            outputs=[OutputArc("output")],
            action=lambda tokens: tokens,
        )
    )

    # Mock the executor's submit method to fail
    net._executor.submit = MagicMock(side_effect=RuntimeError("thread pool full"))

    token = Token(payload={"test": True})
    net.deposit("input", token)

    with pytest.raises(RuntimeError, match="thread pool full"):
        net.step()

    # The token must be returned to the input place, not leaked
    assert len(net.places["input"]) == 1
    assert net.places["input"].tokens[0] == token


def test_leftover_resource_tokens_returned():
    """Ensure surplus resource tokens are returned to their source places if the action returns fewer."""
    net = PetriNet(max_workers=2)
    net.add_place(Place("input"))
    net.add_place(Place("output"))
    net.add_place(ResourcePlace("gpu", capacity=2))

    # A transition that consumes 2 GPU slots but only returns 1 resource token in outputs
    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("input"), InputArc("gpu", count=2)],
            outputs=[OutputArc("output"), OutputArc("gpu", count=1)],
            action=lambda tokens: [t for t in tokens if not t.is_resource],
        )
    )

    data_token = Token()
    net.deposit("input", data_token)

    # Trigger transition execution
    net.step()
    net.run(deadline=time.monotonic() + 1.0)

    # The surplus GPU token that was not pop-deposited must be returned to its place
    assert len(net.places["gpu"]) == 2
    assert len(net.places["output"]) == 1


def test_paced_resource_cooldown_on_tokens():
    """Ensure PacedResourcePlace sets token cooldown timestamps (available_at) upon deposit."""
    place = PacedResourcePlace("gpu", capacity=2, pacing_secs=5.0)

    # Retrieve one token and deposit it back to start cooldown
    retrieved = place.retrieve(1)[0]
    assert retrieved.available_at == 0.0
    place.deposit(retrieved)

    # Cooldown timestamp is stored directly on the deposited token in the place
    deposited = [t for t in place.tokens if t.id == retrieved.id][0]
    assert deposited.available_at > time.monotonic()


def test_dynamic_sleep_and_configurable_cooldown():
    """Verify timed feature detection and configurable cooldown intervals."""
    net_no_timed = PetriNet(cooldown_interval=0.1)
    net_no_timed.add_place(Place("input"))
    assert net_no_timed._has_timed_features is False
    assert net_no_timed.cooldown_interval == 0.1

    net_with_timed = PetriNet(cooldown_interval=0.2)
    net_with_timed.add_place(PacedResourcePlace("gpu", capacity=1, pacing_secs=1.0))
    assert net_with_timed._has_timed_features is True
    assert net_with_timed.cooldown_interval == 0.2


def test_snapshot_payload_copy():
    """Ensure token payloads are immutable and raise TypeError on mutation attempts."""
    net = PetriNet()
    net.add_place(Place("input"))

    token = Token(payload={"nested": {"value": 1}})
    net.deposit("input", token)

    import pytest

    with pytest.raises(TypeError):
        token.payload["nested"]["value"] = 99
