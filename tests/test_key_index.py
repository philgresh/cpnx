"""Tests for the persistent per-arc key-index (issue #25, PR 2).

A certified ``InputArc.key`` is served from a ``(key, seq)`` min-heap maintained on the
place across firings, instead of re-sorting the whole available pool on every firing. The
index is a **pure optimisation**: every read can decline, and the engine then computes the
same answer the slow way. So the tests come in three groups:

- *equivalence* — the indexed path and the per-firing sort produce byte-identical
  consumption order, including under key ties and filters;
- *decline* — every condition that must fall back actually does (timed tokens, uncertified
  callables, a broken key), and the net still behaves exactly as PR 1 specified;
- *maintenance* — the heap tracks deposits, removals, and post-construction key
  reassignment without leaking stale entries or serving them.

Note on what each group can actually catch. `_order_available` re-sorts whatever pool it is
given, so a *mis-ordered* index read is invisible end-to-end — the sort simply puts it back.
That is a real safety property (the index can only ever be a selection hint, never the
authority on order), but it means the equivalence tests are blind to ordering bugs inside
the heap and only bite when the index returns the wrong *set* — a short read, or one
omitting an eligible token. The maintenance tests are what actually pin heap behaviour;
mutation-testing the index (reversing the order, rotating the selection, defeating the
staleness check) fails 6-8 of them and none of the equivalence tests. Both groups are
needed, for different reasons.
"""

import time

import pytest

import cpnx.places as places_module
from cpnx.certification import certify
from cpnx.engine import PetriNet
from cpnx.places import Place
from cpnx.tokens import Token
from cpnx.transitions import InputArc, OutputArc, Transition


def priority_key(token: Token) -> int:
    """Certified per-token key: ascending payload priority."""
    return token.payload["p"]


def even_filter(token: Token) -> bool:
    """Certified per-token eligibility: even priorities only."""
    return token.payload["p"] % 2 == 0


_MUTABLE: dict[str, int] = {}


def uncertified_key(token: Token) -> int:
    """Reads mutable external state, so `certify` rejects it."""
    return _MUTABLE.get("offset", 0) + token.payload["p"]


class TestKeyIndexEquivalence:
    """The index must never change *what* the engine does, only how fast it does it."""

    @staticmethod
    def _drain(n, *, use_index, with_filter=False, tie_mod=None, monkeypatch=None):
        """Run a keyed drain and return the consumption order, optionally forcing fallback."""
        if not use_index:
            monkeypatch.setattr(places_module._TokenStore, "peek_by_key", lambda self, i, k, p=None: None)
        net = PetriNet(max_workers=1)
        net.add_place(Place("in"))
        net.add_place(Place("out"))
        order: list[int] = []
        arc = InputArc("in", key=priority_key, filter=even_filter) if with_filter else InputArc("in", key=priority_key)
        net.add_transition(
            Transition(
                "t",
                inputs=[arc],
                outputs=[OutputArc("out")],
                action=lambda toks: order.append(toks[0].payload["id"]) or toks,
            )
        )
        for i in range(n):
            p = (i % tie_mod) if tie_mod else (i * 7919) % (n * 3)
            net.deposit("in", Token(payload={"p": p, "id": i}))
        net.run(deadline=time.monotonic() + 30)
        return order

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},  # distinct keys
            {"with_filter": True},  # filter applied at pop
            {"tie_mod": 3},  # heavy key ties -> seq tiebreak
            {"tie_mod": 3, "with_filter": True},
        ],
        ids=["distinct-keys", "with-filter", "key-ties", "ties-and-filter"],
    )
    def test_index_and_sort_agree_exactly(self, kwargs, monkeypatch):
        """Indexed and non-indexed drains consume the same tokens in the same order.

        The tie cases matter most: `(key, seq)` heap order has to reproduce the *stable*
        ascending sort PR 1 documented, or a keyed drain stops being deterministic.
        """
        with_index = self._drain(120, use_index=True, **kwargs)
        with monkeypatch.context() as m:
            without_index = self._drain(120, use_index=False, monkeypatch=m, **kwargs)

        assert with_index == without_index
        assert with_index, "the drain must actually consume something for this to mean anything"

    def test_ties_resolve_to_insertion_order(self):
        """Equal keys are consumed oldest-first — the documented `seq` tiebreak."""
        order = self._drain(20, use_index=True, tie_mod=1)  # every key identical
        assert order == sorted(order), "identical keys must degrade to pure FIFO"


