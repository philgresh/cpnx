"""Tests for the non-consuming test/read arc (`InputArc(test=True)`)."""

import time

import pytest

from cpnx.engine import PetriNet
from cpnx.places import Place
from cpnx.tokens import Token
from cpnx.transitions import InputArc, OutputArc, Transition


class TestTestArcConstruction:
    def test_test_flag_defaults_false(self):
        assert InputArc("p").test is False

    def test_test_flag_settable(self):
        assert InputArc("p", test=True).test is True

    def test_test_with_consume_all_raises(self):
        with pytest.raises(TypeError, match="test=True cannot be combined with consume_all"):
            InputArc("p", test=True, consume_all=True)

    def test_reassigning_into_conflict_raises(self):
        arc = InputArc("p", test=True)
        with pytest.raises(TypeError):
            arc.consume_all = True


class TestTestArcGating:
    def _gated_net(self):
        net = PetriNet(max_workers=1)
        net.add_place(Place("gate"))
        net.add_place(Place("work"))
        net.add_place(Place("done"))
        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("work"), InputArc("gate", test=True)],
                outputs=[OutputArc("done")],
                action=lambda toks: [toks[0]],
            )
        )
        return net

    def test_disabled_without_gate_token(self):
        net = self._gated_net()
        net.deposit("work", Token())
        net.validate()
        assert net.is_dead()  # gate empty → test arc fails → not enabled

    def test_enabled_with_gate_token(self):
        net = self._gated_net()
        net.deposit("work", Token())
        net.deposit("gate", Token())
        net.validate()
        assert not net.is_dead()

    def test_firing_does_not_consume_gate_token(self):
        net = self._gated_net()
        net.deposit("gate", Token())
        for _ in range(5):
            net.deposit("work", Token())
        net.run(deadline=time.monotonic() + 2.0)
        assert len(net.places["done"]) == 5
        assert len(net.places["gate"]) == 1  # the single gate token was never consumed

    def test_gate_token_not_passed_to_guard(self):
        # The guard must see only the consuming ("work") arc's tokens, never the gate token.
        seen: list[int] = []

        def guard(toks):
            seen.append(len(toks))
            return True

        net = PetriNet(max_workers=1)
        net.add_place(Place("gate"))
        net.add_place(Place("work"))
        net.add_place(Place("done"))
        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("work"), InputArc("gate", test=True)],
                outputs=[OutputArc("done")],
                action=lambda toks: [toks[0]],
                guard=guard,
            )
        )
        net.deposit("gate", Token())
        net.deposit("work", Token())
        net.run(deadline=time.monotonic() + 2.0)
        assert seen and all(n == 1 for n in seen)  # only the work token, not the gate token

    def test_count_gates_on_presence(self):
        # count=2 requires at least two tokens present, still consuming none.
        net = PetriNet(max_workers=1)
        net.add_place(Place("gate"))
        net.add_place(Place("work"))
        net.add_place(Place("done"))
        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("work"), InputArc("gate", count=2, test=True)],
                outputs=[OutputArc("done")],
                action=lambda toks: [toks[0]],
            )
        )
        net.deposit("work", Token())
        net.deposit("gate", Token())
        net.validate()
        assert net.is_dead()  # only one gate token, need two
        net.deposit("gate", Token())
        assert not net.is_dead()


class TestTestArcConcurrency:
    def test_many_transitions_share_one_gate_token(self):
        # Several transitions test the same place concurrently; the single gate token must
        # remain (non-consuming) and every transition must fire.
        net = PetriNet(max_workers=4)
        net.add_place(Place("gate"))
        net.add_place(Place("done"))
        for i in range(6):
            src = f"work{i}"
            net.add_place(Place(src))
            net.add_transition(
                Transition(
                    name=f"t{i}",
                    inputs=[InputArc(src), InputArc("gate", test=True)],
                    outputs=[OutputArc("done")],
                    action=lambda toks: [toks[0]],
                )
            )
            net.deposit(src, Token())
        net.deposit("gate", Token())

        net.run(deadline=time.monotonic() + 3.0)

        assert len(net.places["done"]) == 6
        assert len(net.places["gate"]) == 1  # never consumed despite concurrent readers
