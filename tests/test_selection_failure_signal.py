"""Tests for `on_error` reporting of a raising `InputArc.key`/`filter` (issue #29).

A selection callable that raises makes its arc unsatisfiable — that part is long-standing
and unchanged. What was missing is the *report*: the transition simply looked disabled,
`run()` reached quiescence with work still pending, and nothing was logged. The engine
signals every other comparable degradation (`on_binding_search_exhausted`, and
`binding_priority_key` failures via `_pending_key_failures`), so selection was the odd one
out — and the per-token split made it sharper, since an opaque `expression` at least let the
author `try/except` internally while a per-token callable's exception escapes into the
engine's blanket handler.

The delicate property here is **edge-triggering**. Selection re-runs on every enabling
check, so the naive implementation fires the callback on every `step()` for as long as the
offending token sits in the place. These tests pin that a persistent fault reports once, a
recovered-then-recurring fault reports again, and an ordinary empty selection never reports
at all.
"""

import time

import pytest

from cpnx.engine import PetriNet
from cpnx.places import Place
from cpnx.tokens import Token
from cpnx.transitions import InputArc, OutputArc, Transition


def boom_key(token: Token) -> int:
    raise ValueError("key exploded")


def boom_filter(token: Token) -> bool:
    raise ValueError("filter exploded")


def payload_key(token: Token) -> int:
    return token.payload["p"]


def _net(arc, *, tokens=(0, 1, 2)):
    """A one-transition net over `arc`, returning it with the list `on_error` writes into."""
    net = PetriNet(max_workers=1)
    net.add_place(Place("in"))
    net.add_place(Place("out"))
    net.add_transition(Transition("t", inputs=[arc], outputs=[OutputArc("out")], action=lambda toks: toks))
    errors: list[tuple[str, Exception]] = []
    net.on_error = lambda name, exc, tok: errors.append((name, exc))
    for p in tokens:
        net.deposit("in", Token(payload={"p": p}))
    return net, errors


class TestSelectionFailureIsReported:
    @pytest.mark.parametrize(
        "arc",
        [InputArc("in", key=boom_key), InputArc("in", filter=boom_filter)],
        ids=["raising-key", "raising-filter"],
    )
    def test_a_raising_selection_callable_reaches_on_error(self, arc):
        net, errors = _net(arc)
        net.run(deadline=time.monotonic() + 0.4)

        assert len(errors) == 1
        name, exc = errors[0]
        assert name == "t"
        assert isinstance(exc, RuntimeError)
        assert "'t'" in str(exc) and "'in'" in str(exc), "must name the transition and the place"
        assert isinstance(exc.__cause__, ValueError), "the original error must be chained"

    def test_the_arc_is_still_unsatisfiable(self):
        """Reporting is additive — the pre-existing disable-the-transition behaviour holds."""
        net, _ = _net(InputArc("in", key=boom_key))
        assert net.step() is False
        net.run(deadline=time.monotonic() + 0.4)
        assert len(net.places["in"]) == 3, "nothing consumed"
        assert len(net.places["out"]) == 0

    def test_a_missing_on_error_handler_is_not_a_crash(self):
        net, _ = _net(InputArc("in", key=boom_key))
        net.on_error = None
        net.run(deadline=time.monotonic() + 0.3)  # must not raise
        assert len(net.places["in"]) == 3

    def test_a_callback_that_itself_raises_is_swallowed(self):
        """`on_error` is user code on the flush path; it must not take the engine down."""
        net, _ = _net(InputArc("in", key=boom_key))

        def exploding_handler(name, exc, tok):
            raise RuntimeError("handler is broken too")

        net.on_error = exploding_handler
        net.run(deadline=time.monotonic() + 0.3)  # must not raise


class TestEdgeTriggering:
    """A persistent fault must report once — not once per enabling check."""

    def test_a_persistent_fault_reports_exactly_once(self):
        net, errors = _net(InputArc("in", key=boom_key))
        for _ in range(25):
            net.step()

        assert len(errors) == 1, f"expected 1 report across 25 steps, got {len(errors)}"

    def test_incomparable_keys_report_once_despite_two_failure_sites(self):
        """A broken key can fail in the key-index *and* in the fallback; that is one event.

        `places._KeyIndex` disables itself when it cannot order a token and the engine falls
        back silently — the fallback then re-runs the same callable and reports. Exactly one
        report must come out, from the fallback, which is the site that knows the transition.
        """
        net, errors = _net(InputArc("in", key=payload_key), tokens=())
        net.deposit("in", Token(payload={"p": 1}))
        net.deposit("in", Token(payload={"p": "x"}))  # incomparable with the first
        for _ in range(10):
            net.step()

        assert len(errors) == 1

    def test_recovery_re_arms_reporting(self):
        """A fault that recurs after recovering is a new event, not a duplicate of the old one."""
        net = PetriNet(max_workers=1)
        net.add_place(Place("in"))
        errors: list[Exception] = []
        net.on_error = lambda name, exc, tok: errors.append(exc)
        # `key` raises only for the poison payload, so removing it restores selection.
        arc = InputArc("in", key=lambda t: 1 / 0 if t.payload["p"] < 0 else t.payload["p"])
        net.add_transition(Transition("t", inputs=[arc], outputs=[], action=lambda toks: toks))

        poison = Token(payload={"p": -1})
        net.deposit("in", poison)
        net.deposit("in", Token(payload={"p": 1}))
        net.step()
        assert len(errors) == 1, "first fault reports"

        net.places["in"].retrieve_specific([poison])  # remove the poison -> selection recovers
        net.step()
        assert len(errors) == 1, "recovery itself is not an event"

        net.deposit("in", Token(payload={"p": -2}))  # fault again
        net.step()
        assert len(errors) == 2, "a recurrence after recovery must report again"


