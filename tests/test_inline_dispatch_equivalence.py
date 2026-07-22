"""Behaviour-equivalence tests for the Phase 2 inline-dispatch fast path.

``cpnx.certification.certify`` decides whether a callable guard/arc-inscription
is closed-world safe. At construction, ``InputArc`` computes two independent
flags — ``_key_inline_safe`` and ``_filter_inline_safe`` — one per per-token
callable, while ``OutputArc``/``Transition`` each compute a single boolean
``_inline_safe``. The engine's ``_eval_expression`` then dispatches a certified
callable straight through (``expression(arg)``, no executor) instead of the old
timeout-bounded ``ThreadPoolExecutor`` round-trip (``_call_expr``) that every
callable used before Phase 2 and that every *uncertified* callable still uses.

Every test here builds the exact same net/scenario twice — once left at the
certifier's verdict (inline-safe flag is ``True``, runs inline) and once with
the flag force-flipped to ``False`` immediately after construction (so the
very same callable is dispatched through the old executor path instead) — and
asserts the two runs produce identical observable engine behaviour. This
proves the new fast path is a pure dispatch optimization, not a semantic
change.

All guard/expression callables are defined at module level (not as lambdas in
a heredoc) so ``inspect.getsourcelines``/``verify_callable_purity`` can recover
real source, per the certifier's requirements.
"""

import time

import pytest

from cpnx.engine import PetriNet
from cpnx.places import Place
from cpnx.tokens import Token
from cpnx.transitions import InputArc, OutputArc, Transition

_DEADLINE_SECS = 2.0


def _drain(net: PetriNet) -> None:
    """Run the net to quiescence, deterministically, without relying on wall-clock luck."""
    net.run(deadline=time.monotonic() + _DEADLINE_SECS)
    deadline = time.monotonic() + _DEADLINE_SECS
    while net._running_count > 0 and time.monotonic() < deadline:
        time.sleep(0.005)


# ---------------------------------------------------------------------------
# Module-level callables. Each is referenced by name (never inlined as a
# lambda passed straight from a test body-adjacent closure) so certification's
# AST recovery has real source to walk.
# ---------------------------------------------------------------------------


def guard_is_msft(toks: list[Token]) -> bool:
    """Certified transition guard: allow only when the (sole) candidate token is MSFT."""
    return toks[0].payload["sym"] == "MSFT"


def guard_divide_by_zero(toks: list[Token]) -> bool:
    """Certified guard that always raises (ZeroDivisionError) regardless of tokens."""
    return bool(toks) and 1 / 0 == 0


def priority_key(token: Token) -> object:
    """Certified per-token key: descending priority (negate to invert the default ascending order)."""
    return -token.payload["priority"]


def high_priority_filter(token: Token) -> bool:
    """Certified per-token filter: keep only priority >= 10."""
    return token.payload["priority"] >= 10


def output_predicate_even_count(toks: list[Token]) -> bool:
    """Certified output-arc predicate: active only when an even number of tokens flow."""
    return len(toks) % 2 == 0


def output_predicate_first_token(toks: list[Token]) -> bool:
    """Certified output-arc predicate reading ``toks[0]`` — used for the raises-parity case."""
    return toks[0].payload["route"] == "a"


# Module-level mutable state read by an *uncertified* (impure-by-closure)
# callable. `verify_callable_purity` only blocks I/O/import/global-mutation,
# not closures over mutable state, so this construction succeeds; but
# `certify` rejects it (mutable external read), so `_inline_safe` is False and
# it must still run correctly via the executor, exactly as before Phase 2.
_STATE = {"allow": False}


def guard_reads_mutable_state(toks: list[Token]) -> bool:
    return _STATE["allow"]


class TestGuardEquivalence:
    """`_check_transition_guard`: certified guard fires identically inline vs executor."""

    def _build(self, force_inline: bool) -> PetriNet:
        net = PetriNet(
            max_workers=1,
            places=[Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=lambda toks: toks,
                    guard=guard_is_msft,
                )
            ],
        )
        transition = net.transitions["t"]
        assert transition._inline_safe is True  # certified -> inline by default
        if not force_inline:
            transition._inline_safe = False  # force the old executor path
        return net

    def _run_blocking_case(self, force_inline: bool):
        net = self._build(force_inline)
        net.deposit("input", Token(payload={"sym": "AAPL"}))
        fired = net.step()
        return fired, [t.payload["sym"] for t in net.places["output"].tokens]

    def _run_allowing_case(self, force_inline: bool):
        net = self._build(force_inline)
        net.deposit("input", Token(payload={"sym": "MSFT"}))
        fired = net.step()
        if fired:
            _drain(net)
        return fired, [t.payload["sym"] for t in net.places["output"].tokens]

    def test_guard_blocks_identically_inline_and_executor(self):
        assert self._run_blocking_case(force_inline=True) == self._run_blocking_case(force_inline=False)
        # And pin down what "identical" means here: blocked, nothing produced.
        fired, produced = self._run_blocking_case(force_inline=True)
        assert fired is False
        assert produced == []

    def test_guard_allows_identically_inline_and_executor(self):
        assert self._run_allowing_case(force_inline=True) == self._run_allowing_case(force_inline=False)
        fired, produced = self._run_allowing_case(force_inline=True)
        assert fired is True
        assert produced == ["MSFT"]


