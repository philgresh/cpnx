"""Property-based fuzzing of the cpnx engine via a Hypothesis RuleBasedStateMachine.

Builds a small net that exercises every place type (`Place`, `ResourcePlace`,
`PacedResourcePlace`, `ThresholdPlace`, `SinkPlace`) with pass-through / resource-returning
actions, then fuzzes deposits, steps, and time advances while asserting three formal
invariants: k-bound respect, data-token conservation, and liveness (no stuck data tokens
in a genuinely dead marking).
"""

import time

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule
from hypothesis.strategies import composite

from cpnx import (
    BindingPolicy,
    InputArc,
    OutputArc,
    PacedResourcePlace,
    PetriNet,
    Place,
    ResourcePlace,
    SinkPlace,
    ThresholdPlace,
    Token,
    Transition,
)

SEED = 12345
WORK_BOUND = 8


def _pass_through(tokens: list[Token]) -> list[Token]:
    """Return every consumed token unchanged (data pass-through, resource return)."""
    return list(tokens)


def build_verification_net(seed: int) -> PetriNet:
    """Build a small net touching every place type; all actions are pass-through/resource-returning.

    Topology: P_in -> P_work -> (acquire+return P_scale) -> (acquire+return P_grinder)
    -> P_tray (ThresholdPlace) -> P_served (SinkPlace). The error place is overridden with a
    SinkPlace so dead-lettered tokens are counted toward conservation too.
    """
    places = [
        Place("P_in"),
        Place("P_work", bound=WORK_BOUND),
        ResourcePlace("P_scale", capacity=2),
        PacedResourcePlace("P_grinder", capacity=1, pacing_secs=1.0),
        ThresholdPlace("P_tray", threshold=2),
        SinkPlace("P_served", keep_last=5),
        SinkPlace("failed", keep_last=5),
    ]

    transitions = [
        Transition(
            name="t_intake",
            # Small but nonzero settle window to exercise the settle-window mechanism
            # without making the fuzz run slow (see CPNTestMachine._pending_settle_wait).
            inputs=[InputArc("P_in", count=1, settle_secs=0.02)],
            outputs=[OutputArc("P_work", count=1)],
            action=_pass_through,
        ),
        Transition(
            name="t_weigh",
            inputs=[
                InputArc("P_scale", count=1),
                InputArc("P_work", count=1),
            ],
            outputs=[
                OutputArc("P_scale", count=1),
                OutputArc("P_grinder_stage", count=1),
            ],
            action=_pass_through,
        ),
        Transition(
            name="t_grind",
            inputs=[
                InputArc("P_grinder", count=1),
                InputArc("P_grinder_stage", count=1),
            ],
            outputs=[
                OutputArc("P_grinder", count=1),
                OutputArc("P_tray", count=1),
            ],
            action=_pass_through,
        ),
        Transition(
            name="t_serve",
            inputs=[InputArc("P_tray", count=2)],
            outputs=[OutputArc("P_served", count=2)],
            action=_pass_through,
        ),
    ]
    # P_grinder_stage is an intermediate unbounded holding place between the scale and
    # the grinder (kept distinct from P_work so t_weigh's resource-arc + data-arc binding
    # is unambiguous).
    places.insert(2, Place("P_grinder_stage"))

    return PetriNet(
        max_workers=1,
        error_place="failed",
        places=places,
        transitions=transitions,
        binding_policy=BindingPolicy.RANDOM,
        seed=seed,
    )


@composite
def _token_strategy(draw):
    color = draw(st.sampled_from([None, "espresso", "oat_milk"]))
    payload = draw(
        st.dictionaries(
            st.text(max_size=5),
            st.integers() | st.booleans(),
            max_size=4,
        )
    )
    return Token(color=color, payload=payload)


