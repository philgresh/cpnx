import time

import pytest

from cpnx.engine import PetriNet
from cpnx.places import Place, ResourcePlace
from cpnx.tokens import Token
from cpnx.transitions import InputArc, OutputArc, Transition


def _slow_action(sleep_secs: float):
    def action(tokens):
        time.sleep(sleep_secs)
        return tokens

    return action


class TestActionTimeoutRollback:
    def test_timeout_triggers_rollback_to_source(self):
        net = PetriNet(max_workers=2)
        net.add_place(Place("source"))
        net.add_place(Place("output"))

        net.add_transition(
            Transition(
                name="slow",
                inputs=[InputArc("source")],
                outputs=[OutputArc("output")],
                action=_slow_action(2.0),
                action_timeout_secs=0.1,
            )
        )

        net.deposit("source", Token())
        net.step()
        net.run(deadline=time.monotonic() + 0.5)

        assert len(net.places["output"].tokens) == 0
        assert len(net.places["source"].tokens) == 1

    def test_resource_tokens_returned_on_timeout(self):
        net = PetriNet(max_workers=2)
        net.add_place(Place("source"))
        net.add_place(Place("output"))
        net.add_place(ResourcePlace("gpu", capacity=1))

        net.add_transition(
            Transition(
                name="slow",
                inputs=[InputArc("source"), InputArc("gpu")],
                outputs=[OutputArc("output"), OutputArc("gpu")],
                action=_slow_action(2.0),
                action_timeout_secs=0.1,
            )
        )

        net.deposit("source", Token())
        net.step()
        net.run(deadline=time.monotonic() + 0.5)

        assert len(net.places["gpu"].tokens) == 1
        assert net.places["gpu"].tokens[0].is_resource
        assert len(net.places["source"].tokens) == 1

    def test_completes_within_timeout_no_rollback(self):
        net = PetriNet(max_workers=2)
        net.add_place(Place("source"))
        net.add_place(Place("output"))

        net.add_transition(
            Transition(
                name="fast",
                inputs=[InputArc("source")],
                outputs=[OutputArc("output")],
                action=_slow_action(0.05),
                action_timeout_secs=1.0,
            )
        )

        net.deposit("source", Token())
        net.step()
        net.run(deadline=time.monotonic() + 2.0)

        assert len(net.places["output"].tokens) == 1
        assert len(net.places["source"].tokens) == 0

    def test_none_timeout_is_unlimited(self):
        net = PetriNet(max_workers=2)
        net.add_place(Place("source"))
        net.add_place(Place("output"))

        net.add_transition(
            Transition(
                name="normal",
                inputs=[InputArc("source")],
                outputs=[OutputArc("output")],
                action=_slow_action(0.3),
                action_timeout_secs=None,
            )
        )

        net.deposit("source", Token())
        net.step()
        net.run(deadline=time.monotonic() + 2.0)

        assert len(net.places["output"].tokens) == 1
        assert len(net.places["source"].tokens) == 0

    def test_on_error_fires_with_timeout_runtime_error(self):
        error_info: dict = {}

        def on_err(name, exc, token):
            error_info["name"] = name
            error_info["exc"] = exc

        net = PetriNet(max_workers=2)
        net.on_error = on_err
        net.add_place(Place("source"))
        net.add_place(Place("output"))

        net.add_transition(
            Transition(
                name="tardy",
                inputs=[InputArc("source")],
                outputs=[OutputArc("output")],
                action=_slow_action(2.0),
                action_timeout_secs=0.1,
            )
        )

        net.deposit("source", Token())
        net.step()
        net.run(deadline=time.monotonic() + 0.5)

        assert error_info.get("name") == "tardy"
        exc = error_info.get("exc")
        assert isinstance(exc, RuntimeError)
        assert "tardy" in str(exc)
        assert "0.1" in str(exc)
