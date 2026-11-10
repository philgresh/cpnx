"""`drive_to_quiescence(inline_actions=True)` runs each firing on the driving thread (#79).

The drive already keeps one firing in flight, so running it inline must change nothing but the
thread: the same firings in the same order, the same retries and dead letters, the same clock
advances, and the same seeded random streams afterwards. These tests drive identical nets both
ways and compare everything observable.
"""

import threading

import pytest

from cpnx import (
    BindingPolicy,
    InputArc,
    OutputArc,
    PacedResourcePlace,
    PetriNet,
    Place,
    RetryPolicy,
    SinkPlace,
    SubstitutionTransition,
    Token,
    Transition,
)

SEED = 20261110
# A fixed logical start, so clock readings compare across runs instead of tracking the wall clock.
START = 1e7


def _even(toks: list[Token]) -> bool:
    return toks[0].payload["i"] % 2 == 0


class _Recorder:
    """Actions and callbacks for one net, writing everything they see to one log."""

    def __init__(self) -> None:
        self.log: list[tuple] = []
        self.threads: set[int] = set()

    def work(self, toks: list[Token]) -> list[Token]:
        self.threads.add(threading.get_ident())
        job = next(t for t in toks if not t.is_resource)
        self.log.append(("work", job.payload["i"], job.attempts))
        if job.payload["i"] % 5 == 0 and job.attempts < 2:
            raise ValueError(f"job {job.payload['i']} failed")
        return [job]

    def route(self, name: str):
        def action(toks: list[Token]) -> list[Token]:
            self.threads.add(threading.get_ident())
            self.log.append((name, toks[0].payload["i"]))
            return toks

        return action

    def attach(self, net: PetriNet) -> None:
        net.on_transition_fired = lambda name, _duration: self.log.append(("fired", name))
        net.on_error = lambda name, error, token: self.log.append(
            ("error", name, str(error), token.payload["i"] if token is not None else None)
        )
        net.on_token_dead_lettered = lambda name, token: self.log.append(("dead", name, token.payload["i"]))


def _flat_net(rec: _Recorder) -> PetriNet:
    """Every order-sensitive path at once: RANDOM bindings under a guard, a paced pool,
    jittered retries that dead-letter, and a scheduler tie-break between two routes."""
    net = PetriNet(
        seed=SEED,
        binding_policy=BindingPolicy.RANDOM,
        places=[
            Place("jobs"),
            PacedResourcePlace("R", capacity=2, pacing_secs=0.5),
            Place("mid"),
            Place("went_left"),
            Place("went_right"),
            SinkPlace("failed", keep_last=100),
        ],
        transitions=[
            Transition(
                "work",
                [InputArc("jobs"), InputArc("R")],
                [OutputArc("mid")],
                rec.work,
                guard=None,
                priority=5,
                max_retries=1,
                retry_policy=RetryPolicy(delay=0.3, backoff=2.0, max_delay=2.0, jitter=True),
            ),
            Transition("left", [InputArc("mid")], [OutputArc("went_left")], rec.route("left"), guard=_even),
            Transition("right", [InputArc("mid")], [OutputArc("went_right")], rec.route("right")),
        ],
    )
    rec.attach(net)
    net.advance_time(START)
    for i in range(40):
        net.deposit("jobs", Token(payload={"i": i}))
    return net


def _snapshot(net: PetriNet) -> dict:
    return {
        name: sorted((t.payload.get("i"), t.attempts, t.available_at) for t in tokens if not t.is_resource)
        for name, tokens in net.marking.items()
    }


def _drive(build, inline: bool):
    rec = _Recorder()
    net = build(rec)
    with net:
        result = net.drive_to_quiescence(inline_actions=inline)
        assert net.is_quiescent()
        state = (
            result,
            net.model_time,
            rec.log,
            _snapshot(net),
            net._rng.getstate(),
            net._retry_rng.getstate(),
        )
    return state, rec.threads


def test_flat_net_drives_identically_inline():
    pooled, pool_threads = _drive(_flat_net, inline=False)
    inline, inline_threads = _drive(_flat_net, inline=True)
    assert inline == pooled
    # The run covered what it is meant to: retries, dead letters and both routes.
    log = pooled[2]
    assert any(entry[0] == "dead" for entry in log)
    assert any(entry[0] == "work" and entry[2] == 1 for entry in log)
    assert {"left", "right"} <= {entry[0] for entry in log}
    assert pooled[0].ticks > 0
    assert inline_threads == {threading.get_ident()}
    assert threading.get_ident() not in pool_threads


