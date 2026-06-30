import time
from unittest.mock import MagicMock

import pytest

from cpnx.engine import PetriNet
from cpnx.places import PacedResourcePlace, Place, ResourcePlace
from cpnx.sandbox import SandboxEvaluator
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


def test_string_guard_recompiles_on_reassignment():
    """A string guard reassigned after construction must evaluate the *new* predicate.

    Regression: the precompiled guard code object was cached in __post_init__ and not
    refreshed on mutation, so the engine silently kept evaluating the original predicate.
    """
    net = _make_net("len(tokens) >= 5")
    transition = net.transitions["t"]
    net.deposit("input", Token(payload={"i": 0}))

    # Original guard requires >= 5 tokens; with one token the transition is blocked.
    assert net._is_transition_enabled(transition) is False

    # Loosen the guard. The compiled object must track the live source.
    transition.guard = "len(tokens) >= 1"
    assert SandboxEvaluator.evaluate_compiled(transition._compiled_guard, {"tokens": [object()]}) is True
    assert net._is_transition_enabled(transition) is True


def test_callable_guard_swapped_to_string_recompiles():
    """Swapping a callable guard to a string must compile it, not leave a stale ``None``.

    Regression: callable guards stored ``_compiled_guard = None``; reassigning a string
    left it ``None``, so the engine's string branch called ``eval(None, ...)`` -> TypeError.
    """
    net = _make_net(lambda tokens: False)
    transition = net.transitions["t"]
    assert transition._compiled_guard is None  # callable -> no compiled object

    transition.guard = "len(tokens) >= 1"
    assert transition._compiled_guard is not None

    net.deposit("input", Token(payload={"i": 0}))
    # Must not raise TypeError from eval(None, ...); the new string predicate enables it.
    assert net._is_transition_enabled(transition) is True


def test_string_guard_mutation_to_forbidden_expression_fails_fast():
    """Reassigning a guard to a forbidden expression raises eagerly and leaves prior state."""
    net = _make_net("len(tokens) >= 1")
    transition = net.transitions["t"]

    with pytest.raises(PermissionError):
        transition.guard = "__import__('os').system('echo hi')"

    # The bad assignment did not corrupt the previously-valid compiled guard.
    assert SandboxEvaluator.evaluate_compiled(transition._compiled_guard, {"tokens": [object()]}) is True


def test_arc_expression_recompiles_on_reassignment():
    """A string arc expression reassigned after construction must use the new source."""
    arc = OutputArc(place="output", expression="bool(tokens)")
    assert arc._compiled_expression is not None

    # Swap to a callable: compiled object must clear so the engine uses the callable path.
    arc.expression = lambda tokens: True
    assert arc._compiled_expression is None

    # Swap back to a different string: compiled object must track the new source.
    arc.expression = "bool(tokens and tokens[0].color == 'data')"
    ctx = {"tokens": [Token(color="data")]}
    assert SandboxEvaluator.evaluate_compiled(arc._compiled_expression, ctx) is True
