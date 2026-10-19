"""Retry scheduling: how long a rolled-back data token waits before each retry.

A `RetryPolicy` schedules a 0-based attempt `n` (the token's `attempts` count when the
firing failed) at `min(delay * backoff ** n, max_delay)`, drawn uniformly from `[0, that]`
when `jitter` is set — capped exponential backoff with AWS-style full jitter. A transition
uses its own policy when it has one and the net's default otherwise.

Three layers are covered:

1. `RetryPolicy.delay_for` directly, which pins the exact schedule without running a net.
2. `_RetrySchedule`, the per-transition resolution (transition policy, else net default).
3. End-to-end through a failing transition on the **logical clock** (`advance_time`), so
   the `available_at` a retried token lands on is asserted exactly, with no wall-clock
   sleeping (same technique as `test_chronology.py::test_retry_delay_respects_model_time`).
"""

import math
import random
import time

import pytest

from cpnx.engine import PetriNet
from cpnx.places import Place, ResourcePlace
from cpnx.retries import DEFAULT_RETRY_POLICY, RetryPolicy, _RetrySchedule
from cpnx.tokens import Token
from cpnx.transitions import InputArc, OutputArc, Transition


def _boom(tokens):
    raise RuntimeError("transient")


def _schedule(policy: RetryPolicy, attempts: int, rng: random.Random | None = None) -> list[float]:
    """The delays the policy produces for attempts `0..attempts-1`."""
    return [policy.delay_for(n, rng) for n in range(attempts)]


def _settle(net: PetriNet, timeout: float = 5.0) -> None:
    """Block until no action is in flight, so the rollback has been committed."""
    deadline = time.monotonic() + timeout
    while net._running_count > 0 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert net._running_count == 0


def _retry_net(*, transition_policy: RetryPolicy | None = None, **net_kwargs) -> PetriNet:
    """A one-transition net whose action always raises, with infinite retries."""
    net = PetriNet(max_workers=1, **net_kwargs)
    net.add_place(Place("in"))
    net.add_place(Place("out"))
    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("in")],
            outputs=[OutputArc("out")],
            action=_boom,
            max_retries=None,  # never dead-letter: we want the whole schedule
            retry_policy=transition_policy,
        )
    )
    return net


def _observed_delays(net: PetriNet, retries: int, place: str = "in") -> list[float]:
    """Fire `retries` failing firings on the logical clock, collecting each retry delay.

    The clock is advanced to exactly the token's `available_at` each round, so the next
    delay is read as `available_at - now` with no wall-clock interference.
    """
    now = 100.0
    net.advance_time(now)
    net.deposit(place, Token(payload={"n": 1}))

    delays: list[float] = []
    for _ in range(retries):
        assert net.step() is True
        _settle(net)
        (rolled_back,) = net.marking[place]
        delays.append(rolled_back.available_at - now)
        now = rolled_back.available_at
        net.advance_time(now)
    return delays


class TestFlatScheduleIsUnchanged:
    """The default policy must reproduce the pre-backoff flat schedule."""

    def test_policy_defaults_are_flat(self):
        assert _schedule(RetryPolicy(), 6) == [1.0, 1.0, 1.0, 1.0, 1.0, 1.0]

    def test_net_default_is_a_flat_one_second(self):
        assert PetriNet().retry_policy == DEFAULT_RETRY_POLICY
        assert DEFAULT_RETRY_POLICY == RetryPolicy(delay=1.0, backoff=1.0, max_delay=None, jitter=False)

    def test_retry_delay_shorthand_still_works(self):
        """`retry_delay=` predates policies and must keep behaving exactly as it did."""
        net = PetriNet(retry_delay=2.0)
        assert net.retry_delay == 2.0
        assert net.retry_policy == RetryPolicy(delay=2.0)

    def test_end_to_end_flat(self):
        with _retry_net(retry_delay=2.0) as net:
            assert _observed_delays(net, 4) == [2.0, 2.0, 2.0, 2.0]

    def test_backoff_of_one_ignores_the_cap(self):
        """A cap below the base still clamps — `min` is applied unconditionally."""
        assert _schedule(RetryPolicy(delay=5.0, max_delay=30.0), 3) == [5.0, 5.0, 5.0]
        assert _schedule(RetryPolicy(delay=5.0, max_delay=3.0), 3) == [3.0, 3.0, 3.0]


