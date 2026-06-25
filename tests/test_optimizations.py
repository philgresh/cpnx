import time
from unittest.mock import MagicMock

import pytest

from petriq.engine import PetriNet
from petriq.places import PacedResourcePlace, Place, ResourcePlace
from petriq.tokens import Token
from petriq.transitions import InputArc, OutputArc, Transition


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


def test_paced_resource_retrieve_all_clears_cooldowns():
    """Ensure PacedResourcePlace retrieve_all clears token cooldown tracking to prevent dictionary leaks."""
    place = PacedResourcePlace("gpu", capacity=2, pacing_secs=5.0)
    token = place.tokens[0]

    # Retrieve one token and deposit it back to start cooldown
    place.retrieve(1)
    place.deposit(token)
    assert token.id in place._cooldowns

    # Call retrieve_all which clears the tokens
    place.retrieve_all()

    # The cooldown tracker should be empty for that token
    assert token.id not in place._cooldowns


def test_paced_resource_retrieve_specific_clears_cooldowns():
    """Ensure PacedResourcePlace retrieve_specific clears token cooldown tracking to prevent dictionary leaks."""
    place = PacedResourcePlace("gpu", capacity=2, pacing_secs=5.0)
    token = place.tokens[0]

    place.retrieve(1)
    place.deposit(token)
    assert token.id in place._cooldowns

    # Retrieve specifically
    place.retrieve_specific([token])

    assert token.id not in place._cooldowns


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
    """Ensure snapshot payloads are deep-copied to prevent concurrent mutation issues."""
    net = PetriNet()
    net.add_place(Place("input"))

    token = Token(payload={"nested": {"value": 1}})
    net.deposit("input", token)

    snap = net.snapshot()
    # Mutate the original token's payload
    token.payload["nested"]["value"] = 99

    # The snapshot payload should remain isolated (original value 1)
    assert snap["places"]["input"][0]["payload"]["nested"]["value"] == 1
