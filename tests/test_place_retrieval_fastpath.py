"""Regression tests for the early-exit can_retrieve/peek fast path and id-safe removal.

Covers:
- Place.can_retrieve / Place.peek short-circuit correctly with non-contiguous
  availability (an unavailable token sandwiched between available ones).
- ThresholdPlace.can_retrieve still enforces the batch threshold after the
  short-circuit rewrite (`_has_available(t_limit, max(threshold, count))`).
- Removal by object identity instead of Token.id, guarding against the
  PacedResourcePlace cooldown-copy id collision.
"""

from cpnx.places import PacedResourcePlace, Place, ThresholdPlace
from cpnx.tokens import Token


class TestNonContiguousAvailability:
    """A place holding [available, not-yet-available, available] tokens."""

    def _build(self):
        # model time 0: t1 available now, t2 available only after t=100,
        # t3 available now. Insertion order is t1, t2, t3.
        t1 = Token(available_at=0.0)
        t2 = Token(available_at=100.0)
        t3 = Token(available_at=0.0)
        p = Place("p")
        p.deposit(t1, model_time=0.0)
        p.deposit(t2, model_time=0.0)
        p.deposit(t3, model_time=0.0)
        return p, t1, t2, t3

    def test_can_retrieve_counts_only_available_tokens(self):
        p, t1, t2, t3 = self._build()
        # Only t1 and t3 are available at model_time=0 -> 2 available.
        assert p.can_retrieve(2, model_time=0.0)
        assert not p.can_retrieve(3, model_time=0.0)

    def test_peek_skips_unavailable_but_keeps_scanning(self):
        p, t1, t2, t3 = self._build()
        # peek(2) must skip t2 (not yet available) and still find t3,
        # returning [t1, t3] in insertion order rather than stopping at t2.
        peeked = p.peek(2, model_time=0.0)
        assert peeked == [t1, t3]

    def test_peek_one_returns_first_available_only(self):
        p, t1, t2, t3 = self._build()
        peeked = p.peek(1, model_time=0.0)
        assert peeked == [t1]

    def test_can_retrieve_after_time_advances(self):
        p, t1, t2, t3 = self._build()
        # Once model time passes 100, all three are available.
        assert p.can_retrieve(3, model_time=100.0)

    def test_retrieve_skips_unavailable_and_preserves_order(self):
        p, t1, t2, t3 = self._build()
        got = p.retrieve(2, model_time=0.0)
        assert got == [t1, t3]
        remaining = p.tokens
        assert remaining == (t2,)


class TestPacedResourcePlaceNonContiguous:
    def test_peek_and_can_retrieve_skip_cooling_tokens(self):
        rp = PacedResourcePlace("r", capacity=0, pacing_secs=10.0)
        t1 = Token(color="resource", available_at=0.0)
        t2 = Token(color="resource", available_at=50.0)
        t3 = Token(color="resource", available_at=0.0)
        for t in (t1, t2, t3):
            rp._store.append(t)
        assert rp.can_retrieve(2, model_time=0.0)
        assert not rp.can_retrieve(3, model_time=0.0)
        assert rp.peek(2, model_time=0.0) == [t1, t3]


class TestThresholdPlaceGating:
    def test_blocks_below_threshold_even_if_count_satisfied(self):
        tp = ThresholdPlace("batch", threshold=6)
        for _ in range(5):
            tp.deposit(Token())
        # count=1 would normally be satisfiable, but threshold of 6 is not met.
        assert not tp.can_retrieve(1)
        assert not tp.can_retrieve(5)

    def test_admits_once_threshold_met(self):
        tp = ThresholdPlace("batch", threshold=6)
        for _ in range(6):
            tp.deposit(Token())
        assert tp.can_retrieve(1)
        assert tp.can_retrieve(6)
        assert not tp.can_retrieve(7)

    def test_count_may_exceed_threshold(self):
        tp = ThresholdPlace("batch", threshold=3)
        for _ in range(10):
            tp.deposit(Token())
        # Threshold (3) is met, and count (8) exceeds threshold but is still
        # satisfiable given 10 tokens are present.
        assert tp.can_retrieve(8)
        assert not tp.can_retrieve(11)

    def test_threshold_gate_with_partial_availability(self):
        tp = ThresholdPlace("batch", threshold=4)
        for _ in range(3):
            tp.deposit(Token(available_at=0.0), model_time=0.0)
        # A 4th token exists but isn't available yet.
        tp.deposit(Token(available_at=100.0), model_time=0.0)
        assert not tp.can_retrieve(1, model_time=0.0)
        assert tp.can_retrieve(1, model_time=100.0)


class TestIdSafeRemoval:
    """Guards against the PacedResourcePlace cooldown-copy id-collision bug.

    `PacedResourcePlace.deposit` creates a cooldown copy via
    `token.evolve(available_at=..., id=token.id)`, so a place can end up
    holding two distinct Token *objects* that share the same `.id` but have
    different `available_at` values. Removal must be by Python object
    identity, not by `.id`, or retrieving one erroneously deletes both.
    """

    def test_retrieving_available_token_leaves_colliding_future_token(self):
        p = Place("p")
        available = Token(available_at=0.0)
        future = available.evolve(available_at=100.0, id=available.id)
        assert future.id == available.id
        assert future is not available

        p._store.append(available)
        p._store.append(future)

        got = p.retrieve(1, model_time=0.0)
        assert got == [available]
        # The colliding, still-cooling token must remain untouched.
        remaining = p.tokens
        assert remaining == (future,)

    def test_retrieve_all_leaves_colliding_future_token(self):
        p = Place("p")
        available = Token(available_at=0.0)
        future = available.evolve(available_at=100.0, id=available.id)

        p._store.append(available)
        p._store.append(future)

        got = p.retrieve_all(model_time=0.0)
        assert got == [available]
        assert p.tokens == (future,)

    def test_retrieve_specific_removes_only_the_named_instance(self):
        p = Place("p")
        available = Token(available_at=0.0)
        future = available.evolve(available_at=100.0, id=available.id)

        p._store.append(available)
        p._store.append(future)

        got = p.retrieve_specific([available], model_time=0.0)
        assert got == [available]
        assert p.tokens == (future,)

    def test_paced_resource_place_end_to_end_collision(self):
        rp = PacedResourcePlace("r", capacity=1, pacing_secs=100.0)
        # Take the only token, then return it -- deposit() creates a cooldown
        # copy that shares the original token's id.
        (taken,) = rp.retrieve(1, model_time=0.0)
        rp.deposit(taken, model_time=0.0)
        cooling = rp.tokens[0]
        assert cooling.id == taken.id
        assert cooling is not taken

        # A second, independently-created token with the SAME id but already
        # available is appended directly to simulate the collision scenario
        # precisely as described: two distinct instances, same id, different
        # available_at.
        rp._store.append(taken.evolve(available_at=0.0, id=taken.id))

        assert rp.can_retrieve(1, model_time=0.0)
        got = rp.retrieve(1, model_time=0.0)
        assert len(got) == 1
        # The still-cooling copy must remain.
        assert len(rp.tokens) == 1
        assert rp.tokens[0].available_at == 100.0
