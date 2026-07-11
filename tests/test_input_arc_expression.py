"""Tests for InputArc.expression — CPN input arc expressions for filtering/ordering."""

import time

from cpnx.engine import PetriNet
from cpnx.places import Place
from cpnx.tokens import Token
from cpnx.transitions import InputArc, OutputArc, Transition


class TestInputArcExpression:
    def test_expression_none_uses_fifo(self):
        """Default (no expression) consumes tokens in deposit order (verified via step)."""
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
        first = Token(payload={"order": 1})
        second = Token(payload={"order": 2})
        net.deposit("input", first)
        net.deposit("input", second)

        # Verify FIFO by checking which token is consumed first: peek before step
        assert net.places["input"].peek()[0].payload["order"] == 1

        net.run(deadline=time.monotonic() + 2.0)
        out_orders = {t.payload["order"] for t in net.places["output"].tokens}
        assert out_orders == {1, 2}

    def test_expression_priority_order(self):
        """Expression sorts by score descending; all tokens processed, best scores first."""
        net = PetriNet(
            places=[Place("leads"), Place("processed")],
            transitions=[
                Transition(
                    name="pick_best",
                    inputs=[
                        InputArc(
                            "leads",
                            count=1,
                            expression=lambda tokens: sorted(tokens, key=lambda t: -t.payload.get("score", 0)),
                        )
                    ],
                    outputs=[OutputArc("processed")],
                    action=lambda tokens: tokens,
                )
            ],
        )

        net.deposit("leads", Token(payload={"score": 0.3, "name": "low"}))
        net.deposit("leads", Token(payload={"score": 0.9, "name": "high"}))
        net.deposit("leads", Token(payload={"score": 0.6, "name": "mid"}))

        net.run(deadline=time.monotonic() + 2.0)

        # All three should be processed — check names as a set
        processed = net.places["processed"].tokens
        names = {t.payload["name"] for t in processed}
        assert names == {"high", "mid", "low"}
        # The first step always picks the highest score (expression is re-evaluated per step)
        assert len(processed) == 3

    def test_expression_consumes_by_color_when_all_match(self):
        """Expression filtering by color consumes matching tokens, leaving others."""
        net = PetriNet(
            places=[Place("input", color_set=None), Place("priority_out")],
            transitions=[
                Transition(
                    name="priority_lane",
                    inputs=[
                        InputArc(
                            "input",
                            count=1,
                            expression=lambda tokens: [t for t in tokens if t.color == "priority"],
                        )
                    ],
                    outputs=[OutputArc("priority_out")],
                    action=lambda tokens: tokens,
                )
            ],
        )

        p1 = Token(color="priority", payload={"n": 1})
        p2 = Token(color="priority", payload={"n": 2})
        net.deposit("input", p1)
        net.deposit("input", p2)

        net.run(deadline=time.monotonic() + 2.0)

        assert len(net.places["priority_out"].tokens) == 2
        assert {t.payload["n"] for t in net.places["priority_out"].tokens} == {1, 2}

    def test_expression_count_respected(self):
        """Expression orders tokens; engine still consumes exactly arc.count of them."""
        net = PetriNet(
            places=[Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="take_two_best",
                    inputs=[
                        InputArc(
                            "input",
                            count=2,
                            expression=lambda tokens: sorted(tokens, key=lambda t: -t.payload.get("score", 0)),
                        )
                    ],
                    outputs=[OutputArc("output", count=2)],
                    action=lambda tokens: tokens,
                )
            ],
        )

        for score in [0.1, 0.9, 0.5, 0.7]:
            net.deposit("input", Token(payload={"score": score}))

        net.run(deadline=time.monotonic() + 2.0)

        out_scores = {t.payload["score"] for t in net.places["output"].tokens}
        assert out_scores == {0.1, 0.9, 0.5, 0.7}  # all four processed across two firings

    def test_retrieve_specific_removes_correct_tokens(self):
        """Place.retrieve_specific removes exactly the requested tokens by id."""
        p = Place("p")
        t1 = Token(payload={"x": 1})
        t2 = Token(payload={"x": 2})
        t3 = Token(payload={"x": 3})
        p.deposit(t1)
        p.deposit(t2)
        p.deposit(t3)

        retrieved = p.retrieve_specific([t3, t1])
        assert {t.id for t in retrieved} == {t1.id, t3.id}
        assert len(p.tokens) == 1
        assert p.tokens[0].id == t2.id