class CPNTestMachine(RuleBasedStateMachine):
    """Fuzzes the verification net and checks bounds, conservation, and liveness."""

    def __init__(self):
        super().__init__()
        self.net = build_verification_net(SEED)
        self.deposited = 0
        self.clock = 0.0
        self._time_advanced = False

    def _drain(self):
        """Run step() to quiescence: keep stepping while work fires, is in flight, or is
        merely waiting out a settle window (net.is_dead() is an instantaneous check that
        does not know a settle window is about to elapse, so we wait it out explicitly
        rather than mistake it for a genuine deadlock).

        Once `advance_time` has ever been called, settle windows are measured against the
        net's logical clock, which real-time sleeping cannot advance — so in that mode we
        stop retrying immediately and leave any settle-blocked tokens to be recognized (and
        excluded) by `check_liveness`.
        """
        iterations = 0
        max_iterations = 10000
        while iterations < max_iterations:
            iterations += 1
            if self.net.step():
                continue
            if self.net.snapshot()["running_count"] > 0:
                time.sleep(0.001)
                continue
            if self._time_advanced:
                break
            wait = self._pending_settle_wait()
            if wait > 0:
                time.sleep(wait + 0.005)
                continue
            break

    def _pending_settle_wait(self) -> float:
        """Return the longest remaining settle-window wait (seconds), using wall-clock time.

        Only meaningful before `advance_time` has ever been called (real-time sleeping
        cannot advance a logical clock). Zero if nothing is currently settle-blocked.
        """
        now = time.monotonic()
        longest = 0.0
        for transition in self.net.transitions.values():
            for arc in transition.inputs:
                if arc.settle_secs <= 0:
                    continue
                place = self.net.places.get(arc.place)
                if place is None or len(place) == 0:
                    continue
                remaining = arc.settle_secs - (now - place.last_deposit_time)
                if remaining > longest:
                    longest = remaining
        return longest

    def _resource_blocked_places(self) -> set:
        """Return names of (non-resource) input places whose owning transition currently
        cannot fire only because a *resource* input arc (`ResourcePlace`/`PacedResourcePlace`)
        has no available permit right now — e.g. every permit is mid-cooldown on a
        `PacedResourcePlace`. A data token waiting behind an exhausted/cooling resource pool
        is legitimately blocked, not deadlocked.
        """
        now = self.net.model_time
        blocked = set()
        for transition in self.net.transitions.values():
            resource_starved = False
            for arc in transition.inputs:
                place = self.net.places.get(arc.place)
                if isinstance(place, (ResourcePlace, PacedResourcePlace)) and not place.can_retrieve(
                    arc.count, model_time=now
                ):
                    resource_starved = True
                    break
            if resource_starved:
                for arc in transition.inputs:
                    place = self.net.places.get(arc.place)
                    if not isinstance(place, (ResourcePlace, PacedResourcePlace)):
                        blocked.add(arc.place)
        return blocked

    def _settle_blocked_places(self) -> set:
        """Return names of places currently blocked only by an unmet settle window.

        Uses `net.model_time` (logical clock if `advance_time` was ever called, else
        wall-clock), mirroring the engine's own `_is_settle_time_met` check exactly.
        """
        now = self.net.model_time
        blocked = set()
        for transition in self.net.transitions.values():
            for arc in transition.inputs:
                if arc.settle_secs <= 0:
                    continue
                place = self.net.places.get(arc.place)
                if place is None or len(place) == 0:
                    continue
                elapsed = now - place.last_deposit_time_model
                if elapsed < arc.settle_secs:
                    blocked.add(arc.place)
        return blocked

    @rule(token=_token_strategy())
    def deposit(self, token):
        if not token.is_resource:
            self.deposited += 1
        self.net.deposit("P_in", token)
        self._drain()

    @rule()
    def step(self):
        self.net.step()
        self._drain()

    @rule(delta=st.floats(min_value=0.01, max_value=10.0, allow_nan=False, allow_infinity=False))
    def advance_time(self, delta):
        self.clock += delta
        self.net.advance_time(self.clock)
        self._time_advanced = True
        self._drain()

    @invariant()
    def check_bounds(self):
        for name, tokens in self.net.marking.items():
            place = self.net.places.get(name)
            if isinstance(place, Place) and place.bound is not None:
                assert len(tokens) <= place.bound, f"Place '{name}' exceeded bound {place.bound}"

    @invariant()
    def check_conservation(self):
        marking = self.net.marking
        live_data = 0
        for name, tokens in marking.items():
            place = self.net.places.get(name)
            if isinstance(place, SinkPlace):
                continue
            live_data += sum(1 for t in tokens if not t.is_resource)

        absorbed = self.net.places["P_served"]._absorbed + self.net.places["failed"]._absorbed

        assert self.deposited == live_data + absorbed, (
            f"Conservation violated: deposited={self.deposited}, live_data={live_data}, absorbed={absorbed}"
        )

    @invariant()
    def check_liveness(self):
        if not self.net.is_dead():
            return
        marking = self.net.marking
        settle_blocked = self._settle_blocked_places()
        resource_blocked = self._resource_blocked_places()
        stuck = 0
        for name, tokens in marking.items():
            place = self.net.places.get(name)
            if isinstance(place, SinkPlace):
                continue
            # A ThresholdPlace below its threshold is legitimately blocked waiting for a
            # batch to accumulate — that's not a deadlock, so exclude it deliberately.
            if isinstance(place, ThresholdPlace) and len(tokens) < place.threshold:
                continue
            # A place whose sole consuming arc has an unmet settle window is also
            # legitimately blocked (waiting out the settle window), not deadlocked.
            if name in settle_blocked:
                continue
            # A data token waiting on a transition whose resource arc (ResourcePlace /
            # PacedResourcePlace) has no available permit right now (e.g. mid-cooldown) is
            # legitimately blocked, not deadlocked.
            if name in resource_blocked:
                continue
            stuck += sum(1 for t in tokens if not t.is_resource)
        assert stuck == 0, f"Dead marking with {stuck} stuck non-resource token(s)"


TestCPNStateMachine = CPNTestMachine.TestCase
TestCPNStateMachine.settings = settings(max_examples=50, stateful_step_count=20, deadline=None)
