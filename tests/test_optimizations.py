import time
from unittest.mock import MagicMock

import pytest

from cpnx.engine import PetriNet
from cpnx.places import PacedResourcePlace, Place, ResourcePlace
from cpnx.tokens import Token
from cpnx.transitions import InputArc, OutputArc, Transition

# Module-level mutable state so `_reads_mutable_state` closes over something the
# certifier rejects (but the purity blocklist still allows — no I/O).
_MUTABLE = {"n": 0}


def _reads_mutable_state(tokens):
    return _MUTABLE["n"] == 0


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


def _make_net(guard):
    """A two-place net whose single transition carries *guard* and fires one token."""
    net = PetriNet()
    net.add_place(Place("input"))
    net.add_place(Place("output"))
    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("input")],
            outputs=[OutputArc("output")],
            action=lambda tokens: tokens,
            guard=guard,
        )
    )
    return net


def test_guard_reassignment_recertifies_and_takes_effect():
    """A guard reassigned after construction must evaluate the *new* predicate and re-certify.

    Regression (from the string era): the cached artifact was not refreshed on mutation, so
    the engine kept evaluating the original predicate. `__setattr__` now recomputes both the
    live guard and its `_inline_safe` flag on every assignment.
    """
    net = _make_net(lambda tokens: len(tokens) >= 5)
    transition = net.transitions["t"]
    assert transition._inline_safe is True  # certified
    net.deposit("input", Token(payload={"i": 0}))

    # Original guard requires >= 5 tokens; with one token the transition is blocked.
    assert net._is_transition_enabled(transition) is False

    # Loosen the guard — the engine must track the live callable.
    transition.guard = lambda tokens: len(tokens) >= 1
    assert transition._inline_safe is True
    assert net._is_transition_enabled(transition) is True


def test_reassignment_to_uncertified_guard_flips_inline_safe():
    """Swapping a certified guard for one that reads mutable external state drops inline-safety.

    The uncertified guard still passes the purity blocklist (no I/O), so it is accepted and
    routed to the timeout-bounded executor rather than inlined.
    """
    net = _make_net(lambda tokens: True)
    transition = net.transitions["t"]
    assert transition._inline_safe is True

    transition.guard = _reads_mutable_state
    assert transition._inline_safe is False  # certification rejects mutable-state closure
    net.deposit("input", Token())
    assert net._is_transition_enabled(transition) is True  # still evaluates correctly (via executor)


def test_string_guard_reassignment_is_rejected():
    """Reassigning a guard to a string raises TypeError and leaves the prior guard intact."""
    net = _make_net(lambda tokens: len(tokens) >= 1)
    transition = net.transitions["t"]

    with pytest.raises(TypeError, match="callable"):
        transition.guard = "len(tokens) >= 1"

    # The rejected assignment did not corrupt the previously-valid guard.
    assert transition._inline_safe is True
    net.deposit("input", Token())
    assert net._is_transition_enabled(transition) is True


def test_arc_condition_reassignment_recertifies():
    """An arc condition reassigned after construction re-certifies; a string is rejected."""
    arc = OutputArc(place="output", condition=lambda tokens: bool(tokens))
    assert arc._inline_safe is True

    arc.condition = _reads_mutable_state
    assert arc._inline_safe is False

    with pytest.raises(TypeError, match="callable"):
        arc.condition = "bool(tokens)"


def test_input_arc_key_reassignment_recertifies():
    """An InputArc.key reassigned after construction re-certifies and updates `_key_inline_safe`."""
    arc = InputArc(place="input", key=lambda tok: tok.payload["w"])
    assert arc._key_inline_safe is True

    arc.key = _reads_mutable_state
    assert arc._key_inline_safe is False

    with pytest.raises(TypeError, match="callable"):
        arc.key = "tok.payload['w']"