def _subnet_net(rec: _Recorder) -> PetriNet:
    """A substitution transition whose subnet fails some firings, so the parent rolls back
    and restores the subnet, retrying on a jittered schedule."""
    subnet = PetriNet(
        seed=SEED + 1,
        places=[Place("Orders"), PacedResourcePlace("Grinder", capacity=1, pacing_secs=1.0), Place("Ready")],
        transitions=[
            Transition(
                "Brew",
                [InputArc("Orders"), InputArc("Grinder")],
                [OutputArc("Ready")],
                rec.work,
                max_retries=0,
            )
        ],
    )
    parent = PetriNet(
        seed=SEED,
        places=[Place("Queue"), Place("Served"), SinkPlace("failed", keep_last=100)],
        transitions=[
            SubstitutionTransition(
                name="Cafe",
                inputs=[InputArc("Queue")],
                outputs=[OutputArc("Served")],
                action=lambda tokens: tokens,
                subnet=subnet,
                port_socket_map={"Orders": "Queue", "Ready": "Served"},
                max_retries=3,
                retry_policy=RetryPolicy(delay=0.25, backoff=2.0, jitter=True),
            )
        ],
    )
    rec.attach(parent)
    parent.advance_time(START)
    subnet.advance_time(START)
    for i in range(12):
        parent.deposit("Queue", Token(payload={"i": i}))
    return parent


def test_subnet_net_drives_identically_inline():
    pooled, _ = _drive(_subnet_net, inline=False)
    inline, inline_threads = _drive(_subnet_net, inline=True)
    assert inline == pooled
    assert any(entry[0] == "error" for entry in pooled[2]), "some firings must roll back"
    assert inline_threads == {threading.get_ident()}, "the subnet's actions run inline too"


def test_step_and_the_default_drive_still_use_the_thread_pool():
    rec = _Recorder()
    net = PetriNet(places=[Place("mid"), Place("went_right")])
    net.add_transition(Transition("right", [InputArc("mid")], [OutputArc("went_right")], rec.route("right")))
    with net:
        net.deposit("mid", Token(payload={"i": 0}))
        net.drive_to_quiescence(inline_actions=True)
        assert rec.threads == {threading.get_ident()}
        rec.threads.clear()
        net.deposit("mid", Token(payload={"i": 1}))
        net.step()
        net._await_inflight()
        net.deposit("mid", Token(payload={"i": 2}))
        net.drive_to_quiescence()
    assert len(rec.threads) >= 1 and threading.get_ident() not in rec.threads
    assert len(net.marking["went_right"]) == 3


def _interrupt(toks: list[Token]) -> list[Token]:
    raise KeyboardInterrupt


def test_inline_keyboard_interrupt_propagates_after_rollback():
    net = PetriNet(places=[Place("in"), Place("out")])
    net.add_transition(Transition("t", [InputArc("in")], [OutputArc("out")], _interrupt))
    net.deposit("in", Token(payload={"i": 0}))
    with net:
        with pytest.raises(KeyboardInterrupt):
            net.drive_to_quiescence(inline_actions=True)
        assert net._running_count == 0
        assert [t.attempts for t in net.marking["in"]] == [1], "the token was rolled back for a retry"


def _hang(toks: list[Token]) -> list[Token]:
    threading.Event().wait(5.0)
    return toks


def test_action_timeout_still_bounds_an_inline_drive():
    errors: list[BaseException] = []
    net = PetriNet(places=[Place("in"), Place("out"), SinkPlace("failed", keep_last=10)])
    net.add_transition(
        Transition("t", [InputArc("in")], [OutputArc("out")], _hang, action_timeout_secs=0.05, max_retries=0)
    )
    net.on_error = lambda _name, error, _token: errors.append(error)
    net.deposit("in", Token(payload={"i": 0}))
    with net:
        result = net.drive_to_quiescence(inline_actions=True)
    assert result.steps == 1
    assert len(errors) == 1 and "timeout" in str(errors[0])
    assert len(net.marking["failed"]) == 1
