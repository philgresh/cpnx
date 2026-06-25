import time

import pytest

from petriq.engine import PetriNet
from petriq.places import Place, ThresholdPlace
from petriq.tokens import Token
from petriq.transitions import InputArc, OutputArc, Transition


class TestThresholdPlace:
    def test_cannot_retrieve_below_threshold(self):
        tp = ThresholdPlace("t", threshold=5)
        for _ in range(4):
            tp.deposit(Token())
        assert not tp.can_retrieve(1)

    def test_can_retrieve_at_threshold(self):
        tp = ThresholdPlace("t", threshold=3)
        for _ in range(3):
            tp.deposit(Token())
        assert tp.can_retrieve(1)

    def test_retrieve_below_threshold_raises(self):
        tp = ThresholdPlace("t", threshold=3)
        tp.deposit(Token())
        with pytest.raises(ValueError):
            tp.retrieve(1)

    def test_retrieve_all_below_threshold_raises(self):
        tp = ThresholdPlace("t", threshold=3)
        tp.deposit(Token())
        with pytest.raises(ValueError):
            tp.retrieve_all()

    def test_threshold_one_is_normal_place(self):
        tp = ThresholdPlace("t", threshold=1)
        t = Token()
        tp.deposit(t)
        assert tp.can_retrieve(1)
        got = tp.retrieve(1)
        assert got == [t]

    def test_fifo_ordering_preserved(self):
        tp = ThresholdPlace("t", threshold=3)
        tokens = [Token(payload={"i": i}) for i in range(3)]
        for tok in tokens:
            tp.deposit(tok)
        got = tp.retrieve(3)
        assert [t.payload["i"] for t in got] == [0, 1, 2]


class TestThresholdInEngine:
    def test_transition_fires_only_at_threshold(self):
        net = PetriNet(max_workers=2)
        net.add_place(ThresholdPlace("input", threshold=5))
        net.add_place(Place("output"))

        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("input", count=5)],
                outputs=[OutputArc("output", count=5)],
                action=lambda tokens: tokens,
            )
        )

        for _ in range(4):
            net.deposit("input", Token())
        assert not net.step()
        assert len(net.places["output"].tokens) == 0

        net.deposit("input", Token())
        assert net.step()
        net.run(deadline=time.monotonic() + 1.0)
        assert len(net.places["output"].tokens) == 5

    def test_threshold_with_multiple_batches(self):
        net = PetriNet(max_workers=2)
        net.add_place(ThresholdPlace("input", threshold=3))
        net.add_place(Place("output"))

        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("input", count=3)],
                outputs=[OutputArc("output", count=3)],
                action=lambda tokens: tokens,
            )
        )

        for _ in range(6):
            net.deposit("input", Token())

        net.run(deadline=time.monotonic() + 2.0)
        assert len(net.places["output"].tokens) == 6
        assert len(net.places["input"].tokens) == 0


class TestThresholdCanRetrieveCountBug:
    """Regression tests for the fixed can_retrieve(count) bug."""

    def test_count_above_threshold_but_below_available_returns_false(self):
        tp = ThresholdPlace("t", threshold=3)
        for _ in range(3):
            tp.deposit(Token())
        # threshold met, but count=5 > 3 available
        assert not tp.can_retrieve(5)

    def test_count_exactly_matches_available(self):
        tp = ThresholdPlace("t", threshold=3)
        for _ in range(5):
            tp.deposit(Token())
        assert tp.can_retrieve(5)
        assert not tp.can_retrieve(6)

    def test_engine_does_not_fire_when_count_exceeds_available(self):
        net = PetriNet(max_workers=2)
        net.add_place(ThresholdPlace("input", threshold=3))
        net.add_place(Place("output"))
        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("input", count=5)],
                outputs=[OutputArc("output", count=5)],
                action=lambda tokens: tokens,
            )
        )
        # 3 tokens meet threshold but count=5 needed — must NOT fire
        for _ in range(3):
            net.deposit("input", Token())
        assert not net.step()

        # 4 tokens still not enough
        net.deposit("input", Token())
        assert not net.step()

        # 5 tokens — now count is satisfied
        net.deposit("input", Token())
        assert net.step()
        net.run(deadline=time.monotonic() + 1.0)
        assert len(net.places["output"].tokens) == 5
