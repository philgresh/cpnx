"""Tests for CircuitBreakerPlace and its engine coupling."""

import time

import pytest

from cpnx.engine import PetriNet
from cpnx.places import CircuitBreakerPlace, Place
from cpnx.tokens import Token
from cpnx.transitions import InputArc, OutputArc, Transition


def _identity(toks):
    return [toks[0].evolve(payload_updates={"seen": True})]


class TestCircuitBreakerConstruction:
    def test_rejects_bad_threshold(self):
        with pytest.raises(ValueError):
            CircuitBreakerPlace("b", trip_predicate=lambda e: True, failure_threshold=0)

    def test_rejects_negative_cooldown(self):
        with pytest.raises(ValueError):
            CircuitBreakerPlace("b", trip_predicate=lambda e: True, cooldown_secs=-1.0)

    def test_rejects_non_callable_predicate(self):
        with pytest.raises(TypeError):
            CircuitBreakerPlace("b", trip_predicate="nope")

    def test_rejects_non_callable_probe(self):
        with pytest.raises(TypeError):
            CircuitBreakerPlace("b", trip_predicate=lambda e: True, probe="nope")

    def test_starts_closed(self):
        b = CircuitBreakerPlace("b", trip_predicate=lambda e: True)
        assert b.state == "closed"
        assert not b.is_open()
        assert b.can_retrieve(1)


class TestBreakerStateMachine:
    def test_trips_after_threshold_consecutive_classified_failures(self):
        b = CircuitBreakerPlace("b", trip_predicate=lambda e: isinstance(e, ValueError), failure_threshold=3)
        assert b.record_result(False, ValueError(), now=100.0) is False
        assert b.record_result(False, ValueError(), now=100.0) is False
        assert b.record_result(False, ValueError(), now=100.0) is True  # 3rd trips
        assert b.is_open()
        assert b.probe_at == pytest.approx(700.0)  # 100 + default cooldown 600

    def test_unclassified_failures_never_trip(self):
        b = CircuitBreakerPlace("b", trip_predicate=lambda e: isinstance(e, ValueError), failure_threshold=2)
        for _ in range(10):
            b.record_result(False, KeyError(), now=0.0)  # not a ValueError → ignored
        assert b.state == "closed"

    def test_success_resets_consecutive_count(self):
        b = CircuitBreakerPlace("b", trip_predicate=lambda e: True, failure_threshold=2)
        b.record_result(False, ValueError(), now=0.0)
        assert b.consecutive_failures == 1
        b.record_result(True, None, now=0.0)
        assert b.consecutive_failures == 0
        b.record_result(False, ValueError(), now=0.0)  # count restarts; does not trip
        assert b.state == "closed"

    def test_gating_while_open(self):
        b = CircuitBreakerPlace("b", trip_predicate=lambda e: True, failure_threshold=1, cooldown_secs=5.0)
        b.record_result(False, ValueError(), now=100.0)
        assert b.is_open()
        assert b.can_retrieve(1, model_time=101.0) is False  # gated for a real firing
        assert b.can_retrieve(1, model_time=float("inf")) is True  # resumable under ignore-timing

    def test_raising_predicate_does_not_trip_or_crash(self):
        def boom(_exc):
            raise RuntimeError("classifier bug")

        b = CircuitBreakerPlace("b", trip_predicate=boom, failure_threshold=1)
        assert b.record_result(False, ValueError(), now=0.0) is False
        assert b.state == "closed"

    def test_out_of_band_trip_and_close(self):
        b = CircuitBreakerPlace("b", trip_predicate=lambda e: True, cooldown_secs=5.0)
        assert b.trip(now=10.0) is True
        assert b.is_open()
        assert b.trip(now=10.0) is False  # already open
        b.close()
        assert b.state == "closed"

    def test_probe_result_transitions(self):
        b = CircuitBreakerPlace("b", trip_predicate=lambda e: True, failure_threshold=1, cooldown_secs=5.0)
        b.record_result(False, ValueError(), now=0.0)
        b.begin_probe()
        assert b.probing
        b.record_probe_result(False, now=10.0)  # still down
        assert b.state == "open" and not b.probing
        b.record_probe_result(True, now=20.0)  # recovered
        assert b.state == "closed"

    def test_deposit_and_consume_rejected(self):
        b = CircuitBreakerPlace("b", trip_predicate=lambda e: True)
        with pytest.raises(TypeError):
            b.deposit(Token())
        with pytest.raises(ValueError):
            b.retrieve()
        assert b.can_deposit() is False


