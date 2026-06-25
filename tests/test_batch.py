import time

from cpnx.engine import PetriNet
from cpnx.places import Place
from cpnx.tokens import Token
from cpnx.transitions import InputArc, OutputArc, Transition


class TestSettlingBatch:
    def test_fires_after_settle(self):
        net = PetriNet(max_workers=2)
        net.add_place(Place("input"))
        net.add_place(Place("output"))

        received = []

        def action(tokens):
            received.extend(tokens)
            return tokens

        net.add_transition(
            Transition(
                name="batch_t",
                inputs=[InputArc("input", consume_all=True, settle_secs=0.15)],
                outputs=[OutputArc("output", count=5)],
                action=action,
            )
        )

        for i in range(5):
            net.deposit("input", Token(payload={"i": i}))
            time.sleep(0.02)

        # Should not fire immediately
        assert not net.step()

        time.sleep(0.18)
        assert net.step()
        net.run(deadline=time.monotonic() + 1.0)
        assert len(received) == 5

    def test_does_not_fire_while_tokens_still_arriving(self):
        net = PetriNet(max_workers=2)
        net.add_place(Place("input"))
        net.add_place(Place("output"))

        net.add_transition(
            Transition(
                name="batch_t",
                inputs=[InputArc("input", consume_all=True, settle_secs=0.3)],
                outputs=[OutputArc("output", count=5)],
                action=lambda tokens: tokens,
            )
        )

        # Deposit first batch
        for _ in range(3):
            net.deposit("input", Token())
        time.sleep(0.25)

        # Deposit more — resets settle clock
        for _ in range(3):
            net.deposit("input", Token())

        # Not settled yet
        assert not net.step()

    def test_consume_all_drains_entire_place(self):
        net = PetriNet(max_workers=2)
        net.add_place(Place("input"))
        net.add_place(Place("output"))

        consumed_count = [0]

        def action(tokens):
            consumed_count[0] = len(tokens)
            return tokens

        net.add_transition(
            Transition(
                name="drain",
                inputs=[InputArc("input", consume_all=True, settle_secs=0.05)],
                outputs=[OutputArc("output", count=5)],
                action=action,
            )
        )

        for _ in range(10):
            net.deposit("input", Token())

        time.sleep(0.08)
        net.run(deadline=time.monotonic() + 1.0)
        assert consumed_count[0] == 10
        assert len(net.places["input"].tokens) == 0

    def test_batch_preserves_payload(self):
        net = PetriNet(max_workers=2)
        net.add_place(Place("input"))
        net.add_place(Place("output"))

        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("input", consume_all=True, settle_secs=0.05)],
                outputs=[OutputArc("output", count=5)],
                action=lambda tokens: tokens,
            )
        )

        payloads = [{"k": i} for i in range(5)]
        for p in payloads:
            net.deposit("input", Token(payload=p))

        time.sleep(0.08)
        net.run(deadline=time.monotonic() + 1.0)
        got_payloads = [t.payload for t in net.places["output"].tokens]
        assert sorted(got_payloads, key=lambda x: x["k"]) == payloads