class TestKeyIndexDeclines:
    """Every condition under which the index must refuse to answer."""

    @staticmethod
    def _place_with(tokens, key_fn=priority_key, index_id=1):
        place = Place("p")
        place.register_key_index(index_id, key_fn)
        for tok in tokens:
            place.deposit(tok)
        return place

    def test_serves_when_untimed_and_certified(self):
        place = self._place_with([Token(payload={"p": p}) for p in (3, 1, 2)])
        assert [t.payload["p"] for t in place.peek_by_key(1, 3)] == [1, 2, 3]

    def test_declines_while_a_timed_token_is_present(self):
        """A cooling token never enters `_ready`, so the index cannot see the whole pool."""
        place = self._place_with([Token(payload={"p": p}) for p in (3, 1)])
        assert place.peek_by_key(1, 3) is not None  # fine so far
        place.deposit(Token(payload={"p": 0}, available_at=1e18))
        assert place.peek_by_key(1, 3) is None, "must fall back, not serve a partial ordering"

    def test_declines_when_no_index_registered(self):
        place = Place("p")
        place.deposit(Token(payload={"p": 1}))
        assert place.peek_by_key(999, 3) is None

    def test_a_raising_key_disables_the_index_without_breaking_deposit(self):
        """`deposit()` must not raise just because an arc's key is broken."""
        place = Place("p")
        place.register_key_index(1, lambda t: 1 / 0)
        place.deposit(Token(payload={"p": 1}))  # must not raise
        assert place.peek_by_key(1, 3) is None
        assert len(place) == 1, "the token is still stored; only the index gave up"

    def test_incomparable_keys_disable_the_index_without_breaking_deposit(self):
        """Mutually incomparable keys surface from the heap comparison, not the callable."""
        place = Place("p")
        place.register_key_index(1, lambda t: t.payload["p"])
        place.deposit(Token(payload={"p": 1}))
        place.deposit(Token(payload={"p": "x"}))  # must not raise
        assert place.peek_by_key(1, 3) is None
        assert len(place) == 2

    def test_incomparable_keys_still_disable_the_transition_end_to_end(self):
        """The PR 1 guarantee survives the index: the arc goes unsatisfiable, quietly."""
        net = PetriNet(max_workers=1)
        net.add_place(Place("in"))
        net.add_place(Place("out"))
        net.add_transition(
            Transition(
                "t",
                inputs=[InputArc("in", key=priority_key)],
                outputs=[OutputArc("out")],
                action=lambda toks: toks,
            )
        )
        net.deposit("in", Token(payload={"p": 1}))
        net.deposit("in", Token(payload={"p": "x"}))

        assert net.step() is False
        net.run(deadline=time.monotonic() + 0.5)
        assert len(net.places["in"]) == 2

    def test_uncertified_key_is_never_indexed(self):
        """An uncertified key must not run on the deposit path — the engine must not ask."""
        assert certify(uncertified_key).certified is False
        net = PetriNet(max_workers=1)
        net.add_place(Place("in"))
        net.add_place(Place("out"))
        arc = InputArc("in", key=uncertified_key)
        net.add_transition(Transition("t", inputs=[arc], outputs=[OutputArc("out")], action=lambda toks: toks))
        for p in (3, 1, 2):
            net.deposit("in", Token(payload={"p": p}))

        assert net._ensure_key_index(arc, net.places["in"]) is False
        net.run(deadline=time.monotonic() + 5)
        assert len(net.places["out"]) == 3, "still drains, just via the per-firing sort"

    def test_uncertified_filter_blocks_indexing_even_with_a_certified_key(self):
        """A capped index read plus an after-the-fact filter would silently under-select."""
        net = PetriNet(max_workers=1)
        net.add_place(Place("in"))
        arc = InputArc("in", key=priority_key, filter=lambda t: _MUTABLE.get("x", True))
        net.add_transition(Transition("t", inputs=[arc], outputs=[], action=lambda toks: toks))
        assert net._ensure_key_index(arc, net.places["in"]) is False


