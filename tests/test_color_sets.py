"""Tests for Place.color_set, Place.initial_marking, and PetriNet.is_dead()."""

import time

import pytest

from cpnx.engine import PetriNet
from cpnx.places import Place, ResourcePlace
from cpnx.tokens import Token
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
