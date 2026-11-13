"""Shared-place binding, checked across a sequence of operations (issue #75).

`test_every_arc_shape_binds_exactly_when_distinct_tokens_exist` compares the engine with a
brute-force oracle on one freshly built marking. This machine draws a net the same way, then
deposits tokens (some not available until later), advances the logical clock and fires, and
compares again after every operation. So it also covers what one marking cannot: a place's
state after a firing took tokens from its middle or drained it, tokens that become available
as the clock moves, and the scheduler behind `step()`, which must agree with a direct probe.

Each example runs two nets through the same operations: one with a search limit too large to
run out, which must match the oracle exactly, and one with a small limit, which may miss a
binding only by reporting that its search ran out.
"""

import contextlib
import copy
import functools
import warnings
from dataclasses import dataclass, field

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule

# The arc kinds and the oracle are the one-shot property's own, so the two tests cannot drift apart.
from test_shared_place_arcs import (
    _KINDS,
    _assert_valid_binding,
    _distinct_binding_exists,
    _record_exhaustion,
    _token_payloads,
)

from cpnx import BindingPolicy, InputArc, PetriNet, Place, Token, Transition

_PLACES = ("p", "q", "r")
_PLACE_CAP = 6  # tokens a place may hold, ready or not: it bounds the oracle's brute force
_EXACT_LIMIT = 100_000  # more candidates than `_PLACE_CAP` tokens per place can form
_START = 1.0  # the logical clock's first value


def _even_sum(tokens: list[Token]) -> bool:
    return sum(token.payload["n"] for token in tokens) % 2 == 0


def _always(tokens: list[Token]) -> bool:
    return True


@functools.cache
def _checked_arc(place: str, kind: str) -> InputArc:
    return InputArc(place, **_KINDS[kind])


def _new_arc(place: str, kind: str) -> InputArc:
    """A new arc of `kind` on `place`.

    Constructing an `InputArc` checks each callable's source, which parses the module that
    defines it: some 20 ms per arc for the callables of `_KINDS`, more than everything else an
    example does. A copy of an arc already checked is a separate arc object without that cost.
    """
    return copy.copy(_checked_arc(place, kind))


_place_indexes = st.integers(0, len(_PLACES) - 1)  # taken modulo the number of places the net has
_deposits = st.lists(_token_payloads, min_size=1, max_size=3)


@st.composite
def _net_shapes(draw) -> tuple[tuple[str, ...], list[tuple[str, str]]]:
    """Two or three places and two to four input arcs over them, as `(place, kind)` pairs."""
    places = _PLACES[: draw(st.integers(2, 3))]
    arc = st.tuples(st.sampled_from(places), st.sampled_from(sorted(_KINDS)))
    return places, draw(st.lists(arc, min_size=2, max_size=4))


@dataclass
class _Subject:
    """One net under test, with what `check_binding` last saw of it."""

    net: PetriNet
    exact: bool  # its search limit cannot run out, so it must match the oracle exactly
    exhausted: list[str]
    ready: dict[str, list[Token]] = field(default_factory=dict)  # the tokens available now, per place
    bound: bool = False  # the engine binds them


