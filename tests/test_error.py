import time

from petriq.engine import PetriNet
from petriq.places import Place, ResourcePlace
from petriq.tokens import Token
from petriq.transitions import InputArc, OutputArc, Transition


class TestResourceReturnOnFailure:
    def test_resource_returned_after_action_raises(self):
        net = PetriNet(max_workers=2, error_place="failed")
        net.add_place(Place("input"))
        net.add_place(Place("output"))
        net.add_place(ResourcePlace("gpu", capacity=1))

        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("input"), InputArc("gpu")],
                outputs=[OutputArc("output"), OutputArc("gpu")],
                action=lambda tokens: (_ for _ in ()).throw(ValueError("boom")),
            )
        )

        net.deposit("input", Token())
        net.step()
        net.run(deadline=time.monotonic() + 1.0)

        # Resource slot returned
        assert len(net.places["gpu"].tokens) == 1
        assert net.places["gpu"].tokens[0].is_resource

    def test_data_token_sent_to_error_place(self):
        net = PetriNet(max_workers=2, error_place="dead_letter")
        net.add_place(Place("input"))
        net.add_place(Place("output"))

        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("input")],
                outputs=[OutputArc("output")],
                action=lambda tokens: (_ for _ in ()).throw(RuntimeError("fail")),
            )
        )

        data_token = Token(payload={"job": 1})
        net.deposit("input", data_token)
        net.step()
        net.run(deadline=time.monotonic() + 1.0)

        assert len(net.places["dead_letter"].tokens) == 1
        assert net.places["dead_letter"].tokens[0] == data_token
        assert len(net.places["output"].tokens) == 0

    def test_on_error_callback_fires(self):
        net = PetriNet(max_workers=2)
        net.add_place(Place("input"))
        net.add_place(Place("output"))

        error_info = {}

        def on_err(name, exc, token):
            error_info["name"] = name
            error_info["exc"] = exc
            error_info["token"] = token

        net.on_error = on_err

        net.add_transition(
            Transition(
                name="boom_t",
                inputs=[InputArc("input")],
                outputs=[OutputArc("output")],
                action=lambda tokens: (_ for _ in ()).throw(ValueError("test error")),
            )
        )

        t = Token()
        net.deposit("input", t)
        net.step()
        net.run(deadline=time.monotonic() + 1.0)

        assert error_info.get("name") == "boom_t"
        assert isinstance(error_info.get("exc"), ValueError)
        assert error_info.get("token") == t

    def test_multiple_resources_all_returned_on_failure(self):
        net = PetriNet(max_workers=2)
        net.add_place(Place("input"))
        net.add_place(Place("output"))
        net.add_place(ResourcePlace("gpu", capacity=2))
        net.add_place(ResourcePlace("api", capacity=3))

        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("input"), InputArc("gpu"), InputArc("api")],
                outputs=[OutputArc("output"), OutputArc("gpu"), OutputArc("api")],
                action=lambda tokens: (_ for _ in ()).throw(RuntimeError("fail")),
            )
        )

        net.deposit("input", Token())
        net.step()
        net.run(deadline=time.monotonic() + 1.0)

        assert len(net.places["gpu"].tokens) == 2
        assert len(net.places["api"].tokens) == 3

    def test_successful_transitions_not_affected_by_others_failing(self):
        net = PetriNet(max_workers=4)
        net.add_place(Place("good_input"))
        net.add_place(Place("bad_input"))
        net.add_place(Place("output"))

        net.add_transition(
            Transition(
                name="good",
                inputs=[InputArc("good_input")],
                outputs=[OutputArc("output")],
                action=lambda tokens: tokens,
            )
        )
        net.add_transition(
            Transition(
                name="bad",
                inputs=[InputArc("bad_input")],
                outputs=[OutputArc("output")],
                action=lambda tokens: (_ for _ in ()).throw(RuntimeError("bad")),
            )
        )

        for _ in range(5):
            net.deposit("good_input", Token())
        for _ in range(3):
            net.deposit("bad_input", Token())

        net.run(deadline=time.monotonic() + 2.0)

        assert len(net.places["output"].tokens) == 5
        assert len(net.places["failed"].tokens) == 3

    def test_error_callback_exception_does_not_crash_engine(self):
        net = PetriNet(max_workers=2)
        net.add_place(Place("input"))
        net.add_place(Place("output"))

        def bad_callback(name, exc, token):
            raise RuntimeError("callback also exploded")

        net.on_error = bad_callback

        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("input")],
                outputs=[OutputArc("output")],
                action=lambda tokens: (_ for _ in ()).throw(ValueError("first fail")),
            )
        )

        net.deposit("input", Token())
        # Should not raise
        net.step()
        net.run(deadline=time.monotonic() + 1.0)


class TestTokenMintingFix:
    """Regression tests for the fixed token-minting bug."""

    def test_resource_arc_mismatch_routes_to_error_not_mint(self):
        """Transition with 2 resource outputs but only 1 resource input must error, not mint."""
        net = PetriNet(max_workers=2)
        net.add_place(Place("input"))
        net.add_place(Place("output"))
        net.add_place(ResourcePlace("gpu", capacity=1))
        net.add_place(ResourcePlace("gpu2", capacity=0))  # separate pool, starts empty

        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("input"), InputArc("gpu")],
                # Two resource outputs but only one resource token consumed
                outputs=[OutputArc("output"), OutputArc("gpu"), OutputArc("gpu2")],
                action=lambda tokens: [t for t in tokens if not t.is_resource],
            )
        )

        net.deposit("input", Token())
        net.step()
        net.run(deadline=time.monotonic() + 1.0)

        # Must NOT have minted a new gpu2 token
        assert len(net.places["gpu2"].tokens) == 0
        # Must have returned the original gpu token
        assert len(net.places["gpu"].tokens) == 1
        # Data token sent to error place
        assert len(net.places["failed"].tokens) == 1

    def test_data_arc_mismatch_routes_to_error_not_mint(self):
        """Transition whose action returns 0 tokens but arc.count=2 must error, not mint."""
        net = PetriNet(max_workers=2)
        net.add_place(Place("input"))
        net.add_place(Place("output"))

        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("input")],
                outputs=[OutputArc("output", count=2)],
                action=lambda tokens: [],  # returns nothing
            )
        )

        t = Token()
        net.deposit("input", t)
        net.step()
        net.run(deadline=time.monotonic() + 1.0)

        # Must NOT have minted 2 synthetic tokens
        assert len(net.places["output"].tokens) == 0
        # Data token routed to error place
        assert len(net.places["failed"].tokens) == 1


class TestCallbackDeadlockFix:
    """Regression: on_token_deposited must fire outside the engine lock."""

    def test_callback_can_call_deposit_without_deadlock(self):
        net = PetriNet(max_workers=2)
        net.add_place(Place("input"))
        net.add_place(Place("log"))
        net.add_place(Place("output"))

        # This would deadlock in the old code (callback held engine lock → re-entered deposit)
        def on_deposited(place_name, token):
            if place_name == "output":
                net.deposit("log", Token(payload={"logged": token.id}))

        net.on_token_deposited = on_deposited

        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("input")],
                outputs=[OutputArc("output")],
                action=lambda tokens: tokens,
            )
        )

        net.deposit("input", Token())
        net.run(deadline=time.monotonic() + 2.0)

        assert len(net.places["output"].tokens) == 1
        assert len(net.places["log"].tokens) == 1
