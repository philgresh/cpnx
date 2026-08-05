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


def _borrow_net(*, auto_return=True, declare_return=False, consume_all=False, paced=False):
    """A transition that borrows one permit from `pool` to move `src` -> `sink`."""
    net = PetriNet(max_workers=2)
    net.add_place(Place("src"))
    net.add_place(Place("sink"))
    pool = PacedResourcePlace("pool", capacity=1, pacing_secs=0.0) if paced else ResourcePlace("pool", capacity=1)
    net.add_place(pool)
    outputs = [OutputArc("sink")]
    if declare_return:
        outputs.append(OutputArc("pool"))
    net.add_transition(
        Transition(
            name="work",
            inputs=[InputArc("src"), InputArc("pool", consume_all=consume_all)],
            outputs=outputs,
            action=lambda tokens: [t for t in tokens if not t.is_resource],
            auto_return_resources=auto_return,
        )
    )
    return net


def _pool_output_arcs(net):
    return [a for a in net.transitions["work"].outputs if a.place == "pool"]


class TestResourceReturnSynthesis:
    def test_validate_synthesizes_return_arc(self):
        net = _borrow_net()
        net.validate()
        arcs = _pool_output_arcs(net)
        assert len(arcs) == 1
        assert arcs[0].synthesized is True
        assert arcs[0].count == 1

    def test_declared_self_loop_is_not_duplicated(self):
        net = _borrow_net(declare_return=True)
        net.validate()
        arcs = _pool_output_arcs(net)
        assert len(arcs) == 1
        assert arcs[0].synthesized is False

    def test_opt_out_skips_synthesis(self):
        net = _borrow_net(auto_return=False)
        net.validate()
        assert _pool_output_arcs(net) == []

    def test_consume_all_skips_synthesis(self):
        net = _borrow_net(consume_all=True)
        net.validate()
        assert _pool_output_arcs(net) == []

    def test_paced_resource_place_synthesizes(self):
        net = _borrow_net(paced=True)
        net.validate()
        arcs = _pool_output_arcs(net)
        assert len(arcs) == 1 and arcs[0].synthesized is True

    def test_synthesis_is_idempotent(self):
        net = _borrow_net()
        net.validate()
        net.validate()
        assert len(_pool_output_arcs(net)) == 1

    def test_synthesis_rearms_after_add_transition(self):
        net = _borrow_net()
        net.validate()
        net.add_place(Place("sink2"))
        net.add_transition(
            Transition(
                name="work2",
                inputs=[InputArc("sink"), InputArc("pool")],
                outputs=[OutputArc("sink2")],
                action=lambda tokens: [t for t in tokens if not t.is_resource],
            )
        )
        net.validate()
        assert [a.place for a in net.transitions["work2"].outputs if a.place == "pool"] == ["pool"]

    def test_behaviour_preserved_permit_returns_via_synthesized_arc(self):
        net = _borrow_net()
        net.deposit("src", Token(payload={"job": 1}))
        net.run(deadline=time.monotonic() + 2.0)
        assert len(net.places["sink"].tokens) == 1
        # Permit returned to the pool through the (validated) synthesized deposit path.
        assert len(net.places["pool"].tokens) == 1

    def test_opt_out_still_returns_permit_via_implicit_path(self):
        net = _borrow_net(auto_return=False)
        net.deposit("src", Token(payload={"job": 1}))
        net.run(deadline=time.monotonic() + 2.0)
        assert len(net.places["sink"].tokens) == 1
        assert len(net.places["pool"].tokens) == 1  # implicit leftover-return still works

    def test_drive_to_quiescence_triggers_synthesis(self):
        net = _borrow_net()
        net.deposit("src", Token(payload={"job": 1}))
        net.drive_to_quiescence()
        assert _pool_output_arcs(net)[0].synthesized is True
        assert len(net.places["pool"].tokens) == 1

    def test_to_dot_draws_solid_return_not_implicit(self):
        net = _borrow_net()
        dot = net.to_dot()
        # A real arc, not the dashed implicit-return edge.
        assert '"work" -> "pool"' in dot
        assert "return (implicit)" not in dot

    def test_to_dot_keeps_implicit_edge_when_opted_out(self):
        net = _borrow_net(auto_return=False)
        dot = net.to_dot()
        assert "return (implicit)" in dot

    def test_trace_impact_sees_borrowed_pool(self):
        net = _borrow_net()
        assert "pool" in net.trace_impact("work").places