class TestCappedExponentialSchedule:
    def test_policy_doubles_then_saturates(self):
        policy = RetryPolicy(delay=1.0, backoff=2.0, max_delay=10.0)
        assert _schedule(policy, 7) == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0, 10.0]

    def test_policy_uncapped_grows_forever(self):
        assert _schedule(RetryPolicy(delay=0.5, backoff=3.0), 4) == [0.5, 1.5, 4.5, 13.5]

    def test_fractional_backoff(self):
        policy = RetryPolicy(delay=1.0, backoff=1.5, max_delay=3.0)
        assert _schedule(policy, 4) == pytest.approx([1.0, 1.5, 2.25, 3.0])

    def test_end_to_end_doubles_then_saturates(self):
        policy = RetryPolicy(delay=1.0, backoff=2.0, max_delay=10.0)
        with _retry_net(retry_policy=policy) as net:
            assert _observed_delays(net, 6) == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0]

    def test_attempts_counter_drives_the_schedule(self):
        """The delay is read off the token's own `attempts`, not a per-transition counter."""
        policy = RetryPolicy(delay=1.0, backoff=2.0, max_delay=100.0)
        assert policy.delay_for(0) == 1.0
        assert policy.delay_for(5) == 32.0

    def test_overflowing_exponent_saturates_instead_of_raising(self):
        """An unbounded net with `backoff > 1` can run the exponent past the float range."""
        assert RetryPolicy(delay=1.0, backoff=2.0).delay_for(5000) == math.inf
        assert RetryPolicy(delay=1.0, backoff=2.0, max_delay=60.0).delay_for(5000) == 60.0


class TestPerTransitionPolicy:
    """A transition paces its own dependency; the net's policy is only the fallback."""

    def test_transition_policy_wins_over_the_net_default(self):
        schedule = _RetrySchedule(default=RetryPolicy(delay=9.0))
        mine = RetryPolicy(delay=1.0, backoff=2.0)
        assert [schedule.delay_for(mine, n) for n in range(3)] == [1.0, 2.0, 4.0]

    def test_transition_without_a_policy_inherits_the_net_default(self):
        schedule = _RetrySchedule(default=RetryPolicy(delay=9.0, backoff=3.0))
        assert [schedule.delay_for(None, n) for n in range(3)] == [9.0, 27.0, 81.0]

    def test_two_transitions_keep_separate_schedules(self):
        """The point of the feature: one net, two dependencies, two different pacings."""
        fast = RetryPolicy(delay=0.5)
        slow = RetryPolicy(delay=4.0, backoff=2.0)
        with PetriNet(max_workers=1) as net:
            net.advance_time(100.0)
            for name, policy in (("fast", fast), ("slow", slow)):
                net.add_place(Place(name))
                net.add_transition(
                    Transition(
                        name=f"t_{name}",
                        inputs=[InputArc(name)],
                        outputs=[],
                        action=_boom,
                        max_retries=None,
                        retry_policy=policy,
                        priority=1 if name == "fast" else 2,
                    )
                )

            net.deposit("fast", Token(payload={"n": 1}))
            net.deposit("slow", Token(payload={"n": 2}))
            assert net.step() is True  # fast fires first (lower priority value)
            _settle(net)
            assert net.step() is True
            _settle(net)

            assert net.marking["fast"][0].available_at == 100.5
            assert net.marking["slow"][0].available_at == 104.0

    def test_end_to_end_transition_policy(self):
        policy = RetryPolicy(delay=0.5, backoff=2.0, max_delay=2.0)
        with _retry_net(transition_policy=policy, retry_delay=60.0) as net:
            # The net default (60s) is never used: this transition has its own policy.
            assert _observed_delays(net, 5) == [0.5, 1.0, 2.0, 2.0, 2.0]

    def test_a_policy_can_be_shared_by_many_transitions(self):
        """Policies are immutable values, so one instance can pace a whole dependency."""
        shared = RetryPolicy(delay=0.25, backoff=2.0)
        with PetriNet(max_workers=1) as net:
            net.add_place(Place("p"))
            transitions = [
                Transition(name=f"t{i}", inputs=[InputArc("p")], outputs=[], action=_boom, retry_policy=shared)
                for i in range(3)
            ]
            for t in transitions:
                net.add_transition(t)
            assert all(t.retry_policy is shared for t in transitions)

    def test_policy_is_assignable_after_construction(self):
        t = Transition(name="t", inputs=[], outputs=[], action=_boom)
        assert t.retry_policy is None
        t.retry_policy = RetryPolicy(delay=2.0)
        assert t.retry_policy.delay == 2.0

    @pytest.mark.parametrize("bad", ["2", 2.0, object()])
    def test_a_non_policy_is_rejected(self, bad):
        """Caught at assignment: the rollback path reads this while holding consumed tokens."""
        with pytest.raises(TypeError, match="retry_policy must be a RetryPolicy or None"):
            Transition(name="t", inputs=[], outputs=[], action=_boom, retry_policy=bad)