class TestKeyIndexMaintenance:
    """Deposit, removal, back-fill, and key reassignment keep the heap honest."""

    def test_removal_is_lazily_invalidated(self):
        place = Place("p")
        place.register_key_index(1, priority_key)
        for p in (3, 1, 2, 0):
            place.deposit(Token(payload={"p": p}))
        place.retrieve(1)  # removes the FIFO head (p=3), leaving a stale heap entry

        assert [t.payload["p"] for t in place.peek_by_key(1, 4)] == [0, 1, 2]

    def test_reads_are_non_destructive(self):
        place = Place("p")
        place.register_key_index(1, priority_key)
        for p in (2, 0, 1):
            place.deposit(Token(payload={"p": p}))

        first = [t.payload["p"] for t in place.peek_by_key(1, 3)]
        second = [t.payload["p"] for t in place.peek_by_key(1, 3)]
        assert first == second == [0, 1, 2]
        assert len(place) == 3

    def test_registration_backfills_an_existing_marking(self):
        """An index can be added to a place that already holds tokens."""
        place = Place("p")
        for p in (5, 4, 6):
            place.deposit(Token(payload={"p": p}))
        place.register_key_index(7, priority_key)

        assert [t.payload["p"] for t in place.peek_by_key(7, 3)] == [4, 5, 6]

    def test_filter_at_pop_skips_without_stopping(self):
        """A rejected token is skipped but stays indexed, so the scan continues past it."""
        place = Place("p")
        place.register_key_index(1, priority_key)
        for p in (0, 1, 2, 3, 4):
            place.deposit(Token(payload={"p": p}))

        assert [t.payload["p"] for t in place.peek_by_key(1, 2, even_filter)] == [0, 2]
        # the odd tokens it skipped are still there for another arc
        assert [t.payload["p"] for t in place.peek_by_key(1, 5)] == [0, 1, 2, 3, 4]

    def test_reassigning_arc_key_rebuilds_the_index(self):
        """PR 1 allows `arc.key = f` post-construction; the index must not serve stale order."""
        net = PetriNet(max_workers=1)
        net.add_place(Place("in"))
        arc = InputArc("in", key=priority_key)
        net.add_transition(Transition("t", inputs=[arc], outputs=[], action=lambda toks: toks))
        place = net.places["in"]
        for p in (0, 1, 2):
            net.deposit("in", Token(payload={"p": p}))

        net._ensure_key_index(arc, place)
        assert [t.payload["p"] for t in place.peek_by_key(id(arc), 3)] == [0, 1, 2]

        arc.key = lambda t: -t.payload["p"]  # now descending
        net._ensure_key_index(arc, place)
        assert [t.payload["p"] for t in place.peek_by_key(id(arc), 3)] == [2, 1, 0]


class TestTokenArgCertification:
    """`certify` is arity-agnostic, so per-token keys/filters certify without a whitelist change."""

    @pytest.mark.parametrize(
        "fn",
        [
            lambda t: t.payload["priority"],
            lambda t: t.payload.get("is_synthetic", False),
            lambda t: (t.payload.get("tier", 0), t.payload["seq"]),
            lambda t: -t.payload.get("score", 0),
            lambda t: t.created_at,
            lambda t: t.color == "order",
        ],
        ids=["subscript", "get-with-default", "tuple", "negated", "attribute", "comparison"],
    )
    def test_token_arg_selection_callables_certify(self, fn):
        """These are the real shapes `key`/`filter` take; all must reach the inline path."""
        assert certify(fn).certified is True

    def test_mutable_closure_still_rejected_for_token_args(self):
        assert certify(uncertified_key).certified is False

    def test_certified_key_sets_the_arc_inline_flag(self):
        """`_key_inline_safe` is what the engine reads to decide indexability."""
        assert InputArc("p", key=priority_key)._key_inline_safe is True
        assert InputArc("p", key=uncertified_key)._key_inline_safe is False