class TestBreakerValidation:
    def test_consuming_arc_on_breaker_rejected(self):
        net = PetriNet()
        net.add_place(CircuitBreakerPlace("b", trip_predicate=lambda e: True))
        net.add_place(Place("done"))
        net.add_transition(Transition("t", inputs=[InputArc("b")], outputs=[OutputArc("done")], action=_identity))
        with pytest.raises(TypeError, match="consuming input arc on CircuitBreakerPlace"):
            net.validate()

    def test_output_arc_to_breaker_rejected(self):
        net = PetriNet()
        net.add_place(CircuitBreakerPlace("b", trip_predicate=lambda e: True))
        net.add_place(Place("work"))
        net.add_transition(Transition("t", inputs=[InputArc("work")], outputs=[OutputArc("b")], action=_identity))
        with pytest.raises(TypeError, match="output arc targeting CircuitBreakerPlace"):
            net.validate()

    def test_breaker_binding_must_name_breaker_place(self):
        net = PetriNet()
        net.add_place(Place("plain"))
        net.add_place(Place("work"))
        net.add_place(Place("done"))
        net.add_transition(
            Transition(
                "t",
                inputs=[InputArc("work")],
                outputs=[OutputArc("done")],
                action=_identity,
                breaker="plain",
            )
        )
        with pytest.raises(TypeError, match="not a CircuitBreakerPlace"):
            net.validate()

    def test_breaker_binding_unknown_place(self):
        net = PetriNet()
        net.add_place(Place("work"))
        net.add_place(Place("done"))
        net.add_transition(
            Transition(
                "t",
                inputs=[InputArc("work")],
                outputs=[OutputArc("done")],
                action=_identity,
                breaker="ghost",
            )
        )
        with pytest.raises(KeyError):
            net.validate()


class TestBreakerEngineIntegration:
    def _build(self, state, *, probe=None, threshold=2, cooldown=1.0):
        def up(toks):
            return [toks[0].evolve(payload_updates={"s": "mid"})]

        def down(toks):
            if state["down"]:
                state["fails"] += 1
                raise ConnectionError("dependency down")
            return [toks[0].evolve(payload_updates={"s": "done"})]

        net = PetriNet(max_workers=1)
        net.add_place(Place("work"))
        net.add_place(Place("mid", bound=200))
        net.add_place(Place("done"))
        net.add_place(
            CircuitBreakerPlace(
                "healthy",
                trip_predicate=lambda e: isinstance(e, ConnectionError),
                failure_threshold=threshold,
                cooldown_secs=cooldown,
                probe=probe,
            )
        )
        net.add_transition(
            Transition(
                "upstream",
                inputs=[InputArc("work"), InputArc("healthy", test=True)],
                outputs=[OutputArc("mid")],
                action=up,
                max_retries=0,
            )
        )
        net.add_transition(
            Transition(
                "downstream",
                inputs=[InputArc("mid"), InputArc("healthy", test=True)],
                outputs=[OutputArc("done")],
                action=down,
                max_retries=0,
                breaker="healthy",
            )
        )
        return net

    def test_incremental_scheduler_disabled_for_breaker_net(self):
        net = self._build({"down": True, "fails": 0})
        assert net._has_timed_features is True
        assert net._incremental_eligible is False

    def test_trip_and_probe_recovery_deterministic(self):
        # drive_to_quiescence awaits each firing, so failures are observed before the next
        # firing — the breaker trips after exactly `threshold` failures, then a probe recovers.
        state = {"down": True, "fails": 0, "probes": 0}

        def probe():
            state["probes"] += 1
            state["down"] = False  # dependency recovers on first probe
            return True

        net = self._build(state, probe=probe, threshold=2, cooldown=1.0)
        for i in range(20):
            net.deposit("work", Token(payload={"i": i}))

        result = net.drive_to_quiescence()

        assert state["fails"] == 2  # tripped after exactly the threshold
        assert state["probes"] == 1
        assert net.places["healthy"].state == "closed"
        assert len(net.places["done"]) == 18  # the 2 failed tokens dead-lettered
        assert len(net.places["failed"]) == 2
        assert result.ticks == 1  # one logical-clock jump to the cooldown boundary

    def test_open_breaker_holds_work_and_is_not_quiescent(self):
        # With the dependency permanently down and no probe, once the breaker opens the gated
        # transitions stop; queued work stays put and the net is dead-now but not quiescent.
        state = {"down": True, "fails": 0}
        net = self._build(state, probe=None, threshold=2, cooldown=100.0)
        for i in range(5):
            net.deposit("mid", Token(payload={"i": i}))  # seed downstream directly

        # Fire synchronously until the breaker trips.
        for _ in range(10):
            net.step()
            net._await_inflight()
            if net.places["healthy"].is_open():
                break

        assert net.places["healthy"].is_open()
        assert net.is_dead()  # nothing can fire this instant
        assert not net.is_quiescent()  # but work remains behind the breaker → not quiescent
        assert len(net.places["mid"]) > 0  # remaining work is held in place

    def test_optimistic_recovery_without_probe(self):
        # No probe: after the cooldown the breaker closes optimistically and held work resumes.
        state = {"down": True, "fails": 0}
        net = self._build(state, probe=None, threshold=2, cooldown=0.1)
        for i in range(10):
            net.deposit("mid", Token(payload={"i": i}))  # seed downstream directly

        # Step synchronously until the breaker trips, so work stays queued behind it.
        for _ in range(10):
            net.step()
            net._await_inflight()
            if net.places["healthy"].is_open():
                break
        assert net.places["healthy"].is_open()
        assert len(net.places["mid"]) > 0  # remaining work held behind the open breaker

        state["down"] = False
        net.run(deadline=time.monotonic() + 2.0)  # cooldown elapses → optimistic close → resume
        assert net.places["healthy"].state == "closed"
        assert len(net.places["done"]) > 0


