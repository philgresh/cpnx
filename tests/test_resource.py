import time

import pytest

from cpnx.engine import PetriNet
from cpnx.places import PacedResourcePlace, Place, ResourcePlace
from cpnx.tokens import Token
from cpnx.transitions import InputArc, OutputArc, Transition


class TestResourcePlace:
    def test_prefilled_with_resource_tokens(self):
        rp = ResourcePlace("r", capacity=3)
        assert len(rp.tokens) == 3
        assert all(t.is_resource for t in rp.tokens)

    def test_capacity_zero(self):
        rp = ResourcePlace("r", capacity=0)
        assert not rp.can_retrieve(1)

    def test_retrieve_and_return_cycle(self):
        rp = ResourcePlace("r", capacity=2)
        taken = rp.retrieve(2)
        assert len(rp.tokens) == 0
        for t in taken:
            rp.deposit(t)
        assert len(rp.tokens) == 2

    def test_retrieve_more_than_capacity_raises(self):
        rp = ResourcePlace("r", capacity=2)
        with pytest.raises(ValueError):
            rp.retrieve(3)

    def test_all_tokens_are_resource_flagged(self):
        rp = ResourcePlace("r", capacity=5)
        assert all(t.is_resource for t in rp.retrieve(5))

    def test_partially_drain_and_refill(self):
        rp = ResourcePlace("r", capacity=4)
        taken = rp.retrieve(2)
        assert rp.can_retrieve(2)
        assert not rp.can_retrieve(3)
        for t in taken:
            rp.deposit(t)
        assert rp.can_retrieve(4)


class TestPacedResourcePlace:
    def test_available_immediately_at_init(self):
        paced = PacedResourcePlace("p", capacity=2, pacing_secs=0.2)
        assert paced.can_retrieve(2)

    def test_cooldown_blocks_reuse(self):
        paced = PacedResourcePlace("p", capacity=1, pacing_secs=0.15)
        t = paced.retrieve(1)[0]
        paced.deposit(t)
        assert not paced.can_retrieve(1)

    def test_available_after_cooldown(self):
        paced = PacedResourcePlace("p", capacity=1, pacing_secs=0.05)
        t = paced.retrieve(1)[0]
        paced.deposit(t)
        time.sleep(0.08)
        assert paced.can_retrieve(1)

    def test_multiple_tokens_independent_cooldowns(self):
        paced = PacedResourcePlace("p", capacity=2, pacing_secs=0.1)
        t1, t2 = paced.retrieve(2)
        # Return t1 first, then t2 a bit later
        paced.deposit(t1)
        time.sleep(0.05)
        paced.deposit(t2)
        # Neither should be available yet
        assert not paced.can_retrieve(2)
        time.sleep(0.07)
        # t1 is past cooldown but t2 isn't
        assert paced.can_retrieve(1)
        assert not paced.can_retrieve(2)

    def test_retrieve_raises_when_in_cooldown(self):
        paced = PacedResourcePlace("p", capacity=1, pacing_secs=0.2)
        t = paced.retrieve(1)[0]
        paced.deposit(t)
        with pytest.raises(ValueError):
            paced.retrieve(1)


class TestPacedTransitionPipelining:
    def test_paced_resource_enforces_spacing(self):
        net = PetriNet(max_workers=2)
        net.add_place(Place("input"))
        net.add_place(Place("output"))
        net.add_place(PacedResourcePlace("resource", capacity=1, pacing_secs=0.1))

        def action(tokens):
            data = [t for t in tokens if not t.is_resource]
            return data

        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("input"), InputArc("resource")],
                outputs=[OutputArc("output"), OutputArc("resource")],
                action=action,
            )
        )

        for _ in range(3):
            net.deposit("input", Token())

        start = time.monotonic()
        net.run(deadline=start + 2.0)
        elapsed = time.monotonic() - start

        assert len(net.places["output"].tokens) == 3
        # 3 jobs through 1 paced slot at 0.1s cooldown = at least 0.2s
        assert elapsed >= 0.18
