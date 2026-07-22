"""Tests for OutputArc guard (CPN arc guard semantics / conditional output routing)."""

import time

from cpnx.engine import PetriNet
from cpnx.places import Place, ResourcePlace
from cpnx.tokens import Token
from cpnx.transitions import InputArc, OutputArc, Transition


class TestOutputArcGuard:
    def test_guard_routes_to_correct_place(self):
        """Token is routed to the arc whose guard returns True, not the other."""
        net = PetriNet(
            places=[Place("input"), Place("passed"), Place("rejected")],
            transitions=[
                Transition(
                    name="filter",
                    inputs=[InputArc("input")],
                    outputs=[
                        OutputArc("passed", condition=lambda tokens: tokens[0].payload.get("ok")),
                        OutputArc("rejected", condition=lambda tokens: not tokens[0].payload.get("ok")),
                    ],
                    action=lambda tokens: tokens,
                )
            ],
        )

        net.deposit("input", Token(payload={"ok": True}))
        net.run(deadline=time.monotonic() + 2.0)

        assert len(net.places["passed"].tokens) == 1
        assert len(net.places["rejected"].tokens) == 0

    def test_guard_false_discards_token(self):
        """When all arc guards return False, the data token is silently discarded."""
        net = PetriNet(
            places=[Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output", condition=lambda tokens: False)],
                    action=lambda tokens: tokens,
                )
            ],
        )

        net.deposit("input", Token())
        net.run(deadline=time.monotonic() + 2.0)

        assert len(net.places["output"].tokens) == 0
        assert len(net.places["failed"].tokens) == 0  # not an error — intentional discard

    def test_unguarded_and_guarded_arcs_coexist(self):
        """Unguarded arc always fires; guarded arc fires conditionally."""
        net = PetriNet(
            places=[Place("input"), Place("always"), Place("sometimes")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input", count=2)],
                    outputs=[
                        OutputArc("always"),  # no guard — fires for every token
                        OutputArc("sometimes", condition=lambda tokens: tokens[0].payload.get("extra")),
                    ],
                    # action returns two tokens; only first goes to "always", second to "sometimes" if guard passes
                    action=lambda tokens: tokens,
                )
            ],
        )

        net.deposit("input", Token(payload={"extra": True}))
        net.deposit("input", Token(payload={"extra": True}))
        net.run(deadline=time.monotonic() + 2.0)

        assert len(net.places["always"].tokens) == 1
        assert len(net.places["sometimes"].tokens) == 1

    def test_resource_tokens_always_returned_regardless_of_guard(self):
        """Resource tokens are returned to their source even when the data arc guard is False."""
        net = PetriNet(
            places=[Place("input"), Place("output"), ResourcePlace("slot", capacity=1)],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input"), InputArc("slot")],
                    outputs=[
                        OutputArc("output", condition=lambda tokens: False),  # always skip
                        OutputArc("slot"),  # resource return — no guard
                    ],
                    action=lambda tokens: [t for t in tokens if not t.is_resource],
                )
            ],
        )

        net.deposit("input", Token())
        net.run(deadline=time.monotonic() + 2.0)

        assert len(net.places["slot"].tokens) == 1
        assert net.places["slot"].tokens[0].is_resource
        assert len(net.places["output"].tokens) == 0

    def test_both_guards_true_both_arcs_fire(self):
        """When two guards both return True, both output arcs receive a token."""
        net = PetriNet(
            places=[Place("input"), Place("a"), Place("b")],
            transitions=[
                Transition(
                    name="broadcast",
                    inputs=[InputArc("input")],
                    outputs=[
                        OutputArc("a", condition=lambda tokens: True),
                        OutputArc("b", condition=lambda tokens: True),
                    ],
                    action=lambda tokens: tokens + [Token()],  # produce 2 data tokens
                )
            ],
        )

        net.deposit("input", Token())
        net.run(deadline=time.monotonic() + 2.0)

        assert len(net.places["a"].tokens) == 1
        assert len(net.places["b"].tokens) == 1