class TestSecondConsumer:
    """Validate the primitive generalises: two independent dependencies, two breakers, one net.

    Each breaker has its own `trip_predicate`, threshold, cooldown, and probe, and gates its own
    transition. Tripping one must not affect the other — the observable behaviour a hand-rolled
    per-dependency breaker would have, expressed entirely through the primitive.
    """

    def _two_breaker_net(self, state):
        def make_action(dep):
            def action(toks):
                if state[dep]["down"]:
                    raise ConnectionError(dep)
                return [toks[0].evolve(payload_updates={"dep": dep})]

            return action

        def make_probe(dep):
            def probe():
                state[dep]["probes"] += 1
                state[dep]["down"] = False  # the dependency recovers when first probed
                return True

            return probe

        net = PetriNet(max_workers=1)
        net.add_place(Place("done"))
        for dep in ("alpha", "beta"):
            net.add_place(Place(f"work_{dep}"))
            net.add_place(
                CircuitBreakerPlace(
                    f"healthy_{dep}",
                    trip_predicate=lambda e: isinstance(e, ConnectionError),
                    failure_threshold=state[dep]["threshold"],
                    cooldown_secs=state[dep]["cooldown"],
                    probe=make_probe(dep),
                )
            )
            net.add_transition(
                Transition(
                    f"call_{dep}",
                    inputs=[InputArc(f"work_{dep}"), InputArc(f"healthy_{dep}", test=True)],
                    outputs=[OutputArc("done")],
                    action=make_action(dep),
                    max_retries=0,
                    breaker=f"healthy_{dep}",
                )
            )
        return net

    def test_two_breakers_trip_and_recover_independently(self):
        state = {
            "alpha": {"down": True, "probes": 0, "threshold": 2, "cooldown": 1.0},
            "beta": {"down": False, "probes": 0, "threshold": 3, "cooldown": 2.0},
        }
        net = self._two_breaker_net(state)
        for i in range(6):
            net.deposit("work_alpha", Token(payload={"i": i}))
            net.deposit("work_beta", Token(payload={"i": i}))

        # alpha starts down and recovers on its first probe; beta is healthy throughout.
        net.drive_to_quiescence(max_ticks=50)

        # beta never opened, so its probe never ran; alpha opened once and recovered.
        assert state["beta"]["probes"] == 0
        assert net.places["healthy_beta"].state == "closed"
        assert state["alpha"]["probes"] == 1
        assert net.places["healthy_alpha"].state == "closed"
        # All beta tokens complete (6) plus alpha's survivors after its 2 dead-letters (4) = 10.
        assert len(net.places["done"]) == 10
        assert len(net.places["failed"]) == 2  # alpha's two failures before it tripped