class TestFullJitter:
    """Full jitter: a uniform draw in `[0, capped delay]` (AWS "Exponential Backoff And Jitter")."""

    def test_draws_are_within_the_capped_window(self):
        rng = random.Random(1234)
        policy = RetryPolicy(delay=1.0, backoff=2.0, max_delay=10.0, jitter=True)
        for n in range(200):
            ceiling = min(1.0 * 2.0**n, 10.0) if n < 10 else 10.0
            assert 0.0 <= policy.delay_for(n, rng) <= ceiling

    def test_seeded_rng_is_reproducible(self):
        policy = RetryPolicy(delay=1.0, backoff=2.0, max_delay=8.0, jitter=True)
        a = _schedule(policy, 6, random.Random(7))
        assert a == _schedule(policy, 6, random.Random(7))
        # A different seed gives a different stream (so the draws are really being used).
        assert a != _schedule(policy, 6, random.Random(8))

    def test_draws_are_not_all_the_ceiling(self):
        """Guard against a policy that quietly ignores its RNG."""
        delays = _schedule(RetryPolicy(delay=10.0, jitter=True), 20, random.Random(99))
        assert all(0.0 <= d <= 10.0 for d in delays)
        assert len(set(delays)) > 1

    def test_jitter_without_an_rng_is_the_unjittered_ceiling(self):
        """Called directly with no stream — the engine always supplies one."""
        assert _schedule(RetryPolicy(delay=4.0, jitter=True), 3) == [4.0, 4.0, 4.0]

    def test_infinite_window_is_left_unjittered(self):
        """`uniform(0, inf)` can yield NaN; an uncapped overflow is a park, not a draw."""
        policy = RetryPolicy(delay=1.0, backoff=2.0, jitter=True)
        assert policy.delay_for(5000, random.Random(0)) == math.inf

    def test_end_to_end_uses_the_injected_rng(self):
        """With a seeded `retry_rng`, the net's retry deadlines are exactly predictable."""
        policy = RetryPolicy(delay=1.0, backoff=2.0, max_delay=6.0, jitter=True)
        expected = _schedule(policy, 5, random.Random(42))
        with _retry_net(retry_policy=policy, retry_rng=random.Random(42)) as net:
            assert _observed_delays(net, 5) == pytest.approx(expected)

    def test_jitter_off_ignores_the_rng(self):
        policy = RetryPolicy(delay=1.0, backoff=2.0)
        with _retry_net(retry_policy=policy, retry_rng=random.Random(42)) as net:
            assert _observed_delays(net, 3) == [1.0, 2.0, 4.0]

    def test_seed_makes_jitter_reproducible(self):
        """`seed=` alone seeds the jitter stream — no separate `retry_rng` needed."""
        policy = RetryPolicy(delay=1.0, backoff=2.0, max_delay=6.0, jitter=True)
        runs = []
        for _ in range(2):
            with _retry_net(retry_policy=policy, seed=5) as net:
                runs.append(_observed_delays(net, 4))
        assert runs[0] == runs[1]

    def test_jitter_draws_do_not_shift_the_scheduler_stream(self):
        """Retry jitter draws from its own RNG, so failures cannot perturb seeded firing."""
        net = PetriNet(seed=11, retry_policy=RetryPolicy(jitter=True))
        reference = random.Random(11)
        assert [net._rng.random() for _ in range(3)] == [reference.random() for _ in range(3)]
        assert net._rng is not net._retry_rng

    def test_one_jitter_stream_is_shared_by_every_transition(self):
        net = PetriNet(retry_rng=random.Random(3), retry_policy=RetryPolicy(jitter=True))
        assert net._resolve_retry_schedule().rng is net._retry_rng