class SharedPlaceMachine(RuleBasedStateMachine):
    """Deposits, clock advances and firings on a drawn net, checked against brute force."""

    def __init__(self) -> None:
        super().__init__()
        self.places: tuple[str, ...] = ()
        self.arcs: list[tuple[str, str]] = []
        self.guard = _always
        self.now = _START
        self.subjects: list[_Subject] = []
        self._nets = contextlib.ExitStack()  # each net owns thread pools

    @initialize(
        shape=_net_shapes(),
        guarded=st.booleans(),
        policy=st.sampled_from([BindingPolicy.FIRST, BindingPolicy.RANDOM, BindingPolicy.PRIORITY]),
        small_limit=st.integers(1, 8),
        one_arc_object=st.booleans(),
    )
    def build(self, shape, guarded, policy, small_limit, one_arc_object):
        self.places, self.arcs = shape
        self.guard = _even_sum if guarded else _always
        for limit in (_EXACT_LIMIT, small_limit):
            net = self._nets.enter_context(PetriNet(seed=0, binding_search_limit=limit))
            for name in self.places:
                net.add_place(Place(name))
            made: dict[tuple[str, str], InputArc] = {}
            inputs = []
            for place, kind in self.arcs:  # arcs alike are one object or one each: the engine must not care
                if not one_arc_object or (place, kind) not in made:
                    made[place, kind] = _new_arc(place, kind)
                inputs.append(made[place, kind])
            transition = Transition(
                name="t",
                inputs=inputs,
                outputs=[],
                action=lambda tokens: [],
                binding_policy=policy,
                **({"guard": _even_sum} if guarded else {}),
            )
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # two draining arcs on one place warn; here they are a case
                net.add_transition(transition)
            net.advance_time(self.now)
            self.subjects.append(_Subject(net, exact=limit == _EXACT_LIMIT, exhausted=_record_exhaustion(net)))

    def teardown(self) -> None:
        self._nets.close()

    def _ready(self, net: PetriNet) -> dict[str, list[Token]]:
        return {name: net.places[name].peek(_PLACE_CAP, model_time=net.model_time) for name in self.places}

    def _deposit(self, place: int, payloads: list[dict], **timing: float) -> None:
        name = self.places[place % len(self.places)]
        for subject in self.subjects:
            room = _PLACE_CAP - len(subject.net.places[name])
            for payload in payloads[:room]:
                subject.net.deposit(name, Token(payload=dict(payload), **timing))

    @rule(place=_place_indexes, payloads=_deposits)
    def deposit(self, place, payloads):
        self._deposit(place, payloads)

    @rule(place=_place_indexes, payloads=_deposits, delay=st.sampled_from([0.5, 2.0]))
    def deposit_cooling(self, place, payloads, delay):
        self._deposit(place, payloads, available_at=self.now + delay)

    @rule(delta=st.sampled_from([0.5, 1.0, 2.0]))
    def advance_clock(self, delta):
        self.now += delta
        for subject in self.subjects:
            subject.net.advance_time(self.now)

    @precondition(lambda self: any(subject.bound for subject in self.subjects))
    @rule(times=st.integers(1, 3))
    def step(self, times):
        # Only a marking some net can fire on is worth a step; `check_binding` covers the others
        # through `is_dead()`. The later steps of a run fire on what the first left behind.
        for n in range(times):
            if n:
                self.check_binding()
            for subject in self.subjects:
                self._step(subject)

    def _step(self, subject: _Subject) -> None:
        # `check_binding` ran on this marking, so `subject` holds the tokens that were ready and
        # whether the engine bound them: firing must agree.
        net = subject.net
        before = net.marking
        fired = net.step()
        net._await_inflight()
        assert fired is subject.bound, "step() and a direct probe disagree"
        after = net.marking
        gone = {
            name: [token for token in before[name] if not any(token is kept for kept in after[name])]
            for name in self.places
        }
        if not fired:
            assert not any(gone.values()), "a step that fired nothing took tokens"
            return
        for name in self.places:
            assert len(after[name]) == len(before[name]) - len(gone[name])
            assert all(any(token is ready for ready in subject.ready[name]) for token in gone[name])
            if any(arc == (name, "all") for arc in self.arcs):
                assert len(gone[name]) == len(subject.ready[name]), "a drained place kept an available token"
            else:
                demand = sum(2 if kind == "pair" else 1 for place, kind in self.arcs if place == name)
                assert len(gone[name]) == demand, "a place lost more or less than its arcs take"
        # The tokens that left are a binding on their own: filters, join keys and the guard hold.
        assert _distinct_binding_exists(self.arcs, gone, self.guard)

    @invariant()
    def check_binding(self):
        for subject in self.subjects:
            net = subject.net
            transition = net.transitions["t"]
            now = net.model_time
            subject.ready = ready = self._ready(net)
            expected = _distinct_binding_exists(self.arcs, ready, self.guard)

            subject.exhausted.clear()
            probe = net._binding_exists(transition, now)
            probe_ran_out = bool(subject.exhausted)
            subject.exhausted.clear()
            binding = net._resolve_binding(transition, now)
            resolve_ran_out = bool(subject.exhausted)
            subject.bound = binding is not None

            assert probe is subject.bound, "the probe and the firing path disagree"
            assert net.is_dead() is (not probe), "is_dead() and a direct probe disagree"
            if binding is not None:
                assert expected, "the engine binds where no distinct tokens exist"
                _assert_valid_binding(binding, self.arcs, ready, self.guard)
            if subject.exact:
                assert subject.bound is expected, "the engine and brute force disagree on whether a binding exists"
                assert not probe_ran_out and not resolve_ran_out
            elif expected and binding is None:
                assert probe_ran_out and resolve_ran_out, "a search missed a binding without reporting that it ran out"


TestSharedPlaceStateMachine = SharedPlaceMachine.TestCase
TestSharedPlaceStateMachine.settings = settings(max_examples=300, stateful_step_count=30, deadline=None)
