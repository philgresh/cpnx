"""Tests for BindingPolicy — opt-in binding search that fixes head-of-line blocking."""

import time

from cpnx.engine import PetriNet
from cpnx.places import Place
from cpnx.tokens import Token
from cpnx.transitions import BindingPolicy, InputArc, OutputArc, Transition


def _fire_once(net):
    """Fire a single transition occurrence and block until its async action settles.

    Unlike ``run`` (which loops to quiescence and would fire every satisfying
    binding), this drives exactly one firing so single-occurrence semantics can
    be asserted.
    """
    assert net.step() is True
    deadline = time.monotonic() + 2.0
    while net._running_count > 0 and time.monotonic() < deadline:
        time.sleep(0.005)


class TestHeadOfLineFix:
    """Headline: FIRST fixes HoL blocking; LEGACY/default is unchanged."""

    def _build(self, policy):
        net = PetriNet(
            places=[Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=lambda toks: toks,
                    guard=lambda toks: toks[0].payload["sym"] == "MSFT",
                    binding_policy=policy,
                )
            ],
        )
        net.deposit("input", Token(payload={"sym": "AAPL"}))
        net.deposit("input", Token(payload={"sym": "MSFT"}))
        return net

    def test_first_fires_and_selects_deep_token(self):
        net = self._build(BindingPolicy.FIRST)
        net.run(deadline=time.monotonic() + 2.0)

        out = net.places["output"].tokens
        assert [t.payload["sym"] for t in out] == ["MSFT"]
        remaining = {t.payload["sym"] for t in net.places["input"].tokens}
        assert remaining == {"AAPL"}

    def test_legacy_does_not_fire_hol_blocked(self):
        net = self._build(BindingPolicy.LEGACY)
        net.run(deadline=time.monotonic() + 0.3)

        assert len(net.places["output"].tokens) == 0
        assert {t.payload["sym"] for t in net.places["input"].tokens} == {"AAPL", "MSFT"}

    def test_unset_policy_defaults_to_legacy(self):
        net = self._build(None)  # inherit net default (LEGACY)
        net.run(deadline=time.monotonic() + 0.3)

        assert len(net.places["output"].tokens) == 0
        assert {t.payload["sym"] for t in net.places["input"].tokens} == {"AAPL", "MSFT"}


class TestDeterminism:
    def test_insertion_order_first_binding_is_stable(self):
        """With multiple satisfying tokens, the earliest-inserted one is chosen, repeatably."""
        selected_ids = []
        for _ in range(5):
            net = PetriNet(
                places=[Place("input"), Place("output")],
                transitions=[
                    Transition(
                        name="t",
                        inputs=[InputArc("input")],
                        outputs=[OutputArc("output")],
                        action=lambda toks: toks,
                        guard=lambda toks: toks[0].payload["ok"] is True,
                        binding_policy=BindingPolicy.FIRST,
                    )
                ],
            )
            # first token fails; next two both satisfy — the earlier should win
            net.deposit("input", Token(payload={"ok": False, "n": 0}))
            net.deposit("input", Token(payload={"ok": True, "n": 1}))
            net.deposit("input", Token(payload={"ok": True, "n": 2}))
            _fire_once(net)
            out = net.places["output"].tokens
            assert len(out) == 1
            selected_ids.append(out[0].payload["n"])

        assert selected_ids == [1, 1, 1, 1, 1]


class TestGuardFreeFastPath:
    def test_first_no_guard_is_fifo_like_legacy(self):
        """FIRST without a guard behaves identically to LEGACY: first count, FIFO."""
        net = PetriNet(
            places=[Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=lambda toks: toks,
                    binding_policy=BindingPolicy.FIRST,
                )
            ],
        )
        net.deposit("input", Token(payload={"n": 1}))
        net.deposit("input", Token(payload={"n": 2}))

        _fire_once(net)  # consumes the head (n=1)
        assert [t.payload["n"] for t in net.places["output"].tokens] == [1]
        assert [t.payload["n"] for t in net.places["input"].tokens] == [2]


