import threading
import time

from cpnx.engine import PetriNet
from cpnx.places import Place, ResourcePlace
from cpnx.tokens import Token
from cpnx.transitions import InputArc, OutputArc, Transition


class TestConcurrentExecution:
    def test_stress_100_tokens(self):
        net = PetriNet(max_workers=8)
        net.add_place(Place("input"))
        net.add_place(Place("output"))

        net.add_transition(
            Transition(
                name="process",
                inputs=[InputArc("input")],
                outputs=[OutputArc("output")],
                action=lambda tokens: (time.sleep(0.002), tokens)[1],
            )
        )

        for i in range(100):
            net.deposit("input", Token(payload={"idx": i}))

        net.run(deadline=time.monotonic() + 10.0)

        assert len(net.places["input"].tokens) == 0
        assert len(net.places["output"].tokens) == 100

    def test_resource_contention_no_overcommit(self):
        net = PetriNet(max_workers=8)
        net.add_place(Place("input"))
        net.add_place(Place("output"))
        net.add_place(ResourcePlace("gpu", capacity=2))

        in_flight = [0]
        max_in_flight = [0]
        lock = threading.Lock()

        def action(tokens):
            with lock:
                in_flight[0] += 1
                max_in_flight[0] = max(max_in_flight[0], in_flight[0])
            time.sleep(0.02)
            with lock:
                in_flight[0] -= 1
            return [t for t in tokens if not t.is_resource]

        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("input"), InputArc("gpu")],
                outputs=[OutputArc("output"), OutputArc("gpu")],
                action=action,
            )
        )

        for _ in range(20):
            net.deposit("input", Token())

        net.run(deadline=time.monotonic() + 5.0)

        assert len(net.places["output"].tokens) == 20
        assert max_in_flight[0] <= 2  # never exceeded GPU capacity

    def test_deposit_from_multiple_threads(self):
        net = PetriNet(max_workers=4)
        net.add_place(Place("input"))
        net.add_place(Place("output"))

        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("input")],
                outputs=[OutputArc("output")],
                action=lambda tokens: tokens,
            )
        )

        def depositor(n):
            for _ in range(n):
                net.deposit("input", Token())

        threads = [threading.Thread(target=depositor, args=(25,)) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        net.run(deadline=time.monotonic() + 5.0)
        assert len(net.places["output"].tokens) == 100

    def test_transition_fired_callback_called_per_transition(self):
        net = PetriNet(max_workers=4)
        net.add_place(Place("input"))
        net.add_place(Place("output"))

        fire_count = [0]
        lock = threading.Lock()

        def on_fired(name, duration):
            with lock:
                fire_count[0] += 1

        net.on_transition_fired = on_fired

        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("input")],
                outputs=[OutputArc("output")],
                action=lambda tokens: tokens,
            )
        )

        for _ in range(20):
            net.deposit("input", Token())

        net.run(deadline=time.monotonic() + 3.0)
        assert fire_count[0] == 20

    def test_two_transitions_pipeline(self):
        """A → stage1 → B → stage2 → C"""
        net = PetriNet(max_workers=4)
        net.add_place(Place("A"))
        net.add_place(Place("B"))
        net.add_place(Place("C"))

        net.add_transition(
            Transition(
                name="stage1",
                inputs=[InputArc("A")],
                outputs=[OutputArc("B")],
                action=lambda tokens: tokens,
            )
        )
        net.add_transition(
            Transition(
                name="stage2",
                inputs=[InputArc("B")],
                outputs=[OutputArc("C")],
                action=lambda tokens: tokens,
            )
        )

        for _ in range(10):
            net.deposit("A", Token())

        net.run(deadline=time.monotonic() + 3.0)

        assert len(net.places["A"].tokens) == 0
        assert len(net.places["B"].tokens) == 0
        assert len(net.places["C"].tokens) == 10
