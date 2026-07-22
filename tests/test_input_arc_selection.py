"""Tests for InputArc.key / InputArc.filter — per-token CPN input arc selection.

``InputArc`` no longer accepts an opaque ``expression: list[Token] -> list[Token]``
transform. Selection is split into its two honest per-token halves:

- ``filter`` — a ``Callable[[Token], bool]`` eligibility predicate; tokens it rejects
  stay in the place.
- ``key`` — a ``Callable[[Token], object]`` sort key; eligible tokens are consumed in
  **ascending** (min-first) key order, ties broken by insertion order (stable sort).

The engine applies ``filter`` first, then orders survivors by ``key``, then takes the
first ``count``. With neither set, the arc is plain FIFO.
"""

import time
import warnings

import pytest

from cpnx.engine import PetriNet
from cpnx.places import Place
from cpnx.tokens import Token
from cpnx.transitions import InputArc, OutputArc, Transition


class TestInputArcSelection:
    def test_no_key_or_filter_uses_fifo(self):
        """Default (no key/filter) consumes tokens in deposit order (verified via step)."""
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

    def test_key_orders_highest_score_first(self):
        """key negates score so ascending (min-first) order yields highest score first;
        all tokens are still eventually processed (the key is re-applied per step, over
        whatever remains in the place — not baked in once at deposit time).

        `max_workers=1` makes action completion order match firing order, so the final
        `processed` order reflects consumption order deterministically.
        """
        net = PetriNet(
            max_workers=1,
            places=[Place("leads"), Place("processed")],
            transitions=[
                Transition(
                    name="pick_best",
                    inputs=[
                        InputArc(
                            "leads",
                            count=1,
                            key=lambda t: -t.payload.get("score", 0),
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

        # Deposit (FIFO) order would pick "low" first; the key must override that.
        assert net.places["leads"].peek()[0].payload["name"] == "low"

        net.run(deadline=time.monotonic() + 2.0)

        # All three should be processed, highest score consumed (and thus deposited) first.
        processed = net.places["processed"].tokens
        names = [t.payload["name"] for t in processed]
        assert names == ["high", "mid", "low"]

    def test_filter_consumes_by_color_when_all_match(self):
        """filter by color consumes only matching tokens, leaving non-matching ones."""
        net = PetriNet(
            places=[Place("input", color_set=None), Place("priority_out")],
            transitions=[
                Transition(
                    name="priority_lane",
                    inputs=[
                        InputArc(
                            "input",
                            count=1,
                            filter=lambda t: t.color == "priority",
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

    def test_key_count_respected(self):
        """key orders tokens; engine still consumes exactly arc.count of them."""
        net = PetriNet(
            places=[Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="take_two_best",
                    inputs=[
                        InputArc(
                            "input",
                            count=2,
                            key=lambda t: -t.payload.get("score", 0),
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


class TestInputArcSelectionSemantics:
    """Coverage for the now-orthogonal key/filter combinations and their ordering rules."""

    def test_key_alone_orders_ascending_min_first(self):
        """With no filter, key alone orders all eligible tokens ascending (min-first)."""
        net = PetriNet()
        net.add_place(Place("p"))
        net.add_place(Place("out"))
        arc = InputArc("p", count=3, key=lambda t: t.payload["rank"])
        t = Transition(
            "t",
            inputs=[arc],
            outputs=[OutputArc("out", count=3)],
            action=lambda toks: toks,
        )
        net.add_transition(t)
        net.deposit("p", Token(payload={"rank": 3}))
        net.deposit("p", Token(payload={"rank": 1}))
        net.deposit("p", Token(payload={"rank": 2}))

        net.run(deadline=time.monotonic() + 2.0)

        # Consumed in ascending rank order: 1, 2, 3.
        out_ranks = [t.payload["rank"] for t in net.places["out"].tokens]
        assert out_ranks == [1, 2, 3]

    def test_filter_alone_preserves_fifo_among_survivors(self):
        """With no key, filter alone preserves the deposit (FIFO) order among survivors."""
        net = PetriNet()
        net.add_place(Place("p"))
        net.add_place(Place("out"))
        arc = InputArc("p", count=2, filter=lambda t: t.payload["ok"])
        t = Transition(
            "t",
            inputs=[arc],
            outputs=[OutputArc("out", count=2)],
            action=lambda toks: toks,
        )
        net.add_transition(t)
        net.deposit("p", Token(payload={"ok": True, "seq": 1}))
        net.deposit("p", Token(payload={"ok": False, "seq": 2}))  # ineligible, skipped
        net.deposit("p", Token(payload={"ok": True, "seq": 3}))
        net.deposit("p", Token(payload={"ok": True, "seq": 4}))

        net.run(deadline=time.monotonic() + 2.0)

        # First two eligible-by-deposit-order tokens are consumed: seq 1 then seq 3.
        out_seqs = [t.payload["seq"] for t in net.places["out"].tokens]
        assert out_seqs == [1, 3]
        # The remaining eligible token (seq 4) and the ineligible one (seq 2) stay behind.
        assert {t.payload["seq"] for t in net.places["p"].tokens} == {2, 4}

    def test_filter_applied_before_key(self):
        """filter narrows the pool first; key then orders only what survives filtering."""
        net = PetriNet()
        net.add_place(Place("p"))
        net.add_place(Place("out"))
        arc = InputArc(
            "p",
            count=2,
            filter=lambda t: t.payload["eligible"],
            key=lambda t: t.payload["rank"],
        )
        t = Transition(
            "t",
            inputs=[arc],
            outputs=[OutputArc("out", count=2)],
            action=lambda toks: toks,
        )
        net.add_transition(t)
        # An ineligible token with the lowest rank must NOT be selected, even though
        # key would otherwise prefer it — filter runs first and excludes it entirely.
        net.deposit("p", Token(payload={"eligible": False, "rank": 0}))
        net.deposit("p", Token(payload={"eligible": True, "rank": 3}))
        net.deposit("p", Token(payload={"eligible": True, "rank": 1}))
        net.deposit("p", Token(payload={"eligible": True, "rank": 2}))

        net.run(deadline=time.monotonic() + 2.0)

        out_ranks = sorted(t.payload["rank"] for t in net.places["out"].tokens)
        assert out_ranks == [1, 2]  # the two lowest-ranked *eligible* tokens
        remaining_ranks = {t.payload["rank"] for t in net.places["p"].tokens}
        assert remaining_ranks == {0, 3}  # ineligible token and the un-selected eligible one

    def test_key_ties_fall_back_to_insertion_order(self):
        """Equal keys are a stable sort: the earlier-deposited token wins the tie.

        `max_workers=1` makes action completion order match firing order, so the final
        `out` order reflects consumption order deterministically.
        """
        net = PetriNet(max_workers=1)
        net.add_place(Place("p"))
        net.add_place(Place("out"))
        arc = InputArc("p", count=1, key=lambda t: t.payload["rank"])
        t = Transition(
            "t",
            inputs=[arc],
            outputs=[OutputArc("out")],
            action=lambda toks: toks,
        )
        net.add_transition(t)
        earlier = Token(payload={"rank": 5, "which": "earlier"})
        later = Token(payload={"rank": 5, "which": "later"})  # same key, deposited after
        net.deposit("p", earlier)
        net.deposit("p", later)

        net.run(deadline=time.monotonic() + 2.0)

        assert [t.payload["which"] for t in net.places["out"].tokens] == ["earlier", "later"]

    def test_filter_non_bool_return_annotation_raises(self):
        """A filter whose *annotated* return type is not bool raises TypeError at construction."""

        def bad_filter(t) -> int:
            return 1

        with pytest.raises(TypeError, match="InputArc.filter must return bool"):
            InputArc("p", filter=bad_filter)

    def test_key_with_int_annotation_does_not_raise(self):
        """key is never bool-checked, regardless of its return annotation."""

        def rank_key(t) -> int:
            return t.payload["rank"]

        # Must not raise — key legitimately returns an arbitrary comparable, not bool.
        InputArc("p", key=rank_key)


class TestInputArcMultiplicity:
    """CPN arc multiplicity is all-or-nothing: an arc demanding `count` tokens is
    not enabled unless at least `count` tokens satisfy its filter/key selection.

    Regression coverage for the partial-count-firing and zero-token-livelock bugs.
    """

    def test_underselecting_filter_disables_transition(self):
        """count=2 with a filter that admits only 1 of the 2 deposited tokens must NOT fire.

        There is no positional-truncation analogue to the old
        ``expression=lambda tokens: tokens[:1]`` under-selection (key/filter can no
        longer discard by position); the same "not enough eligible tokens" intent is
        expressed here via a filter that only one of the two deposited tokens satisfies.
        """
        net = PetriNet()
        net.add_place(Place("p"))
        net.add_place(Place("out"))
        received: list[int] = []
        arc = InputArc("p", count=2, filter=lambda t: t.payload.get("selected"))  # under-selects
        t = Transition(
            "t",
            inputs=[arc],
            outputs=[OutputArc("out")],
            action=lambda toks: received.append(len(toks)) or toks,
        )
        net.add_transition(t)
        net.deposit("p", Token(payload={"selected": True}))
        net.deposit("p", Token(payload={"selected": False}))  # 2 physically present, but only 1 eligible

        assert net._is_transition_enabled(t) is False
        assert net.step() is False
        net.run(deadline=time.monotonic() + 0.3)

        assert received == []  # action never invoked
        assert len(net.places["p"]) == 2  # both tokens remain
        assert len(net.places["out"]) == 0

    def test_enabled_and_quiescence_checks_agree_when_underselecting(self):
        """The firing check and the quiescence check must not disagree (no busy-wait)."""
        net = PetriNet()
        net.add_place(Place("p"))
        arc = InputArc("p", count=2, filter=lambda t: t.payload.get("selected"))
        t = Transition("t", inputs=[arc], outputs=[], action=lambda toks: toks)
        net.add_transition(t)
        net.deposit("p", Token(payload={"selected": True}))
        net.deposit("p", Token(payload={"selected": False}))

        assert net._is_transition_enabled(t) is False
        assert net._is_transition_potentially_enabled(t) is False
        assert net.is_quiescent() is True  # would be False (livelock) if the two disagreed

    def test_empty_selection_does_not_livelock(self):
        """A filter matching nothing (with tokens present) must not fire at all."""
        net = PetriNet()
        net.add_place(Place("p"))
        fires = {"n": 0}
        arc = InputArc("p", count=1, filter=lambda t: False)  # matches nothing
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
        """Non-regression: when enough tokens satisfy the filter, the arc fires and
        consumes exactly count."""
        net = PetriNet()
        net.add_place(Place("p"))
        net.add_place(Place("out"))
        received: list[int] = []
        # selects tokens whose payload flag is set; needs 2
        arc = InputArc("p", count=2, filter=lambda t: t.payload.get("ok"))
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

        # default arc (no key/filter): count satisfied by slicing
        assert net._resolve_input_tokens(InputArc("p", count=2), [a, b, c]) == [a, b]
        # default arc under-supplied: fewer available than count -> None
        assert net._resolve_input_tokens(InputArc("p", count=2), [a]) is None
        # filter yields fewer than count -> None
        assert net._resolve_input_tokens(InputArc("p", count=2, filter=lambda t: t is a), [a, b]) is None
        # key alone (no filter) yields >= count -> first count returned, ordered by key
        assert net._resolve_input_tokens(InputArc("p", count=2, key=lambda t: 0), [a, b, c]) == [a, b]
        # empty filter result -> None (no zero-token firing)
        assert net._resolve_input_tokens(InputArc("p", count=1, filter=lambda t: False), [a]) is None
        # consume_all with tokens present -> all of them
        assert net._resolve_input_tokens(InputArc("p", consume_all=True), [a, b, c]) == [a, b, c]
        # consume_all on empty -> None (no empty occurrence)
        assert net._resolve_input_tokens(InputArc("p", consume_all=True), []) is None
        # a raising key -> None (unchanged behavior)
        assert net._resolve_input_tokens(InputArc("p", count=1, key=lambda t: 1 / 0), [a]) is None
        # a raising filter -> None as well
        assert net._resolve_input_tokens(InputArc("p", count=1, filter=lambda t: 1 / 0), [a]) is None

    def test_incomparable_keys_make_the_arc_unsatisfiable(self):
        """A `key` returning mutually incomparable values disables the arc, and does not raise.

        A *distinct* branch from a raising key: the `TypeError` originates in `list.sort`
        while comparing two extracted values, not in the callable itself — so it is only
        caught because the sort sits inside `_order_available`'s `try`. This is the shape a
        user actually hits (a key returning `None` for some tokens and an `int` for others),
        and it is the failure mode the "totally ordered *with each other*" requirement on
        `InputArc.key` exists to warn about.
        """
        net = PetriNet()
        a, b = Token(payload={"k": 1}), Token(payload={"k": "x"})
        arc = InputArc("p", count=1, key=lambda t: t.payload["k"])

        # int vs str have no ordering between them -> unsatisfiable, not an exception.
        assert net._resolve_input_tokens(arc, [a, b]) is None
        assert net._order_available(arc, [a, b]) is None

        # The same key is fine when the values *are* mutually comparable.
        c = Token(payload={"k": 0})
        assert net._resolve_input_tokens(arc, [a, c]) == [c]

    def test_incomparable_keys_disable_the_transition_without_raising(self):
        """End-to-end: a poison token makes the transition unbindable, and `run()` still ends."""
        net = PetriNet()
        net.add_place(Place("p"))
        net.add_place(Place("out"))
        fired: list[object] = []
        t = Transition(
            "t",
            inputs=[InputArc("p", count=1, key=lambda tok: tok.payload["k"])],
            outputs=[OutputArc("out")],
            action=lambda toks: fired.append(toks) or toks,
        )
        net.add_transition(t)
        net.deposit("p", Token(payload={"k": 1}))
        net.deposit("p", Token(payload={"k": "x"}))  # incomparable with the first

        assert net._is_transition_enabled(t) is False
        assert net.step() is False
        net.run(deadline=time.monotonic() + 0.5)  # must not raise, must not spin forever
        assert fired == []
        assert len(net.places["p"]) == 2  # nothing consumed

    def test_consume_all_bypasses_key_and_filter(self):
        """`consume_all` drains everything — a filter-rejected token is consumed anyway.

        This is a deliberate, documented footgun (see the warning on `InputArc`): a
        draining arc preserves the pre-`key`/`filter` behavior of the arc inscription it
        replaced and takes the whole available pool in FIFO order. Pinned here so the
        interaction cannot drift silently in either direction — changing it is a breaking
        change that must be made on purpose.
        """
        net = PetriNet()
        a, b, c = Token(), Token(), Token()

        # `filter` rejects everything, yet a draining arc still consumes all three.
        with pytest.warns(UserWarning, match="consume_all=True ignores"):
            arc = InputArc("p", consume_all=True, filter=lambda t: False)
        assert net._resolve_input_tokens(arc, [a, b, c]) == [a, b, c]

        # `key` would reverse the order, yet a draining arc still hands over FIFO.
        order = {a: 2, b: 1, c: 0}
        with pytest.warns(UserWarning, match="consume_all=True ignores"):
            keyed = InputArc("p", consume_all=True, key=lambda t: order[t])
        assert net._resolve_input_tokens(keyed, [a, b, c]) == [a, b, c]


class TestConsumeAllSelectionWarning:
    """Constructing a draining arc with `key`/`filter` warns that they are ignored.

    The bypass itself is pinned by `test_consume_all_bypasses_key_and_filter`; these
    tests pin that it is *audible*, since a silently-ignored `filter` reads as a
    declaration of eligibility that the engine does not honour.
    """

    def test_warns_and_names_both_ignored_parameters(self):
        with pytest.warns(UserWarning, match=r"ignores `key` and `filter`") as record:
            InputArc("p", consume_all=True, key=lambda t: 0, filter=lambda t: True)
        assert len(record) == 1, "one warning for the finished arc, not one per field"

    def test_warns_naming_only_the_parameter_actually_set(self):
        with pytest.warns(UserWarning, match=r"ignores `filter`"):
            InputArc("p", consume_all=True, filter=lambda t: True)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"consume_all": True},  # draining, but nothing to ignore
            {"key": lambda t: 0},  # selection, but not draining
            {"filter": lambda t: True},
            {"key": lambda t: 0, "filter": lambda t: True},
            {},
        ],
    )
    def test_no_warning_without_the_conflict(self, kwargs):
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # any warning becomes a test failure
            InputArc("p", **kwargs)

    def test_warns_on_reassignment_in_either_direction(self):
        """The conflict can also be created after construction — both orders must warn."""
        arc = InputArc("p", consume_all=True)
        with pytest.warns(UserWarning, match=r"ignores `key`"):
            arc.key = lambda t: 0

        other = InputArc("p", key=lambda t: 0)
        with pytest.warns(UserWarning, match=r"ignores `key`"):
            other.consume_all = True

    def test_resolving_the_conflict_stops_warning(self):
        with pytest.warns(UserWarning, match="consume_all=True ignores"):
            arc = InputArc("p", consume_all=True, key=lambda t: 0)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            arc.consume_all = False  # no longer draining → nothing is being ignored
            arc.key = lambda t: 1