class TestImmediateDeadLetterIsUnaffected:
    def test_max_retries_zero_dead_letters_at_once(self):
        """Backoff/jitter never delay a `max_retries=0` transition's dead-letter."""
        policy = RetryPolicy(delay=1.0, backoff=10.0, max_delay=600.0, jitter=True)
        with PetriNet(max_workers=1, retry_policy=policy, retry_rng=random.Random(3)) as net:
            net.advance_time(100.0)
            net.add_place(Place("in"))
            net.add_place(ResourcePlace("permit", capacity=1))
            net.add_transition(
                Transition(
                    name="t",
                    inputs=[InputArc("in"), InputArc("permit")],
                    outputs=[OutputArc("permit")],
                    action=_boom,
                    max_retries=0,
                )
            )
            net.deposit("in", Token(payload={"n": 1}))
            assert net.step() is True
            _settle(net)

            assert net.marking["in"] == ()
            (dead,) = net.marking["failed"]
            assert dead.attempts == 0
            assert dead.available_at == 0.0  # immediately retrievable, not backed off
            assert len(net.places["permit"]) == 1  # permit returned, never delayed

    def test_exhausted_retries_dead_letter_without_a_delay(self):
        with _retry_net(transition_policy=RetryPolicy(delay=1.0, backoff=2.0)) as net:
            net.transitions["t"].max_retries = 2
            net.advance_time(100.0)
            net.deposit("in", Token(payload={"n": 1}))

            assert net.step() is True  # attempt 0 -> retry at 101.0
            _settle(net)
            assert net.marking["in"][0].available_at == 101.0

            net.advance_time(101.0)
            assert net.step() is True  # attempt 1 -> retry at 103.0
            _settle(net)
            assert net.marking["in"][0].available_at == 103.0

            net.advance_time(103.0)
            assert net.step() is True  # attempts exhausted -> dead-letter
            _settle(net)
            assert net.marking["in"] == ()
            (dead,) = net.marking["failed"]
            assert dead.attempts == 2
            assert dead.available_at == 0.0


class TestPolicyValidation:
    """A schedule that could only stall or hot-loop is rejected where it is written.

    Each of these fails *silently* at run time if it gets through: a negative delay
    schedules the retry in the past (a hot loop on a failing transition), a `NaN` makes
    every `available_at` comparison false (the token is never retrievable again — a
    stalled net, not an error), and an infinity parks the token for good. `NaN` is the
    one that needs its own test, since it slips past every `<` check.

    Validating here also keeps the rollback path raise-free: it runs under the engine lock
    holding tokens it has consumed but not yet returned, so an exception there strands them.
    """

    @pytest.mark.parametrize("backoff", [0.0, 0.5, -1.0])
    def test_backoff_below_one_is_rejected(self, backoff):
        with pytest.raises(ValueError, match="RetryPolicy.backoff must be >= 1.0"):
            RetryPolicy(backoff=backoff)

    def test_negative_delay_is_rejected(self):
        with pytest.raises(ValueError, match="RetryPolicy.delay must be >= 0"):
            RetryPolicy(delay=-1.0)

    def test_negative_max_delay_is_rejected(self):
        with pytest.raises(ValueError, match="RetryPolicy.max_delay must be >= 0"):
            RetryPolicy(max_delay=-1.0)

    @pytest.mark.parametrize("field", ["delay", "backoff", "max_delay"])
    def test_nan_is_rejected(self, field):
        with pytest.raises(ValueError, match="must not be NaN"):
            RetryPolicy(**{field: math.nan})

    @pytest.mark.parametrize("field", ["delay", "backoff"])
    def test_infinity_is_rejected(self, field):
        with pytest.raises(ValueError, match="must be finite"):
            RetryPolicy(**{field: math.inf})

    def test_infinite_max_delay_is_allowed_as_uncapped(self):
        policy = RetryPolicy(delay=1.0, backoff=2.0, max_delay=math.inf)
        assert _schedule(policy, 4) == [1.0, 2.0, 4.0, 8.0]

    @pytest.mark.parametrize("field", ["delay", "backoff", "max_delay"])
    def test_non_numbers_are_rejected(self, field):
        with pytest.raises(TypeError, match="must be a number"):
            RetryPolicy(**{field: "2"})

    def test_none_delay_is_rejected(self):
        """`None` is only meaningful for the cap, where it spells "uncapped"."""
        with pytest.raises(TypeError, match="RetryPolicy.delay must be a number"):
            RetryPolicy(delay=None)
        assert RetryPolicy(max_delay=None).max_delay is None

    def test_zero_max_delay_is_allowed(self):
        """A zero cap means "retry immediately" — degenerate, but well defined."""
        assert RetryPolicy(max_delay=0.0).delay_for(3) == 0.0

    def test_integers_are_accepted(self):
        assert _schedule(RetryPolicy(delay=2, backoff=3, max_delay=12), 3) == [2.0, 6.0, 12.0]

    def test_policies_compare_by_value(self):
        assert RetryPolicy(delay=1.0, backoff=2.0) == RetryPolicy(delay=1.0, backoff=2.0)
        assert RetryPolicy(delay=1.0) != RetryPolicy(delay=2.0)


