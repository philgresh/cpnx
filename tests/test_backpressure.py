"""Tests for k-bounded place back-pressure (Place.bound)."""

import time

from petriq.engine import PetriNet
from petriq.places import Place
from petriq.tokens import Token
from petriq.transitions import InputArc, OutputArc, Transition


class TestBoundedPlace:
    def test_transition_blocked_when_output_at_bound(self):
        """step() returns False when the output place is at its bound."""
        net = PetriNet(
            places=[Place("input"), Place("output", bound=1)],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=lambda tokens: tokens,
                )
            ],
        )

        # Fill output to its bound before depositing input
        net.places["output"].deposit(Token())
        net.deposit("input", Token())

        result = net.step()
        assert result is False
        assert len(net.places["input"].tokens) == 1  # not consumed

    def test_transition_unblocks_after_drain(self):
        """Transition fires once the downstream place drops below its bound."""
        net = PetriNet(
            places=[Place("input"), Place("output", bound=1)],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=lambda tokens: tokens,
                )
            ],
        )

        net.places["output"].deposit(Token())
        net.deposit("input", Token())

        # Blocked while output is full
        assert net.step() is False

        # Drain the output place
        net.places["output"].retrieve(1)

        # Now it should fire
        assert net.step() is True
        net.run(deadline=time.monotonic() + 1.0)
        assert len(net.places["output"].tokens) == 1

    def test_bound_zero_blocks_permanently(self):
        """A place with bound=0 can never accept tokens — transition is always blocked."""
        net = PetriNet(
            places=[Place("input"), Place("sink", bound=0)],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("sink")],
                    action=lambda tokens: tokens,
                )
            ],
        )

        net.deposit("input", Token())
        assert net.step() is False
        assert len(net.places["input"].tokens) == 1

    def test_unbounded_place_unaffected(self):
        """Place(bound=None) behaves identically to a Place with no bound argument."""
        net = PetriNet(
            places=[Place("input"), Place("output", bound=None)],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=lambda tokens: tokens,
                )
            ],
        )

        for _ in range(50):
            net.deposit("input", Token())

        net.run(deadline=time.monotonic() + 3.0)
        assert len(net.places["output"].tokens) == 50

    def test_back_pressure_does_not_declare_quiescence(self):
        """is_quiescent() returns False when a transition is blocked by back-pressure."""
        net = PetriNet(
            places=[Place("input"), Place("output", bound=1)],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=lambda tokens: tokens,
                )
            ],
        )

        net.places["output"].deposit(Token())
        net.deposit("input", Token())

        assert not net.is_quiescent()
