import time

from petriq.engine import PetriNet
from petriq.places import Place, ResourcePlace
from petriq.tokens import Token
from petriq.transitions import InputArc, OutputArc, Transition


class TestTransitionGuard:
    def test_guard_false_blocks_firing(self):
        net = PetriNet(max_workers=2)
        net.add_place(Place("input"))
        net.add_place(Place("output"))

        allow = False

        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("input")],
                outputs=[OutputArc("output")],
                action=lambda tokens: tokens,
                guard=lambda: allow,
            )
        )

        net.deposit("input", Token())
        assert not net.step()
        assert len(net.places["output"].tokens) == 0

    def test_guard_true_allows_firing(self):
        net = PetriNet(max_workers=2)
        net.add_place(Place("input"))
        net.add_place(Place("output"))

        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("input")],
                outputs=[OutputArc("output")],
                action=lambda tokens: tokens,
                guard=lambda: True,
            )
        )

        net.deposit("input", Token())
        assert net.step()
        net.run(deadline=time.monotonic() + 1.0)
        assert len(net.places["output"].tokens) == 1

    def test_guard_toggled_mid_run(self):
        net = PetriNet(max_workers=2)
        net.add_place(Place("input"))
        net.add_place(Place("output"))

        state = {"allow": False}

        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("input")],
                outputs=[OutputArc("output")],
                action=lambda tokens: tokens,
                guard=lambda: state["allow"],
            )
        )

        net.deposit("input", Token())
        assert not net.step()

        state["allow"] = True
        assert net.step()
        net.run(deadline=time.monotonic() + 1.0)
        assert len(net.places["output"].tokens) == 1

    def test_guard_exception_treated_as_false(self):
        net = PetriNet(max_workers=2)
        net.add_place(Place("input"))
        net.add_place(Place("output"))

        def bad_guard():
            raise RuntimeError("guard blew up")

        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("input")],
                outputs=[OutputArc("output")],
                action=lambda tokens: tokens,
                guard=bad_guard,
            )
        )

        net.deposit("input", Token())
        # Guard exception should not propagate — transition simply won't fire
        assert not net.step()

    def test_guard_with_resource_check(self):
        net = PetriNet(max_workers=2)
        net.add_place(Place("input"))
        net.add_place(Place("output"))
        net.add_place(ResourcePlace("res", capacity=1))

        gate_open = False

        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("input"), InputArc("res")],
                outputs=[OutputArc("output"), OutputArc("res")],
                action=lambda tokens: [t for t in tokens if not t.is_resource],
                guard=lambda: gate_open,
            )
        )

        net.deposit("input", Token())
        assert not net.step()  # guard closed

        gate_open = True
        assert net.step()
        net.run(deadline=time.monotonic() + 1.0)
        assert len(net.places["output"].tokens) == 1
        assert len(net.places["res"].tokens) == 1  # resource returned