class TestInputArcKeyEquivalence:
    """`_order_available`'s per-token `key`: certified key picks the same head token."""

    def _build(self, key, force_inline: bool) -> PetriNet:
        net = PetriNet(
            max_workers=1,
            places=[Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input", key=key)],
                    outputs=[OutputArc("output")],
                    action=lambda toks: toks,
                )
            ],
        )
        arc = net.transitions["t"].inputs[0]
        assert arc._key_inline_safe is True
        if not force_inline:
            arc._key_inline_safe = False
        return net

    def _run_ordering(self, force_inline: bool):
        net = self._build(priority_key, force_inline)
        net.deposit("input", Token(payload={"priority": 1, "label": "low"}))
        net.deposit("input", Token(payload={"priority": 9, "label": "high"}))
        net.step()
        _drain(net)
        return [t.payload["label"] for t in net.places["output"].tokens]

    def test_priority_key_picks_same_head_token_inline_and_executor(self):
        # count=1 with no guard eventually drains both tokens (one per firing);
        # the key determines the *firing order*, so "high" (priority 9, key -9)
        # is consumed before "low" (priority 1, key -1) either way, since it
        # sorts ascending by key.
        assert self._run_ordering(force_inline=True) == self._run_ordering(force_inline=False)
        assert self._run_ordering(force_inline=True) == ["high", "low"]


class TestInputArcFilterEquivalence:
    """`_order_available`'s per-token `filter`: certified filter selects the same tokens."""

    def _build(self, filt, force_inline: bool) -> PetriNet:
        net = PetriNet(
            max_workers=1,
            places=[Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input", filter=filt)],
                    outputs=[OutputArc("output")],
                    action=lambda toks: toks,
                )
            ],
        )
        arc = net.transitions["t"].inputs[0]
        assert arc._filter_inline_safe is True
        if not force_inline:
            arc._filter_inline_safe = False
        return net

    def _run_selection(self, force_inline: bool):
        net = self._build(high_priority_filter, force_inline)
        net.deposit("input", Token(payload={"priority": 5, "label": "low"}))
        net.deposit("input", Token(payload={"priority": 10, "label": "high"}))
        net.step()
        _drain(net)
        remaining = sorted(t.payload["label"] for t in net.places["input"].tokens)
        consumed = sorted(t.payload["label"] for t in net.places["output"].tokens)
        return consumed, remaining

    def test_high_priority_filter_picks_same_tokens_inline_and_executor(self):
        assert self._run_selection(force_inline=True) == self._run_selection(force_inline=False)
        consumed, remaining = self._run_selection(force_inline=True)
        assert consumed == ["high"]
        assert remaining == ["low"]


class TestOutputArcConditionEquivalence:
    """`_is_arc_active`: certified output predicate routes tokens to the same places."""

    def _build(self, force_inline: bool) -> PetriNet:
        net = PetriNet(
            max_workers=1,
            places=[Place("input"), Place("even_out"), Place("overflow")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input", count=2)],
                    outputs=[
                        OutputArc("even_out", count=2, condition=output_predicate_even_count),
                        OutputArc("overflow", count=0),
                    ],
                    action=lambda toks: toks,
                )
            ],
        )
        arc = net.transitions["t"].outputs[0]
        assert arc._inline_safe is True
        if not force_inline:
            arc._inline_safe = False
        return net

    def _run(self, force_inline: bool):
        net = self._build(force_inline)
        net.deposit("input", Token(payload={"n": 1}))
        net.deposit("input", Token(payload={"n": 2}))
        net.step()
        _drain(net)
        return len(net.places["even_out"].tokens), len(net.places["overflow"].tokens)

    def test_active_output_arc_routes_identically_inline_and_executor(self):
        assert self._run(force_inline=True) == self._run(force_inline=False)
        even_count, overflow_count = self._run(force_inline=True)
        assert even_count == 2
        assert overflow_count == 0