class TestOrdinaryEmptySelectionStaysSilent:
    """Rejecting every token is a normal 'not enabled', not a failure."""

    def test_a_filter_that_rejects_everything_never_reports(self):
        net, errors = _net(InputArc("in", filter=lambda t: False))
        for _ in range(10):
            net.step()
        net.run(deadline=time.monotonic() + 0.3)

        assert errors == []

    def test_a_healthy_keyed_drain_never_reports(self):
        net, errors = _net(InputArc("in", key=payload_key))
        net.run(deadline=time.monotonic() + 2.0)

        assert errors == []
        assert len(net.places["out"]) == 3

    def test_an_under_supplied_arc_never_reports(self):
        """Too few eligible tokens disables the transition, but is not an error."""
        net, errors = _net(InputArc("in", count=5, key=payload_key))
        for _ in range(5):
            net.step()

        assert errors == []


class TestLatchSurvivesPoolChanges:
    """The two ways a success-clears-it latch silently broke (review of #33).

    Both are regressions in *opposite* directions — one mutes a real fault forever, the
    other reports one unchanged fault repeatedly — and both come from the same mistake:
    treating "selection succeeded" as proof the fault is gone. The latch instead records the
    tokens a failing attempt saw, so a fault is "the same one" when it still involves any of
    them.
    """

    def test_a_new_fault_after_the_place_drains_is_still_reported(self):
        """Removing the poison token is the likeliest response to the report — it must not mute.

        `_gather_arc_pools` bails as soon as the place holds fewer than `count` eligible
        tokens, so `_order_available` is never reached and a success-based latch never
        re-arms. Draining the place therefore silenced the arc permanently — the exact
        silent failure #29 exists to remove.
        """
        net = PetriNet(max_workers=1)
        net.add_place(Place("in"))
        errors: list[Exception] = []
        net.on_error = lambda name, exc, tok: errors.append(exc)
        arc = InputArc("in", key=lambda t: 1 / 0 if t.payload["p"] < 0 else t.payload["p"])
        net.add_transition(Transition("t", inputs=[arc], outputs=[], action=lambda toks: toks))

        first = Token(payload={"p": -1})
        net.deposit("in", first)
        net.step()
        assert len(errors) == 1, "first fault reports"

        net.places["in"].retrieve_specific([first])  # operator removes the poison
        assert len(net.places["in"]) == 0
        net.step()  # nothing to select — selection is never even reached

        net.deposit("in", Token(payload={"p": -2}))  # a *different* poison token
        net.step()
        assert len(errors) == 2, "a distinct later fault must still be reported"

    def test_one_cooling_poison_token_reports_once_across_repeated_polls(self):
        """`step()` and `is_quiescent()` see different pools; that must not re-arm the latch.

        `is_quiescent()` probes with `ignore_timing=True`, so it sees cooling tokens `step()`
        cannot. A poison token awaiting a retry is therefore invisible to one and visible to
        the other, and a success-based latch had them alternate — clear, report, clear,
        report — for as long as healthy work kept flowing. `run()` polls quiescence every
        loop, so this became a callback storm from a single unchanged token.
        """
        net = PetriNet(max_workers=1)
        net.add_place(Place("in"))
        net.add_place(Place("out"))
        errors: list[Exception] = []
        net.on_error = lambda name, exc, tok: errors.append(exc)
        arc = InputArc("in", key=lambda t: 1 / 0 if t.payload["p"] < 0 else t.payload["p"])
        net.add_transition(Transition("t", inputs=[arc], outputs=[OutputArc("out")], action=lambda toks: toks))
        # One poison token scheduled far in the future (a retry/backoff shape).
        net.deposit("in", Token(payload={"p": -1}, available_at=time.monotonic() + 3600))

        for i in range(20):
            net.deposit("in", Token(payload={"p": i}))  # healthy work keeps flowing
            net.deposit("in", Token(payload={"p": i}))
            net._is_transition_enabled(net.transitions["t"])  # timed view: succeeds
            net._flush_selection_failures()
            net.is_quiescent()  # untimed view: sees the poison, fails

        assert len(errors) == 1, f"one unchanged poison token must report once, got {len(errors)}"
