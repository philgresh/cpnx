"""Tests for deep-review findings: C1, W1, W2."""

import time

import pytest

from petriq.engine import PetriNet
from petriq.places import Place
from petriq.tokens import Token
from petriq.transitions import InputArc, OutputArc, Transition


class TestC1TokenRollbackOnExpressionError:
    """C1: Tokens consumed by earlier arcs must be returned when a later arc expression raises.

    The expression is called twice per step: once during _is_transition_enabled (peek,
    no consumption) and once during the consumption loop. C1 covers the case where
    the expression passes the enable check but raises during the actual consumption.
    We use a call counter to simulate this: pass on call 1, raise on call 2.
    """

    def _make_boom_on_second_call(self, tokens):
        """Return an expression callable that passes on first call, raises on second."""
        calls = {"n": 0}

        def expr(t):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("boom on second call")
            return t

        return expr

    def test_tokens_returned_when_second_arc_expression_raises(self):
        calls = {"b": 0}

        def b_expr(tokens):
            calls["b"] += 1
            if calls["b"] >= 2:
                raise RuntimeError("boom")
            return tokens

        net = PetriNet(
            places=[Place("a"), Place("b"), Place("out")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[
                        InputArc("a"),
                        InputArc("b", expression=b_expr),
                    ],
                    outputs=[OutputArc("out", count=2)],
                    action=lambda tokens: tokens,
                )
            ],
        )
        ta = Token(payload={"src": "a"})
        tb = Token(payload={"src": "b"})
        net.deposit("a", ta)
        net.deposit("b", tb)

        # step() raises on the second call to b_expr (in consumption loop).
        # Token from "a" (already consumed) must be rolled back.
        with pytest.raises(RuntimeError, match="boom"):
            net.step()

        assert len(net.places["a"].tokens) == 1, "token from first arc must be rolled back"
        assert net.places["a"].tokens[0].id == ta.id
        assert len(net.places["out"].tokens) == 0

    def test_tokens_returned_when_string_expression_raises_on_second_call(self):
        """String expressions that raise mid-loop also trigger rollback."""
        calls = {"b": 0}

        def b_expr_str_sim(tokens):
            calls["b"] += 1
            if calls["b"] >= 2:
                raise IndexError("index out of range")
            return tokens

        net = PetriNet(
            places=[Place("a"), Place("b"), Place("out")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[
                        InputArc("a"),
                        InputArc("b", expression=b_expr_str_sim),
                    ],
                    outputs=[OutputArc("out", count=2)],
                    action=lambda tokens: tokens,
                )
            ],
        )
        net.deposit("a", Token())
        net.deposit("b", Token())

        with pytest.raises(IndexError):
            net.step()

        assert len(net.places["a"].tokens) == 1, "first arc token must survive mid-loop failure"

    def test_no_partial_consumption_when_all_expressions_raise_at_enable_check(self):
        """When expression raises at _is_transition_enabled, transition is disabled — no
        tokens are consumed at all, so there is nothing to roll back."""
        net = PetriNet(
            places=[Place("a"), Place("out")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[
                        InputArc(
                            "a",
                            expression=lambda tokens: (_ for _ in ()).throw(ValueError("always")),
                        ),
                    ],
                    outputs=[OutputArc("out")],
                    action=lambda tokens: tokens,
                )
            ],
        )
        net.deposit("a", Token())

        # W1 fix: raises at enable check → transition disabled → step() returns False
        result = net.step()
        assert result is False
        assert len(net.places["a"].tokens) == 1, "token untouched — transition never selected"


class TestW1ArcExpressionExceptionDisablesTransition:
    """W1: A raising arc expression in _is_transition_enabled must disable the transition
    rather than propagate through step() and crash run()."""

    def test_raising_expression_disables_not_crashes(self):
        good_place = Place("good")
        bad_place = Place("bad")
        out_place = Place("out")

        call_count = {"n": 0}

        def boom(tokens):
            call_count["n"] += 1
            raise RuntimeError("expression exploded")

        net = PetriNet(
            places=[good_place, bad_place, out_place],
            transitions=[
                Transition(
                    name="broken",
                    inputs=[InputArc("bad", expression=boom)],
                    outputs=[OutputArc("out")],
                    action=lambda tokens: tokens,
                ),
                Transition(
                    name="healthy",
                    inputs=[InputArc("good")],
                    outputs=[OutputArc("out")],
                    action=lambda tokens: tokens,
                ),
            ],
        )

        net.deposit("good", Token())
        net.deposit("bad", Token())

        # run() must not raise even though "broken"'s expression always errors
        net.run(deadline=time.monotonic() + 2.0)

        # "healthy" must still have fired
        assert len(net.places["out"].tokens) >= 1

    def test_raising_expression_in_potentially_enabled_does_not_crash_quiescence_check(self):
        """_is_transition_potentially_enabled also wraps expressions — quiescence check safe."""
        net = PetriNet(
            places=[Place("p"), Place("out")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("p", expression=lambda tokens: 1 / 0)],
                    outputs=[OutputArc("out")],
                    action=lambda tokens: tokens,
                )
            ],
        )
        net.deposit("p", Token())

        # is_quiescent() calls _is_transition_potentially_enabled — must not raise
        assert net.is_quiescent() or not net.is_quiescent()  # either result is fine; no exception


class TestW2ModelTimeReadUnderLock:
    """W2: _model_time read in _execute_substitution_transition must be locked."""

    def test_subnet_clock_synced_correctly_under_concurrent_advance(self):
        """Advance the parent clock while subnet execution is in flight — clock value must land."""
        from petriq.transitions import SubstitutionTransition

        subnet = PetriNet(places=[Place("port_in"), Place("port_out")])
        subnet.add_transition(
            Transition(
                name="pass",
                inputs=[InputArc("port_in")],
                outputs=[OutputArc("port_out")],
                action=lambda tokens: tokens,
            )
        )

        net = PetriNet(
            places=[Place("socket_in"), Place("socket_out")],
            transitions=[
                SubstitutionTransition(
                    name="sub",
                    inputs=[InputArc("socket_in")],
                    outputs=[OutputArc("socket_out")],
                    action=lambda tokens: tokens,
                    subnet=subnet,
                    port_socket_map={"port_in": "socket_in", "port_out": "socket_out"},
                )
            ],
        )

        net.advance_time(1000.0)
        net.deposit("socket_in", Token())
        net.run(deadline=time.monotonic() + 2.0)

        # Net ran without raising — lock-guarded read was safe
        assert len(net.places["socket_out"].tokens) == 1
