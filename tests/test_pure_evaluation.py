import pytest

from cpnx.engine import PetriNet
from cpnx.places import Place
from cpnx.sandbox import SandboxEvaluator, verify_callable_purity
from cpnx.tokens import Token
from cpnx.transitions import InputArc, OutputArc, Transition


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
        # Busy-wait: time.sleep is on the purity denylist, so use a spin loop instead.
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            pass
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
        # W1 fix: a timed-out expression disables the transition rather than crashing
        # step()/run(). The token stays in place; step() returns False.
        result = net.step()
        assert result is False
        assert len(net.places["in"].tokens) == 1


# --- Audit remediation tests ---


# Module-level helper: set literal default (inspectable by verify_callable_purity)
def _guard_with_set_literal_default(tokens, s={1, 2}):  # noqa: B006
    return True


class TestSandboxIterationBlocking:
    """Task 4: SandboxEvaluator must block all unbounded iteration forms."""

    def test_while_loop_blocked(self):
        assert SandboxEvaluator.evaluate("1 if True else 0", {}) == 1  # baseline passes
        with pytest.raises(PermissionError, match="Unbounded iteration"):
            SandboxEvaluator.evaluate("while True: pass", {})

    def test_for_loop_blocked(self):
        with pytest.raises(PermissionError, match="Unbounded iteration"):
            SandboxEvaluator.evaluate("for x in []: pass", {})

    def test_list_comprehension_blocked(self):
        with pytest.raises(PermissionError, match="Unbounded iteration"):
            SandboxEvaluator.evaluate("[x for x in range(10)]", {})

    def test_dict_comprehension_blocked(self):
        with pytest.raises(PermissionError, match="Unbounded iteration"):
            SandboxEvaluator.evaluate("{k: k for k in []}", {})

    def test_set_comprehension_blocked(self):
        with pytest.raises(PermissionError, match="Unbounded iteration"):
            SandboxEvaluator.evaluate("{x for x in []}", {})

    def test_generator_expression_blocked(self):
        with pytest.raises(PermissionError, match="Unbounded iteration"):
            SandboxEvaluator.evaluate("(x for x in [])", {})


class TestMutableDefaultArgDetection:
    """Task 3: verify_callable_purity must reject mutable default arguments."""

    def test_list_default_rejected(self):
        def guard_with_list_default(tokens, memory=[]):  # noqa: B006
            return True

        with pytest.raises(PermissionError, match="Mutable default argument"):
            verify_callable_purity(guard_with_list_default)

    def test_dict_default_rejected(self):
        def guard_with_dict_default(tokens, cache={}):  # noqa: B006
            return bool(tokens)

        with pytest.raises(PermissionError, match="Mutable default argument"):
            verify_callable_purity(guard_with_dict_default)

    def test_set_default_rejected(self):
        # ast.Set literal default — must be caught
        with pytest.raises(PermissionError, match="Mutable default argument"):
            verify_callable_purity(_guard_with_set_literal_default)

    def test_immutable_default_allowed(self):
        def guard_with_int_default(tokens, threshold=0):
            return len(tokens) > threshold

        verify_callable_purity(guard_with_int_default)  # must not raise


class TestAtomicRollback:
    """Task 2: failed transitions must return all tokens to source, not DLQ."""

    def test_data_token_returned_to_source_not_error_place(self):
        import time as _time

        net = PetriNet(max_workers=1, error_place="dlq")
        net.add_place(Place("source"))
        net.add_place(Place("output"))

        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("source")],
                outputs=[OutputArc("output")],
                action=lambda tokens: (_ for _ in ()).throw(RuntimeError("fail")),
            )
        )

        net.deposit("source", Token())
        net.step()
        net.run(deadline=_time.monotonic() + 0.5)

        assert len(net.places["dlq"].tokens) == 0, "DLQ must be empty — rollback is atomic"
        assert len(net.places["source"].tokens) == 1, "Token must be returned to source place"