class TestNetValidation:
    def test_retry_policy_must_be_a_policy(self):
        with pytest.raises(TypeError, match="retry_policy must be a RetryPolicy"):
            PetriNet(retry_policy=RetryPolicy)  # the class, not an instance

    def test_retry_delay_and_retry_policy_are_mutually_exclusive(self):
        with pytest.raises(ValueError, match="Pass retry_delay or retry_policy, not both"):
            PetriNet(retry_delay=1.0, retry_policy=RetryPolicy(delay=2.0))

    @pytest.mark.parametrize("bad", [-1.0, math.nan, math.inf, "2"])
    def test_bad_retry_delay_is_rejected(self, bad):
        with pytest.raises((ValueError, TypeError), match="retry_delay"):
            PetriNet(retry_delay=bad)

    def test_rng_without_uniform_is_rejected(self):
        """Checked at construction: the draw happens mid-rollback, holding consumed tokens."""
        with pytest.raises(TypeError, match="retry_rng must have a callable `uniform"):
            PetriNet(retry_rng=object())


class TestLiveMutability:
    def test_the_policy_is_read_per_rollback(self):
        """A mid-life change takes effect on the next rollback — resolution happens then."""
        net = PetriNet()
        assert _schedule(net._resolve_retry_schedule().default, 3) == [1.0, 1.0, 1.0]

        net.retry_policy = RetryPolicy(delay=2.0, backoff=3.0, max_delay=12.0)
        assert _schedule(net._resolve_retry_schedule().default, 4) == [2.0, 6.0, 12.0, 12.0]

    def test_retry_delay_is_a_view_onto_the_default_policy(self):
        net = PetriNet(retry_policy=RetryPolicy(delay=1.0, backoff=2.0, max_delay=9.0))
        assert net.retry_delay == 1.0

        net.retry_delay = 3.0
        # Only `delay` moves; the rest of the schedule is left alone.
        assert net.retry_policy == RetryPolicy(delay=3.0, backoff=2.0, max_delay=9.0)

    @pytest.mark.parametrize("bad", [-1.0, math.nan, math.inf])
    def test_mutating_retry_delay_is_validated_too(self, bad):
        """The constructor check is not the only gate — a later assignment is checked as well."""
        net = PetriNet()
        with pytest.raises(ValueError):
            net.retry_delay = bad
        assert net.retry_policy == DEFAULT_RETRY_POLICY  # unchanged

    def test_mutating_retry_policy_is_validated_too(self):
        net = PetriNet()
        with pytest.raises(TypeError, match="retry_policy must be a RetryPolicy"):
            net.retry_policy = "flat"
        assert net.retry_policy == DEFAULT_RETRY_POLICY

    def test_a_valid_mutation_still_works_end_to_end(self):
        with _retry_net() as net:
            net.retry_policy = RetryPolicy(delay=1.0, backoff=2.0)
            assert _observed_delays(net, 3) == [1.0, 2.0, 4.0]

    def test_mutating_a_transition_policy_takes_effect(self):
        with _retry_net(transition_policy=RetryPolicy(delay=1.0)) as net:
            net.transitions["t"].retry_policy = RetryPolicy(delay=0.5, backoff=4.0)
            assert _observed_delays(net, 3) == [0.5, 2.0, 8.0]
