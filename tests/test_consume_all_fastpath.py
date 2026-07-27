"""Regression tests for the `consume_all` lazy-drain fast path (`_try_count_only_binding`).

A guard-free, head-only `consume_all` transition resolves *lazily*: its binding carries the
`_DRAIN` marker instead of the materialized pool, so enablement probes and losing selection
steps never do the O(N) full-pool peek that made draining a deep place O(N²). The whole-pool
consumption happens once, at firing time, via `Place.retrieve_all`.

These tests pin the behaviour that must be preserved:

* the drain still takes the *entire* pool (deep places included);
* a **guard** or a **`binding_priority_key`** disqualifies the fast path, so those callables
  still see the materialized batch (the correctness-critical exclusion);
* `consume_all` keeps ignoring `key`/`filter` on the fast path, exactly as before;
* `is_dead` / `is_quiescent` still read correctly through the count-only probe.
"""

import warnings

from cpnx.engine import PetriNet
from cpnx.places import Place, SinkPlace, ThresholdPlace
from cpnx.tokens import Token
from cpnx.transitions import BindingPolicy, InputArc, OutputArc, Transition


def _summary_action(tokens: list[Token]) -> list[Token]:
    """Fold a swept batch into one token recording how many were consumed."""
    return [Token(payload={"count": len(tokens)})]


def test_deep_place_drains_in_one_firing():
    """A guard-free consume_all arc consumes the whole pool in a single firing (deep place)."""
    n = 500
    net = PetriNet(
        places=[Place("inbox"), SinkPlace("out", keep_last=1)],
        transitions=[
            Transition("drain", [InputArc("inbox", consume_all=True)], [OutputArc("out", count=1)], _summary_action)
        ],
    )
    with net:
        for i in range(n):
            net.deposit("inbox", Token(payload={"i": i}))
        net.drive_to_quiescence()
        assert len(net.places["inbox"]) == 0
        assert net.places["out"].stats()["absorbed"] == 1
        # The single commit swept every one of the N tokens, not just arc.count.
        assert net.places["out"].tokens[-1].payload["count"] == n


def test_threshold_gate_then_full_sweep():
    """ThresholdPlace + consume_all: blocked below threshold, sweeps the whole pool at/above it."""
    net = PetriNet(
        places=[ThresholdPlace("votes", threshold=80), SinkPlace("ledger", keep_last=1)],
        transitions=[
            Transition(
                "commit",
                [InputArc("votes", count=80, consume_all=True)],
                [OutputArc("ledger", count=1)],
                _summary_action,
            )
        ],
    )
    with net:
        for i in range(79):
            net.deposit("votes", Token(payload={"i": i}))
        # 79 < 80: the count-only probe must report the net dead (barrier closed).
        assert net.is_dead()
        assert net.places["ledger"].stats()["absorbed"] == 0

        for i in range(79, 95):  # now 95 total, over the barrier
            net.deposit("votes", Token(payload={"i": i}))
        net.drive_to_quiescence()
        assert net.places["ledger"].stats()["absorbed"] == 1
        assert net.places["ledger"].tokens[-1].payload["count"] == 95  # all 95, not 80
        assert len(net.places["votes"]) == 0


def test_guard_still_sees_the_batch():
    """A guarded consume_all transition is excluded from the fast path, so its guard sees tokens.

    The guard requires at least 3 tokens in the swept batch. With 2 present it must block; the
    3rd deposit must release it. This can only pass if the guard is evaluated against the
    materialized pool — i.e. the fast path correctly falls through for guarded transitions.
    """
    net = PetriNet(
        places=[Place("inbox"), SinkPlace("out", keep_last=1)],
        transitions=[
            Transition(
                "drain",
                [InputArc("inbox", consume_all=True)],
                [OutputArc("out", count=1)],
                _summary_action,
                guard=lambda toks: len(toks) >= 3,
            )
        ],
    )
    with net:
        net.deposit("inbox", Token())
        net.deposit("inbox", Token())
        assert net.is_dead()  # guard sees only 2 -> blocked
        assert net.places["out"].stats()["absorbed"] == 0

        net.deposit("inbox", Token())
        net.drive_to_quiescence()
        assert net.places["out"].stats()["absorbed"] == 1
        assert net.places["out"].tokens[-1].payload["count"] == 3


def test_priority_key_excluded_from_fast_path():
    """A consume_all transition with a binding_priority_key still resolves (fast path skipped)."""
    net = PetriNet(
        places=[Place("inbox"), SinkPlace("out", keep_last=1)],
        transitions=[
            Transition(
                "drain",
                [InputArc("inbox", consume_all=True)],
                [OutputArc("out", count=1)],
                _summary_action,
                binding_policy=BindingPolicy.PRIORITY,
                binding_priority_key=lambda toks: min(t.created_at for t in toks),
            )
        ],
    )
    with net:
        for i in range(10):
            net.deposit("inbox", Token(payload={"i": i}))
        net.drive_to_quiescence()
        assert net.places["out"].stats()["absorbed"] == 1
        assert net.places["out"].tokens[-1].payload["count"] == 10


def test_consume_all_still_ignores_filter_on_fast_path():
    """consume_all keeps draining every token in FIFO order, ignoring `filter` (documented footgun)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # consume_all + filter emits a UserWarning by design
        arc = InputArc("inbox", consume_all=True, filter=lambda t: t.payload["ok"])
    net = PetriNet(
        places=[Place("inbox"), SinkPlace("out", keep_last=1)],
        transitions=[Transition("drain", [arc], [OutputArc("out", count=1)], _summary_action)],
    )
    with net:
        net.deposit("inbox", Token(payload={"ok": True}))
        net.deposit("inbox", Token(payload={"ok": False}))  # filter would reject — but consume_all ignores it
        net.deposit("inbox", Token(payload={"ok": True}))
        net.drive_to_quiescence()
        assert net.places["out"].tokens[-1].payload["count"] == 3  # all three swept, filter notwithstanding
        assert len(net.places["inbox"]) == 0


def test_mixed_consume_all_and_plain_arc():
    """A transition mixing a consume_all arc with a plain count arc resolves and consumes both."""

    def action(tokens: list[Token]) -> list[Token]:
        return [Token(payload={"count": len(tokens)})]

    net = PetriNet(
        places=[Place("bulk"), Place("trigger"), SinkPlace("out", keep_last=1)],
        transitions=[
            Transition(
                "drain",
                [InputArc("bulk", consume_all=True), InputArc("trigger")],
                [OutputArc("out", count=1)],
                action,
            )
        ],
    )
    with net:
        for i in range(5):
            net.deposit("bulk", Token(payload={"i": i}))
        # No trigger yet: the plain arc is unmet, so the transition is not enabled.
        assert net.is_dead()
        net.deposit("trigger", Token())
        net.drive_to_quiescence()
        assert net.places["out"].stats()["absorbed"] == 1
        # 5 bulk + 1 trigger all consumed in the one firing.
        assert net.places["out"].tokens[-1].payload["count"] == 6
        assert len(net.places["bulk"]) == 0
        assert len(net.places["trigger"]) == 0
