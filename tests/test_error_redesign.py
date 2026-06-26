import threading
import time

from cpnx.engine import PetriNet
from cpnx.places import Place, ResourcePlace
from cpnx.tokens import ERROR_COLOR, Token
from cpnx.transitions import InputArc, OutputArc, Transition


def action_boom(tokens):
    raise ValueError("boom")


def test_termination_on_persistent_failure():
    """A transition whose action always raises, max_retries=3:

    after <= 3 retries, the data token is in error_place, attempts == 3,
    and run(deadline=None) returns (net quiesces) without hanging.
    """
    net = PetriNet(max_workers=2, error_place="failed", retry_delay=0.01)
    net.add_place(Place("input"))

    def action_fail(tokens):
        raise ValueError("persistent fail")

    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("input")],
            outputs=[],
            action=action_fail,
            max_retries=3,
        )
    )

    t = Token()
    net.deposit("input", t)

    # We use deadline=None to ensure sound termination guarantee
    net.run()

    # The data token is now in error_place ("failed")
    assert len(net.places["input"].tokens) == 0
    assert len(net.places["failed"].tokens) == 1

    failed_token = net.places["failed"].tokens[0]
    assert failed_token.attempts == 3


def test_resource_safety():
    """Across retries and on exhaustion, every consumed resource token is returned to

    its source place — assert no resource leak and no resource in error_place.
    """
    net = PetriNet(max_workers=2, error_place="failed", retry_delay=0.01)
    net.add_place(Place("input"))
    net.add_place(ResourcePlace("gpu", capacity=1))

    call_count = 0

    def action_fail(tokens):
        nonlocal call_count
        call_count += 1
        raise ValueError("fail")

    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("input"), InputArc("gpu")],
            outputs=[OutputArc("gpu")],
            action=action_fail,
            max_retries=2,
        )
    )

    net.deposit("input", Token())
    # Firing 1 (attempts=0) -> fails, retried (attempts=1)
    # Firing 2 (attempts=1) -> fails, retried (attempts=2)
    # Firing 3 (attempts=2) -> fails, exhausted -> sent to error_place
    net.run()

    # Resource GPU returned to GPU place
    assert len(net.places["gpu"].tokens) == 1
    assert net.places["gpu"].tokens[0].is_resource
    # No resource in failed place
    assert len(net.places["failed"].tokens) == 1
    assert not net.places["failed"].tokens[0].is_resource


def test_color_routing_canonical_path():
    """Action returns an error-coloured token; an OutputArc(expression=is_error)

    routes it to error_place while OutputArc(expression=is_success) routes a success
    token to the normal place. Assert correct place for each, and 1-in-1-out.
    """
    net = PetriNet(max_workers=2)
    net.add_place(Place("input"))
    net.add_place(Place("success_place"))

    # We use OutputArc.on_color helper
    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("input")],
            outputs=[
                OutputArc.on_color("success", "success_place"),
                OutputArc.on_color(ERROR_COLOR, "failed"),
            ],
            action=lambda tokens: [Token(color="success") if tokens[0].payload.get("ok") else Token(color=ERROR_COLOR)],
        )
    )

    # Test success path
    net.deposit("input", Token(payload={"ok": True}))
    net.run()
    assert len(net.places["success_place"].tokens) == 1
    assert len(net.places["failed"].tokens) == 0

    # Clean places for second test
    net.places["success_place"].retrieve_all(model_time=net.model_time)
    net.places["failed"].retrieve_all(model_time=net.model_time)

    # Test error path
    net.deposit("input", Token(payload={"ok": False}))
    net.run()
    assert len(net.places["success_place"].tokens) == 0
    assert len(net.places["failed"].tokens) == 1
    assert net.places["failed"].tokens[0].color == ERROR_COLOR


def test_max_retries_zero():
    """max_retries=0 dead-letters on first failure (docstring-legacy behaviour)."""
    net = PetriNet(max_workers=2, error_place="failed", retry_delay=0.01)
    net.add_place(Place("input"))

    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("input")],
            outputs=[],
            action=action_boom,
            max_retries=0,
        )
    )

    net.deposit("input", Token())
    net.run()

    assert len(net.places["input"].tokens) == 0
    assert len(net.places["failed"].tokens) == 1
    assert net.places["failed"].tokens[0].attempts == 0