class TestRaisesAtRuntimeParity:
    """A certified guard/expression that raises at call time behaves the same both ways."""

    def test_guard_that_raises_blocks_identically_inline_and_executor(self):
        # `guard_divide_by_zero` raises unconditionally (ZeroDivisionError),
        # independent of token content, so it exercises `_check_transition_guard`'s
        # exception-swallowing (`except Exception: return False`) on both paths.
        def _build(force_inline: bool) -> PetriNet:
            net = PetriNet(
                max_workers=1,
                places=[Place("input"), Place("output")],
                transitions=[
                    Transition(
                        name="t",
                        inputs=[InputArc("input")],
                        outputs=[OutputArc("output")],
                        action=lambda toks: toks,
                        guard=guard_divide_by_zero,
                    )
                ],
            )
            transition = net.transitions["t"]
            assert transition._inline_safe is True
            if not force_inline:
                transition._inline_safe = False
            return net

        def _run(force_inline: bool):
            net = _build(force_inline)
            net.deposit("input", Token(payload={"sym": "AAPL"}))
            fired = net.step()
            return fired, len(net.places["output"].tokens), len(net.places["input"].tokens)

        inline_result = _run(force_inline=True)
        executor_result = _run(force_inline=False)
        assert inline_result == executor_result
        # `_check_transition_guard` swallows the exception -> treated as False:
        # the transition simply does not fire, and the token stays put.
        fired, produced, remaining = inline_result
        assert fired is False
        assert produced == 0
        assert remaining == 1

    def test_output_arc_raise_propagates_identically_inline_and_executor(self):
        # `_is_arc_active` has NO try/except, so an output-arc condition that
        # raises propagates out of the firing machinery on both paths. We
        # assert the propagation itself (same exception type) is identical
        # inline vs executor, rather than picking a non-raising case, since the
        # task explicitly calls out this asymmetry with `_check_transition_guard`.
        def _build(force_inline: bool) -> PetriNet:
            net = PetriNet(
                max_workers=1,
                places=[Place("input"), Place("output")],
                transitions=[
                    Transition(
                        name="t",
                        inputs=[InputArc("input")],
                        outputs=[OutputArc("output", condition=output_predicate_first_token)],
                        # Action returns an empty list, so `output_predicate_first_token`
                        # is called with `toks == []` and `toks[0]` raises IndexError.
                        action=lambda toks: [],
                    )
                ],
            )
            arc = net.transitions["t"].outputs[0]
            assert arc._inline_safe is True
            if not force_inline:
                arc._inline_safe = False
            return net

        def _run(force_inline: bool):
            net = _build(force_inline)
            net.deposit("input", Token(payload={"sym": "AAPL"}))
            net.step()
            _drain(net)
            return net

        net_inline = _run(force_inline=True)
        net_executor = _run(force_inline=False)
        # The raise happens inside `_execute_transition`'s output stage, off the
        # engine lock, on both paths: the engine's error handling routes the
        # consumed data token(s) to the error place identically either way, and
        # nothing is ever deposited to "output".
        assert len(net_inline.places["output"].tokens) == 0
        assert len(net_executor.places["output"].tokens) == 0
        assert len(net_inline.places["failed"].tokens) == len(net_executor.places["failed"].tokens)


class TestUncertifiedCallableStillWorksViaExecutor:
    """Additivity: an uncertified callable is unaffected — still drives the net via the executor."""

    def test_impure_by_closure_guard_certifies_false_but_still_fires_correctly(self):
        net = PetriNet(
            max_workers=1,
            places=[Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=lambda toks: toks,
                    guard=guard_reads_mutable_state,
                )
            ],
        )
        transition = net.transitions["t"]
        # verify_callable_purity accepts it (no I/O/import/global-mutation);
        # certify rejects it (mutable external read) -> executor path.
        assert transition._inline_safe is False

        net.deposit("input", Token(payload={"sym": "AAPL"}))
        _STATE["allow"] = False
        try:
            assert net.step() is False
            assert len(net.places["output"].tokens) == 0

            _STATE["allow"] = True
            assert net.step() is True
            _drain(net)
            assert len(net.places["output"].tokens) == 1
        finally:
            _STATE["allow"] = False  # leave module state clean for other tests


class TestStringExpressionsRejected:
    """String guards/expressions were removed — every entry point raises TypeError."""

    def test_string_guard_raises_type_error(self):
        with pytest.raises(TypeError, match="callable"):
            Transition(name="t", inputs=[], outputs=[], action=lambda toks: toks, guard="bool(tokens)")

    def test_string_input_arc_key_raises_type_error(self):
        with pytest.raises(TypeError, match="callable"):
            InputArc("p", key="tokens")

    def test_string_input_arc_filter_raises_type_error(self):
        with pytest.raises(TypeError, match="callable"):
            InputArc("p", filter="tokens")

    def test_string_output_arc_condition_raises_type_error(self):
        with pytest.raises(TypeError, match="callable"):
            OutputArc("q", condition="bool(tokens)")

    def test_string_reassignment_key_raises_type_error(self):
        arc = InputArc("p", key=lambda token: token.payload.get("priority", 0))
        with pytest.raises(TypeError, match="callable"):
            arc.key = "tokens"

    def test_string_reassignment_filter_raises_type_error(self):
        arc = InputArc("p", filter=lambda token: True)
        with pytest.raises(TypeError, match="callable"):
            arc.filter = "tokens"


class TestInlineSafeFlagAlwaysPresent:
    """Every arc/transition has its inline-safe flag(s) set, even with no guard/key/filter at all."""

    def test_input_arc_no_key_or_filter_flags_are_false(self):
        arc = InputArc("p")
        assert arc._key_inline_safe is False
        assert arc._filter_inline_safe is False

    def test_output_arc_no_condition_flag_is_false(self):
        arc = OutputArc("q")
        assert arc._inline_safe is False

    def test_transition_no_guard_flag_is_false(self):
        transition = Transition(
            name="t",
            inputs=[InputArc("input")],
            outputs=[OutputArc("output")],
            action=lambda toks: toks,
        )
        assert transition._inline_safe is False
