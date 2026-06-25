import time

from petriq.engine import PetriNet
from petriq.places import Place
from petriq.tokens import Token
from petriq.transitions import InputArc, OutputArc, Transition


class TestTransitionPriority:
    def test_lower_priority_value_fires_first(self):
        net = PetriNet(max_workers=1)
        net.add_place(Place("input"))
        net.add_place(Place("out_high"))
        net.add_place(Place("out_low"))

        net.add_transition(
            Transition(
                name="low",
                inputs=[InputArc("input")],
                outputs=[OutputArc("out_low")],
                action=lambda tokens: tokens,
                priority=20,
            )
        )
        net.add_transition(
            Transition(
                name="high",
                inputs=[InputArc("input")],
                outputs=[OutputArc("out_high")],
                action=lambda tokens: tokens,
                priority=5,
            )
        )

        net.deposit("input", Token())
        net.step()
        net.run(deadline=time.monotonic() + 1.0)

        assert len(net.places["out_high"].tokens) == 1
        assert len(net.places["out_low"].tokens) == 0

    def test_default_priority_is_equal(self):
        net = PetriNet(max_workers=2)
        net.add_place(Place("input"))
        net.add_place(Place("output"))

        net.add_transition(
            Transition(
                name="t1",
                inputs=[InputArc("input")],
                outputs=[OutputArc("output")],
                action=lambda tokens: tokens,
            )
        )
        net.add_transition(
            Transition(
                name="t2",
                inputs=[InputArc("input")],
                outputs=[OutputArc("output")],
                action=lambda tokens: tokens,
            )
        )

        for _ in range(10):
            net.deposit("input", Token())

        net.run(deadline=time.monotonic() + 2.0)
        # Both transitions should collectively process all tokens
        assert len(net.places["output"].tokens) == 10

    def test_priority_respected_under_load(self):
        """With a single worker, priority should determine order of all firings."""
        net = PetriNet(max_workers=1)
        net.add_place(Place("urgent"))
        net.add_place(Place("normal"))
        net.add_place(Place("output"))

        order = []

        def make_action(label):
            def action(tokens):
                order.append(label)
                return tokens

            return action

        net.add_transition(
            Transition(
                name="normal_t",
                inputs=[InputArc("normal")],
                outputs=[OutputArc("output")],
                action=make_action("normal"),
                priority=10,
            )
        )
        net.add_transition(
            Transition(
                name="urgent_t",
                inputs=[InputArc("urgent")],
                outputs=[OutputArc("output")],
                action=make_action("urgent"),
                priority=1,
            )
        )

        # Deposit both before running so both are available each step
        for _ in range(3):
            net.deposit("normal", Token())
            net.deposit("urgent", Token())

        net.run(deadline=time.monotonic() + 3.0)

        # With single worker, urgent transitions should all fire before normal ones
        # (since urgent priority=1 wins every step over priority=10)
        assert order.count("urgent") == 3
        assert order.count("normal") == 3
        # All urgent should appear before all normal
        urgent_indices = [i for i, v in enumerate(order) if v == "urgent"]
        normal_indices = [i for i, v in enumerate(order) if v == "normal"]
        assert max(urgent_indices) < min(normal_indices)
