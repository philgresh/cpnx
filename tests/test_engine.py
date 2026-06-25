import time

import pytest

from petriq.engine import PetriNet
from petriq.places import Place
from petriq.tokens import Token
from petriq.transitions import InputArc, OutputArc, Transition


class TestEngineSetup:
    def test_default_error_place_created(self):
        net = PetriNet()
        assert "failed" in net.places

    def test_custom_error_place(self):
        net = PetriNet(error_place="dead_letters")
        assert "dead_letters" in net.places
        assert "failed" not in net.places

    def test_add_place_accessible(self):
        net = PetriNet()
        net.add_place(Place("myplace"))
        assert "myplace" in net.places

    def test_add_transition_accessible(self):
        net = PetriNet()
        net.add_place(Place("a"))
        net.add_place(Place("b"))
        net.add_transition(Transition("t", [InputArc("a")], [OutputArc("b")], action=lambda t: t))
        assert "t" in net.transitions

    def test_deposit_to_unknown_place_auto_creates_it(self):
        net = PetriNet()
        net.deposit("new_place", Token())
        assert "new_place" in net.places
        assert len(net.places["new_place"].tokens) == 1


class TestEngineCallbacks:
    def test_on_token_deposited_fires(self):
        net = PetriNet()
        events = []
        net.on_token_deposited = lambda place, token: events.append((place, token.id))

        t = Token()
        net.deposit("myplace", t)
        assert ("myplace", t.id) in events

    def test_on_transition_fired_fires_with_duration(self):
        net = PetriNet(max_workers=2)
        net.add_place(Place("input"))
        net.add_place(Place("output"))

        events = []
        net.on_transition_fired = lambda name, dur: events.append((name, dur))

        net.add_transition(Transition("t", [InputArc("input")], [OutputArc("output")], action=lambda t: t))
        net.deposit("input", Token())
        net.run(deadline=time.monotonic() + 1.0)

        assert len(events) == 1
        assert events[0][0] == "t"
        assert events[0][1] >= 0.0

    def test_callback_exception_does_not_crash_engine(self):
        net = PetriNet(max_workers=2)
        net.add_place(Place("input"))
        net.add_place(Place("output"))

        def bad_callback(name, dur):
            raise RuntimeError("callback exploded")

        net.on_transition_fired = bad_callback

        net.add_transition(Transition("t", [InputArc("input")], [OutputArc("output")], action=lambda t: t))
        net.deposit("input", Token())
        # Should not raise
        net.run(deadline=time.monotonic() + 1.0)
        assert len(net.places["output"].tokens) == 1


class TestEngineQuiescence:
    def test_quiescent_when_empty(self):
        net = PetriNet()
        assert net.is_quiescent()

    def test_not_quiescent_with_enabled_transition(self):
        net = PetriNet(max_workers=2)
        net.add_place(Place("input"))
        net.add_place(Place("output"))
        net.add_transition(Transition("t", [InputArc("input")], [OutputArc("output")], action=lambda t: t))
        net.deposit("input", Token())
        assert not net.is_quiescent()

    def test_quiescent_after_run_completes(self):
        net = PetriNet(max_workers=2)
        net.add_place(Place("input"))
        net.add_place(Place("output"))
        net.add_transition(Transition("t", [InputArc("input")], [OutputArc("output")], action=lambda t: t))
        net.deposit("input", Token())
        net.run(deadline=time.monotonic() + 2.0)
        assert net.is_quiescent()

    def test_step_returns_false_when_no_enabled_transitions(self):
        net = PetriNet(max_workers=2)
        net.add_place(Place("input"))
        net.add_place(Place("output"))
        net.add_transition(Transition("t", [InputArc("input")], [OutputArc("output")], action=lambda t: t))
        # No tokens — step should return False
        assert not net.step()


class TestEngineMultiArcTransition:
    def test_multi_input_transition_requires_all_inputs(self):
        net = PetriNet(max_workers=2)
        net.add_place(Place("a"))
        net.add_place(Place("b"))
        net.add_place(Place("output"))

        net.add_transition(
            Transition(
                name="join",
                inputs=[InputArc("a"), InputArc("b")],
                outputs=[OutputArc("output")],
                action=lambda tokens: [tokens[0]],
            )
        )

        net.deposit("a", Token())
        # Only 'a' has a token — should not fire
        assert not net.step()

        net.deposit("b", Token())
        # Now both inputs are available
        assert net.step()
        net.run(deadline=time.monotonic() + 1.0)
        assert len(net.places["output"].tokens) == 1

    def test_output_arc_count_respected(self):
        net = PetriNet(max_workers=2)
        net.add_place(Place("input"))
        net.add_place(Place("output"))

        def fanout(tokens):
            # Return a 3-element list
            return [Token(payload={"copy": i}) for i in range(3)]

        net.add_transition(
            Transition(
                name="fanout",
                inputs=[InputArc("input")],
                outputs=[OutputArc("output", count=3)],
                action=fanout,
            )
        )

        net.deposit("input", Token())
        net.run(deadline=time.monotonic() + 1.0)
        assert len(net.places["output"].tokens) == 3


def test_basexception_does_not_leak_running_count():
    net = PetriNet(max_workers=1)
    net.add_place(Place("input"))

    def raise_keyboard_interrupt(tokens):
        raise KeyboardInterrupt()

    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("input")],
            outputs=[],
            action=raise_keyboard_interrupt,
        )
    )

    net.deposit("input", Token())
    assert net.step() is True

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with net._lock:
            if net._running_count == 0:
                break
        time.sleep(0.01)

    with net._lock:
        assert net._running_count == 0


def test_bipartite_add_transition_rejects_transition_target():
    net = PetriNet()
    net.add_transition(
        Transition(
            name="t1",
            inputs=[],
            outputs=[],
            action=lambda tokens: tokens,
        )
    )

    with pytest.raises(TypeError, match="Arc target .* is a Transition, not a Place"):
        net.add_transition(
            Transition(
                name="t2",
                inputs=[InputArc("t1")],
                outputs=[],
                action=lambda tokens: tokens,
            )
        )


def test_name_overlap_raises():
    net = PetriNet()
    net.add_place(Place("shared_name"))
    with pytest.raises(ValueError, match="already registered as a Place"):
        net.add_transition(
            Transition(
                name="shared_name",
                inputs=[],
                outputs=[],
                action=lambda tokens: tokens,
            )
        )

    net2 = PetriNet()
    net2.add_transition(
        Transition(
            name="shared_name",
            inputs=[],
            outputs=[],
            action=lambda tokens: tokens,
        )
    )
    with pytest.raises(ValueError, match="already registered as a Transition"):
        net2.add_place(Place("shared_name"))
