import threading
import time

import pytest

from cpnx.engine import DriveResult, PetriNet
from cpnx.places import PacedResourcePlace, Place, SinkPlace
from cpnx.tokens import Token
from cpnx.transitions import InputArc, OutputArc, Transition


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


def test_memoryerror_does_not_leak_running_count():
    with PetriNet(max_workers=1) as net:
        net.add_place(Place("input"))

        def raise_memory_error(tokens):
            raise MemoryError()

        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("input")],
                outputs=[],
                action=raise_memory_error,
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


class TestDriveToQuiescence:
    @staticmethod
    def _paced_net(*, pacing_secs: float) -> PetriNet:
        """A net whose only permit is a `PacedResourcePlace`, so throughput is cooldown-gated."""
        net = PetriNet(max_workers=1)
        net.add_place(Place("P_in"))
        net.add_place(PacedResourcePlace("R", capacity=1, pacing_secs=pacing_secs))
        net.add_place(SinkPlace("out"))
        net.add_transition(
            Transition(
                "t",
                inputs=[InputArc("P_in"), InputArc("R")],
                outputs=[OutputArc("R"), OutputArc("out")],
                action=lambda tokens: list(tokens),
            )
        )
        return net

    def test_drives_to_fixed_point_on_logical_clock(self):
        # Three data tokens through a single permit with a long (5s) cooldown: on the wall clock
        # this would take ~10 real seconds, but drive_to_quiescence jumps the cooldowns on the
        # logical clock, so it reaches a true fixed point in negligible real time.
        net = self._paced_net(pacing_secs=5.0)
        for _ in range(3):
            net.deposit("P_in", Token())

        start = time.monotonic()
        result = net.drive_to_quiescence()
        elapsed = time.monotonic() - start

        assert isinstance(result, DriveResult)
        assert net.is_quiescent()
        assert net.is_dead()
        assert net.places["out"]._absorbed == 3  # every token made it to the sink
        assert result.steps == 3  # one firing per token
        assert result.ticks >= 2  # two cooldown boundaries jumped (tokens 2 and 3)
        assert elapsed < 1.0  # the 10s of logical cooldown cost no real time

    def test_anchors_logical_clock_on_first_call(self):
        net = self._paced_net(pacing_secs=1.0)
        assert net._model_time is None  # starts on the wall clock
        net.drive_to_quiescence()
        assert net._model_time is not None  # first call engaged the logical clock

    def test_await_inflight_spin_cap_raises_on_hung_action(self):
        release = threading.Event()

        def hang(tokens):
            release.wait(timeout=5.0)
            return list(tokens)

        with PetriNet(max_workers=1) as net:
            net.add_place(Place("in"))
            net.add_place(SinkPlace("out"))
            net.add_transition(Transition("t", [InputArc("in")], [OutputArc("out")], action=hang))
            net.deposit("in", Token())

            assert net.step() is True  # action submitted, now mid-flight
            try:
                with pytest.raises(RuntimeError, match="in flight"):
                    net._await_inflight(max_spins=100)
            finally:
                release.set()  # let the worker finish so the pool can shut down cleanly

    def test_drive_to_quiescence_forwards_max_spins_to_barrier(self):
        # drive_to_quiescence must thread its max_spins through to _await_inflight so a caller
        # with legitimately slow actions can lift the barrier's cap. Proven deterministically: a
        # blocking action keeps one firing in flight, and the raised message echoes the *exact*
        # cap we passed — a hardcoded _await_inflight() call would report the 10_000_000 default.
        release = threading.Event()

        def hang(tokens):
            release.wait(timeout=5.0)
            return list(tokens)

        with PetriNet(max_workers=1) as net:
            net.add_place(Place("in"))
            net.add_place(SinkPlace("out"))
            net.add_transition(Transition("t", [InputArc("in")], [OutputArc("out")], action=hang))
            net.deposit("in", Token())

            try:
                with pytest.raises(RuntimeError, match=r"max_spins=7\b"):
                    net.drive_to_quiescence(max_spins=7)
            finally:
                release.set()  # let the worker finish so the pool can shut down cleanly