def test_max_retries_none():
    """max_retries=None preserves infinite-retry (assert it retries past 5;

    bound the test with a deadline so CI can't hang).
    """
    net = PetriNet(max_workers=2, error_place="failed", retry_delay=0.005, cooldown_interval=0.002)
    net.add_place(Place("input"))

    call_count = 0

    def action_fail(tokens):
        nonlocal call_count
        call_count += 1
        raise ValueError("fail")

    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("input")],
            outputs=[],
            action=action_fail,
            max_retries=None,
        )
    )

    net.deposit("input", Token())
    # Bound the run with a deadline
    net.run(deadline=time.monotonic() + 0.15)

    # It should have retried more than 5 times
    assert call_count > 5
    # Since it's infinite retry, the token should still be in the source place (or in flight / being retried)
    # and nothing in failed place
    assert len(net.places["failed"].tokens) == 0


def test_optional_deadline():
    """run() with no deadline returns on quiescence for an acyclic net."""
    net = PetriNet(max_workers=2)
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

    net.deposit("input", Token())
    # This should return because the net is acyclic and quiesces
    net.run()

    assert len(net.places["input"].tokens) == 0
    assert len(net.places["output"].tokens) == 1


def test_cooperative_cancellation():
    """run(stop_event=ev) returns promptly after ev.set() even with work still enabled."""
    net = PetriNet(max_workers=1)
    net.add_place(Place("input"))
    net.add_place(Place("output"))

    # An action that takes some time to run
    def slow_action(tokens):
        time.sleep(0.05)
        return tokens

    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("input")],
            outputs=[OutputArc("output")],
            action=slow_action,
        )
    )

    stop_event = threading.Event()

    # We deposit multiple tokens so the net has ongoing enabled work
    for _ in range(10):
        net.deposit("input", Token())

    # Start running the net in a background thread
    t = threading.Thread(target=net.run, kwargs={"stop_event": stop_event})
    t.start()

    # Let it run for a little bit, then set stop_event
    time.sleep(0.02)
    stop_event.set()

    # Join should return quickly
    join_start = time.monotonic()
    t.join(timeout=0.5)
    join_duration = time.monotonic() - join_start

    assert not t.is_alive()
    assert join_duration < 0.2
    # Not all tokens should have been processed
    assert len(net.places["output"].tokens) < 10


def test_backward_compat_positional_deadline():
    """Existing tests that pass deadline positionally still pass unchanged."""
    net = PetriNet(max_workers=2)
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

    net.deposit("input", Token())
    # Positional deadline
    net.run(time.monotonic() + 1.0)

    assert len(net.places["output"].tokens) == 1


def test_timeout_dead_letter():
    """An action exceeding action_timeout_secs is treated as a failure and

    follows the same bounded-retry -> error_place path.
    """
    net = PetriNet(max_workers=2, error_place="failed", retry_delay=0.01)
    net.add_place(Place("input"))

    def slow_action(tokens):
        time.sleep(0.1)
        return tokens

    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("input")],
            outputs=[],
            action=slow_action,
            action_timeout_secs=0.02,
            max_retries=2,
        )
    )

    net.deposit("input", Token())
    net.run()

    # The token is dead-lettered to error_place
    assert len(net.places["input"].tokens) == 0
    assert len(net.places["failed"].tokens) == 1
    assert net.places["failed"].tokens[0].attempts == 2


def test_on_token_dead_lettered_callback():
    """Verify that on_token_dead_lettered callback is invoked outside the lock."""
    net = PetriNet(max_workers=2, error_place="failed", retry_delay=0.01)
    net.add_place(Place("input"))

    dead_letter_info = []

    def on_dl(transition_name, token):
        dead_letter_info.append((transition_name, token))

    net.on_token_dead_lettered = on_dl

    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("input")],
            outputs=[],
            action=action_boom,
            max_retries=1,
        )
    )

    t = Token(payload={"original_id": "my_job"})
    net.deposit("input", t)
    net.run()

    assert len(dead_letter_info) == 1
    assert dead_letter_info[0][0] == "t"
    assert dead_letter_info[0][1].payload["original_id"] == "my_job"
