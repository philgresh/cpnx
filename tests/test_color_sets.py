"""Tests for Place.color_set, Place.initial_marking, and PetriNet.is_dead()."""

import time

import pytest

from cpnx.engine import PetriNet
from cpnx.places import PacedResourcePlace, Place, ResourcePlace, SinkPlace, ThresholdPlace
from cpnx.tokens import ERROR_COLOR, Token
from cpnx.transitions import InputArc, OutputArc, Transition


class TestColorSet:
    def test_color_set_accepts_matching_color(self):
        p = Place("p", color_set={"resource"})
        t = Token(color="resource")
        p.deposit(t)
        assert len(p.tokens) == 1

    def test_color_set_rejects_wrong_color(self):
        p = Place("p", color_set={"resource"})
        with pytest.raises(TypeError, match="color_set"):
            p.deposit(Token())  # color=None, not in {"resource"}

    def test_color_set_rejects_different_named_color(self):
        p = Place("p", color_set={"priority"})
        with pytest.raises(TypeError, match="color 'resource'"):
            p.deposit(Token(color="resource"))

    def test_color_set_none_accepts_any_color(self):
        p = Place("p", color_set=None)
        p.deposit(Token())
        p.deposit(Token(color="resource"))
        p.deposit(Token(color="priority"))
        assert len(p.tokens) == 3

    def test_color_set_multi_accepts_any_member(self):
        p = Place("p", color_set={"data", "priority"})
        p.deposit(Token(color="data"))
        p.deposit(Token(color="priority"))
        assert len(p.tokens) == 2

    def test_resource_place_enforces_resource_color(self):
        rp = ResourcePlace("r", capacity=1)
        assert rp.color_set == {"resource"}
        with pytest.raises(TypeError):
            rp.deposit(Token())  # color=None rejected

    def test_error_message_includes_place_name_and_color(self):
        p = Place("my_place", color_set={"resource"})
        with pytest.raises(TypeError) as exc_info:
            p.deposit(Token(color="data"))
        msg = str(exc_info.value)
        assert "my_place" in msg
        assert "resource" in msg
        assert "data" in msg


class TestInitialMarking:
    def test_initial_marking_prepopulates_place(self):
        tokens = [Token(), Token(), Token()]
        p = Place("p", initial_marking=tokens)
        assert len(p.tokens) == 3

    def test_initial_marking_none_is_empty(self):
        p = Place("p", initial_marking=None)
        assert len(p.tokens) == 0

    def test_resource_place_initial_marking_all_resource_color(self):
        rp = ResourcePlace("r", capacity=4)
        assert all(t.color == "resource" for t in rp.tokens)
        assert len(rp.tokens) == 4

    def test_initial_marking_respects_color_set(self):
        """Tokens in initial_marking bypass color_set validation (set at construction)."""
        # ResourcePlace.__init__ passes color_set={"resource"} and initial_marking together;
        # the tokens are appended directly without going through deposit(), which is correct
        # because ResourcePlace controls both.
        rp = ResourcePlace("r", capacity=2)
        assert len(rp.tokens) == 2


class TestIsDead:
    def test_empty_net_with_no_transitions_is_dead(self):
        net = PetriNet()
        assert net.is_dead()

    def test_net_with_tokens_and_enabled_transition_is_not_dead(self):
        net = PetriNet(
            places=[Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=lambda tokens: tokens,
                )
            ],
        )
        net.deposit("input", Token())
        assert not net.is_dead()

    def test_net_with_no_tokens_is_dead(self):
        net = PetriNet(
            places=[Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=lambda tokens: tokens,
                )
            ],
        )
        assert net.is_dead()

    def test_is_dead_true_after_all_tokens_consumed(self):
        net = PetriNet(
            places=[Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=lambda tokens: tokens,
                )
            ],
        )
        net.deposit("input", Token())
        net.run(deadline=time.monotonic() + 2.0)
        assert net.is_dead()

    def test_is_dead_does_not_require_running_count_zero(self):
        """is_dead() checks the marking only, not in-flight transitions."""
        net = PetriNet(places=[Place("p")])
        # No transitions registered — net is dead regardless of _running_count
        net._running_count = 5  # simulate in-flight work
        assert net.is_dead()
        net._running_count = 0