class TestInputArcMultiplicity:
    """CPN arc multiplicity is all-or-nothing: an arc demanding `count` tokens is
    not enabled unless at least `count` tokens satisfy its selection expression.

    Regression coverage for the partial-count-firing and zero-token-livelock bugs.
    """

    def test_underyielding_expression_disables_transition(self):
        """count=2 with an expression that selects only 1 token must NOT fire."""
        net = PetriNet()
        net.add_place(Place("p"))
        net.add_place(Place("out"))
        received: list[int] = []
        arc = InputArc("p", count=2, expression=lambda tokens: tokens[:1])  # under-selects
        t = Transition(
            "t",
            inputs=[arc],
            outputs=[OutputArc("out")],
            action=lambda toks: received.append(len(toks)) or toks,
        )
        net.add_transition(t)
        net.deposit("p", Token())
        net.deposit("p", Token())  # 2 physically present, but expression yields 1

        assert net._is_transition_enabled(t) is False
        assert net.step() is False
        net.run(deadline=time.monotonic() + 0.3)

        assert received == []  # action never invoked
        assert len(net.places["p"]) == 2  # both tokens remain
        assert len(net.places["out"]) == 0

    def test_enabled_and_quiescence_checks_agree_when_underyielding(self):
        """The firing check and the quiescence check must not disagree (no busy-wait)."""
        net = PetriNet()
        net.add_place(Place("p"))
        arc = InputArc("p", count=2, expression=lambda tokens: tokens[:1])
        t = Transition("t", inputs=[arc], outputs=[], action=lambda toks: toks)
        net.add_transition(t)
        net.deposit("p", Token())
        net.deposit("p", Token())

        assert net._is_transition_enabled(t) is False
        assert net._is_transition_potentially_enabled(t) is False
        assert net.is_quiescent() is True  # would be False (livelock) if the two disagreed

    def test_empty_selection_does_not_livelock(self):
        """An expression matching nothing (with tokens present) must not fire at all."""
        net = PetriNet()
        net.add_place(Place("p"))
        fires = {"n": 0}
        arc = InputArc("p", count=1, expression=lambda tokens: [])  # matches nothing
        t = Transition(
            "t",
            inputs=[arc],
            outputs=[],
            action=lambda toks: fires.__setitem__("n", fires["n"] + 1) or toks,
        )
        net.add_transition(t)
        net.deposit("p", Token())

        assert net._is_transition_enabled(t) is False
        assert net.is_quiescent() is True
        net.run(deadline=time.monotonic() + 0.3)

        assert fires["n"] == 0  # never fired — no zero-token livelock
        assert len(net.places["p"]) == 1  # token still present

    def test_fires_once_selection_reaches_count(self):
        """Non-regression: when enough tokens satisfy the selection, the arc fires and
        consumes exactly count."""
        net = PetriNet()
        net.add_place(Place("p"))
        net.add_place(Place("out"))
        received: list[int] = []
        # selects tokens whose payload flag is set; needs 2
        arc = InputArc("p", count=2, expression=lambda tokens: [t for t in tokens if t.payload.get("ok")])
        t = Transition(
            "t",
            inputs=[arc],
            outputs=[OutputArc("out", count=2)],
            action=lambda toks: received.append(len(toks)) or toks,
        )
        net.add_transition(t)
        net.deposit("p", Token(payload={"ok": True}))
        net.deposit("p", Token(payload={"ok": False}))  # only 1 matches → not enabled yet

        assert net._is_transition_enabled(t) is False
        net.deposit("p", Token(payload={"ok": True}))  # now 2 match → enabled

        assert net._is_transition_enabled(t) is True
        net.run(deadline=time.monotonic() + 2.0)
        assert received == [2]  # fired once, consuming exactly 2
        assert len(net.places["out"]) == 2

    def test_resolve_input_tokens_multiplicity_rule(self):
        """Direct unit test of the shared resolver's all-or-nothing rule."""
        net = PetriNet()
        a, b, c = Token(), Token(), Token()

        # default arc (no expression): count satisfied by slicing
        assert net._resolve_input_tokens(InputArc("p", count=2), [a, b, c]) == [a, b]
        # default arc under-supplied: fewer available than count -> None
        assert net._resolve_input_tokens(InputArc("p", count=2), [a]) is None
        # expression yields fewer than count -> None
        assert net._resolve_input_tokens(InputArc("p", count=2, expression=lambda toks: toks[:1]), [a, b]) is None
        # expression yields >= count -> first count returned
        assert net._resolve_input_tokens(InputArc("p", count=2, expression=lambda toks: toks), [a, b, c]) == [a, b]
        # empty expression result -> None (no zero-token firing)
        assert net._resolve_input_tokens(InputArc("p", count=1, expression=lambda toks: []), [a]) is None
        # consume_all with tokens present -> all of them
        assert net._resolve_input_tokens(InputArc("p", consume_all=True), [a, b, c]) == [a, b, c]
        # consume_all on empty -> None (no empty occurrence)
        assert net._resolve_input_tokens(InputArc("p", consume_all=True), []) is None
        # expression that raises -> None (unchanged behavior)
        assert net._resolve_input_tokens(InputArc("p", count=1, expression=lambda toks: 1 / 0), [a]) is None
