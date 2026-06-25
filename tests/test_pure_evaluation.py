import pytest

from petriq.engine import PetriNet
from petriq.places import Place
from petriq.sandbox import SandboxEvaluator, verify_callable_purity
from petriq.tokens import Token
from petriq.transitions import InputArc, OutputArc, Transition


def test_sandbox_evaluator_whitelist():
    # Math operations and whitelisted builtins should work
    res = SandboxEvaluator.evaluate("sum([1, 2, 3]) + len(tokens)", {"tokens": [1, 2]})
    assert res == 8


def test_sandbox_evaluator_blacklist():
    # Attempting to call forbidden builtins raises PermissionError
    with pytest.raises(PermissionError, match="Forbidden call to 'print'"):
        SandboxEvaluator.evaluate("print(tokens)", {"tokens": []})

    with pytest.raises(PermissionError, match="Forbidden call to 'open'"):
        SandboxEvaluator.evaluate("open('secrets.txt')", {"tokens": []})

    with pytest.raises(PermissionError, match="Imports are forbidden"):
        SandboxEvaluator.evaluate("import os", {"tokens": []})

    # Non-whitelisted attribute calls are blocked
    with pytest.raises(PermissionError, match="Forbidden call to method 'system'"):
        SandboxEvaluator.evaluate("os.system('ls')", {"tokens": []})

    # Complex/nested calls are blocked
    with pytest.raises(PermissionError, match="Forbidden complex call"):
        SandboxEvaluator.evaluate("func()()", {"tokens": []})


def test_sandbox_evaluator_private_attributes():
    # Accessing private/dunder attributes starting with _ is forbidden
    with pytest.raises(PermissionError, match="Access to private/dunder attribute '__class__' is forbidden"):
        SandboxEvaluator.evaluate("tokens.__class__", {"tokens": []})


def test_callable_purity_verification_valid():
    # A valid pure function should pass purity check
    def valid_action(tokens):
        return [t.evolve(payload_updates={"processed": True}) for t in tokens]

    verify_callable_purity(valid_action)


def test_callable_purity_verification_invalid():
    # Impure functions (I/O, globals, etc.) should raise PermissionError
    def print_action(tokens):
        print("Hello")
        return tokens

    with pytest.raises(PermissionError, match="Forbidden function call 'print'"):
        verify_callable_purity(print_action)

    def write_file_action(tokens):
        with open("log.txt", "w") as f:
            f.write("firing")
        return tokens

    with pytest.raises(PermissionError, match="Forbidden function call 'open'"):
        verify_callable_purity(write_file_action)

    def global_action(tokens):
        global some_global
        some_global = 123
        return tokens

    with pytest.raises(PermissionError, match="Global/nonlocal mutations are forbidden"):
        verify_callable_purity(global_action)


def test_transition_purity_checks():
    # Transition instantiation should trigger purity check and raise on impure guards
    def impure_guard(tokens):
        print("impure")
        return True

    with pytest.raises(PermissionError):
        Transition(
            name="t",
            inputs=[InputArc("in")],
            outputs=[],
            action=lambda tokens: tokens,
            guard=impure_guard,
        )


def test_verify_callable_purity_raises_on_uninspectable():
    import functools

    def my_fn(tokens):
        return tokens

    partial_fn = functools.partial(my_fn, val=42)

    with pytest.raises(PermissionError, match="source unavailable"):
        verify_callable_purity(partial_fn)


def test_arc_expression_sandbox():
    # Test that engine evaluates string expressions in InputArc, OutputArc, and guards
    net = PetriNet()
    p_in = Place("in")
    p_out = Place("out")
    net.add_place(p_in)
    net.add_place(p_out)

    # Let's write transition with string-based guard and input/output arc expressions
    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("in", expression="tokens")],  # Identity input expression
            outputs=[OutputArc("out", expression="len(tokens) > 0")],  # Only deposit if outputs present
            action=lambda tokens: tokens,
            guard="tokens[0].payload.get('val') == 42",  # Enabled if token val is 42
        )
    )

    # Deposit token without val=42 (so guard evaluates to False)
    net.deposit("in", Token())
    assert net.step() is False

    # Retrieve the non-matching token to empty the place
    net.places["in"].retrieve(1)

    # Deposit token with val=42 (so guard evaluates to True)
    net.deposit("in", Token(payload={"val": 42}))
    assert net.step() is True


def test_callable_expression_timeout():
    import time

    def slow_expression(tokens):
        time.sleep(0.5)
        return tokens

    with PetriNet(timeout_secs=0.1) as net:
        p_in = Place("in")
        net.add_place(p_in)
        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("in", expression=slow_expression)],
                outputs=[],
                action=lambda tokens: tokens,
            )
        )
        net.deposit("in", Token())
        with pytest.raises(RuntimeError, match="exceeded 0.1s"):
            net.step()
