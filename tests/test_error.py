import time

from petriq.engine import PetriNet
from petriq.places import Place, ResourcePlace
from petriq.tokens import Token
from petriq.transitions import InputArc, OutputArc, Transition


def test_resource_return_on_failure():
    # Use custom error place name
    net = PetriNet(max_workers=2, error_place="custom_failed")
    net.add_place(Place("input"))
    net.add_place(ResourcePlace("gpu", capacity=1))
    net.add_place(Place("output"))

    def failing_action(tokens):
        raise ValueError("Simulated action failure")

    net.add_transition(
        Transition(
            name="fail_t",
            inputs=[InputArc("input"), InputArc("gpu")],
            outputs=[OutputArc("output"), OutputArc("gpu")],
            action=failing_action,
        )
    )

    error_called = False
    error_token = None

    def on_error_cb(trans_name, exc, token):
        nonlocal error_called, error_token
        error_called = True
        error_token = token

    net.on_error = on_error_cb

    # Deposit data token
    t_data = Token(payload={"job": 123})
    net.deposit("input", t_data)

    fired = net.step()
    assert fired

    net.run(deadline=time.monotonic() + 1.0)

    # 1. Resource token is returned to "gpu"
    assert len(net.places["gpu"].tokens) == 1
    assert net.places["gpu"].tokens[0].is_resource

    # 2. Data token is routed to "custom_failed"
    assert len(net.places["custom_failed"].tokens) == 1
    assert net.places["custom_failed"].tokens[0] == t_data

    # 3. on_error callback is fired
    assert error_called
    assert error_token == t_data