class TestCountAndMultiArc:
    def test_count_two_finds_satisfying_pair(self):
        """count=2 with a guard only some pairs satisfy — FIRST finds one."""
        net = PetriNet(
            places=[Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input", count=2)],
                    outputs=[OutputArc("output", count=2)],
                    action=lambda toks: toks,
                    # only a pair summing to exactly 10 is valid
                    guard=lambda toks: toks[0].payload["v"] + toks[1].payload["v"] == 10,
                    binding_policy=BindingPolicy.FIRST,
                )
            ],
        )
        for v in [1, 3, 7, 9]:  # only (1,9) and (3,7) sum to 10
            net.deposit("input", Token(payload={"v": v}))

        _fire_once(net)
        out_vs = sorted(t.payload["v"] for t in net.places["output"].tokens)
        assert len(out_vs) == 2
        assert sum(out_vs) == 10

    def test_multi_arc_deep_combination(self):
        """Two input places, guard relates one token from each; heads don't match but a
        deeper combination does. FIRST finds it; LEGACY does not."""

        def build(policy):
            net = PetriNet(
                places=[Place("a"), Place("b"), Place("output")],
                transitions=[
                    Transition(
                        name="t",
                        inputs=[InputArc("a"), InputArc("b")],
                        outputs=[OutputArc("output", count=2)],
                        action=lambda toks: toks,
                        guard=lambda toks: toks[0].payload["k"] == toks[1].payload["k"],
                        binding_policy=policy,
                    )
                ],
            )
            # heads: a=1, b=2 (mismatch). Deeper: a has 2, b has 1 → pair on k=2 or k=1
            net.deposit("a", Token(payload={"k": 1}))
            net.deposit("a", Token(payload={"k": 2}))
            net.deposit("b", Token(payload={"k": 2}))
            net.deposit("b", Token(payload={"k": 1}))
            return net

        first_net = build(BindingPolicy.FIRST)
        _fire_once(first_net)
        out = first_net.places["output"].tokens
        assert len(out) == 2
        assert out[0].payload["k"] == out[1].payload["k"]

        legacy_net = build(BindingPolicy.LEGACY)
        legacy_net.run(deadline=time.monotonic() + 0.3)
        assert len(legacy_net.places["output"].tokens) == 0


class TestSearchLimitExhaustion:
    def test_limit_exhaustion_disables_and_fires_callback(self):
        net = PetriNet(
            places=[Place("input"), Place("output")],
            binding_search_limit=5,
            transitions=[
                Transition(
                    name="never_matches",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=lambda toks: toks,
                    guard=lambda toks: toks[0].payload["sym"] == "ZZZZ",
                    binding_policy=BindingPolicy.FIRST,
                )
            ],
        )
        exhausted: list[str] = []
        net.on_binding_search_exhausted = exhausted.append  # record-only, no re-entry

        for i in range(20):
            net.deposit("input", Token(payload={"sym": f"S{i}"}))

        net.run(deadline=time.monotonic() + 0.3)

        assert len(net.places["output"].tokens) == 0
        assert len(net.places["input"].tokens) == 20
        assert "never_matches" in exhausted


class TestConsumptionCorrectness:
    def test_exact_selected_tokens_removed_by_id(self):
        net = PetriNet(
            places=[Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=lambda toks: toks,
                    guard=lambda toks: toks[0].payload["target"] is True,
                    binding_policy=BindingPolicy.FIRST,
                )
            ],
        )
        t0 = Token(payload={"target": False})
        chosen = Token(payload={"target": True})
        t2 = Token(payload={"target": False})
        for tok in (t0, chosen, t2):
            net.deposit("input", tok)

        _fire_once(net)

        out = net.places["output"].tokens
        assert len(out) == 1
        assert out[0].id == chosen.id
        remaining_ids = {t.id for t in net.places["input"].tokens}
        assert remaining_ids == {t0.id, t2.id}


class TestPolicyOverride:
    def test_net_first_transition_overrides_to_legacy(self):
        """Net default FIRST, transition overrides LEGACY → HoL-blocked (no fire)."""
        net = PetriNet(
            places=[Place("input"), Place("output")],
            binding_policy=BindingPolicy.FIRST,
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=lambda toks: toks,
                    guard=lambda toks: toks[0].payload["sym"] == "MSFT",
                    binding_policy=BindingPolicy.LEGACY,
                )
            ],
        )
        net.deposit("input", Token(payload={"sym": "AAPL"}))
        net.deposit("input", Token(payload={"sym": "MSFT"}))
        net.run(deadline=time.monotonic() + 0.3)

        assert len(net.places["output"].tokens) == 0
        assert len(net.places["input"].tokens) == 2

    def test_net_legacy_transition_overrides_to_first(self):
        """Net default LEGACY, transition overrides FIRST → searches and fires."""
        net = PetriNet(
            places=[Place("input"), Place("output")],
            binding_policy=BindingPolicy.LEGACY,
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=lambda toks: toks,
                    guard=lambda toks: toks[0].payload["sym"] == "MSFT",
                    binding_policy=BindingPolicy.FIRST,
                )
            ],
        )
        net.deposit("input", Token(payload={"sym": "AAPL"}))
        net.deposit("input", Token(payload={"sym": "MSFT"}))
        net.run(deadline=time.monotonic() + 2.0)

        assert [t.payload["sym"] for t in net.places["output"].tokens] == ["MSFT"]
        assert {t.payload["sym"] for t in net.places["input"].tokens} == {"AAPL"}


class TestLegacyParitySanity:
    def test_unset_net_guard_matches_head_fires_normally(self):
        """An unset-policy net with a guard matching the head fires once, normally."""
        fired: list[int] = []
        net = PetriNet(
            places=[Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=lambda toks: fired.append(len(toks)) or toks,
                    guard=lambda toks: toks[0].payload["sym"] == "MSFT",
                )
            ],
        )
        net.deposit("input", Token(payload={"sym": "MSFT"}))
        net.run(deadline=time.monotonic() + 2.0)

        assert fired == [1]
        assert [t.payload["sym"] for t in net.places["output"].tokens] == ["MSFT"]
        assert len(net.places["input"].tokens) == 0