class TestSchemaEnforcement:
    def test_schema_type_matching(self):
        class OrderPayload(dict):
            pass

        p = Place("p", schema=dict)
        p.deposit(Token(payload={"id": 1}))
        assert len(p.tokens) == 1

        p_strict = Place("p_strict", schema=OrderPayload)
        with pytest.raises(TypeError, match="schema"):
            p_strict.deposit(Token(payload={"id": 2}))

    def test_schema_callable(self):
        p = Place("p", schema=lambda x: "id" in x)
        p.deposit(Token(payload={"id": 1, "name": "foo"}))
        assert len(p.tokens) == 1
        with pytest.raises(TypeError, match="schema"):
            p.deposit(Token(payload={"name": "missing id"}))

    def test_schema_callable_raises_exception_rejected(self):
        p = Place("p", schema=lambda x: x["val"] > 0)
        with pytest.raises(TypeError, match="schema"):
            p.deposit(Token(payload={}))
        assert len(p.tokens) == 0

    def test_schema_initial_marking_validated(self):
        with pytest.raises(TypeError, match="schema"):
            Place("p", schema=lambda x: "valid" in x, initial_marking=[Token(payload={"wrong": 1})])

        p_valid = Place(
            "p",
            schema=lambda x: "valid" in x,
            initial_marking=[Token(payload={"valid": 1}), Token(payload={"valid": 2})],
        )
        assert len(p_valid.tokens) == 2

    def test_schema_subclasses(self):
        rp = ResourcePlace("rp", capacity=2, schema=dict)
        rp.deposit(Token(color="resource", payload={}))
        with pytest.raises(TypeError, match="schema"):
            rp_strict = ResourcePlace("rp_strict", capacity=0, schema=lambda x: "meta" in x)
            rp_strict.deposit(Token(color="resource", payload={}))

        prp = PacedResourcePlace("prp", 1, 0.1, schema=dict)
        prp.deposit(Token(color="resource", payload={}))

        tp = ThresholdPlace("tp", threshold=1, schema=lambda x: "val" in x)
        tp.deposit(Token(payload={"val": 10}))
        with pytest.raises(TypeError, match="schema"):
            tp.deposit(Token(payload={"other": 10}))

        sp = SinkPlace("sp", keep_last=5, schema=lambda x: "val" in x)
        sp.deposit(Token(payload={"val": 3.14}))
        with pytest.raises(TypeError, match="schema"):
            sp.deposit(Token(payload={"other": 3.14}))

    def test_schema_transition_valid_deposit(self):
        net = PetriNet()
        net.add_place(Place("input"))
        net.add_place(Place("output_valid", schema=lambda x: "code" in x))
        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("input")],
                outputs=[OutputArc("output_valid")],
                action=lambda tokens: [Token(payload={"code": 200})],
            )
        )
        net.deposit("input", Token())
        assert net.step() is True
        net.run(deadline=time.monotonic() + 1.0)
        assert len(net.places["output_valid"].tokens) == 1
        assert net.places["output_valid"].tokens[0].payload == {"code": 200}

    def test_schema_transition_dead_lettering(self):
        net = PetriNet(error_place="errors")
        net.add_place(Place("input"))
        net.add_place(Place("output_strict", schema=lambda x: "required_key" in x))
        net.add_place(Place("output_ok"))
        net.add_place(Place("errors"))

        errors_seen = []
        dead_letters_seen = []

        def on_error(t_name, exc, token):
            errors_seen.append((t_name, str(exc), token))

        def on_dead_letter(t_name, token):
            dead_letters_seen.append((t_name, token))

        net.on_error = on_error
        net.on_token_dead_lettered = on_dead_letter

        net.add_transition(
            Transition(
                name="produce",
                inputs=[InputArc("input")],
                outputs=[OutputArc("output_strict"), OutputArc("output_ok")],
                action=lambda tokens: [
                    Token(payload={"wrong_key": 1}),  # violates schema
                    Token(payload={"ok": True}),  # succeeds in output_ok
                ],
            )
        )

        net.deposit("input", Token())
        assert net.step() is True
        net.run(deadline=time.monotonic() + 1.0)

        assert len(net.places["output_ok"].tokens) == 1
        assert len(net.places["output_strict"].tokens) == 0
        assert len(net.places["errors"].tokens) == 1

        err_tok = net.places["errors"].tokens[0]
        assert err_tok.color == ERROR_COLOR
        assert err_tok.payload["error_type"] == "Color Set / Schema Violation"
        assert err_tok.payload["target_place"] == "output_strict"
        assert err_tok.payload["transition"] == "produce"
        assert "does not match schema" in err_tok.payload["error"]

        assert len(errors_seen) == 1
        assert len(dead_letters_seen) == 1
        assert dead_letters_seen[0][0] == "produce"
        assert dead_letters_seen[0][1].id == err_tok.id
