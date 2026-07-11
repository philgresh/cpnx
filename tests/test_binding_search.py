"""Tests for BindingPolicy — opt-in binding search that fixes head-of-line blocking."""

import time

from cpnx.engine import PetriNet
from cpnx.places import PacedResourcePlace, Place, ResourcePlace
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


class TestSearchIsBounded:
    """The search limit must bound both time and memory, even for count>=2 arcs.

    Regression for a bug where ``itertools.product`` eagerly materialized each arc's
    full ``C(n, count)`` combination list before the per-candidate limit could apply,
    making one enabling check O(C(n, count)) in time and memory.
    """

    def test_count_two_large_place_never_matching_is_fast(self):
        eval_count = {"n": 0}

        def guard(toks):
            eval_count["n"] += 1
            return False  # never satisfies → forces full search up to the limit

        net = PetriNet(
            places=[Place("input"), Place("output")],
            binding_search_limit=5,
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input", count=2)],
                    outputs=[OutputArc("output", count=2)],
                    action=lambda toks: toks,
                    guard=guard,
                    binding_policy=BindingPolicy.FIRST,
                )
            ],
        )
        exhausted: list[str] = []
        net.on_binding_search_exhausted = exhausted.append
        # C(2000, 2) ~ 2e6 combinations; an unbounded search would materialize them all.
        for i in range(2000):
            net.deposit("input", Token(payload={"i": i}))

        start = time.monotonic()
        # is_dead runs one enabling check per transition; that check must be bounded.
        assert net.is_dead() is True
        elapsed = time.monotonic() - start

        assert elapsed < 0.5  # bounded work, not O(C(2000, 2))
        # Guard evaluated at most limit+1 times, regardless of place size.
        assert eval_count["n"] <= net.binding_search_limit + 1
        assert "t" in exhausted


class TestResourcePlaceUnderFirst:
    """FIRST composes with ResourcePlace/PacedResourcePlace consume-by-id semantics."""

    def test_resource_permit_consumed_and_returned(self):
        net = PetriNet(
            places=[ResourcePlace("permits", capacity=2), Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="t",
                    # resource arc first so the data dimension varies fastest
                    inputs=[InputArc("permits"), InputArc("input")],
                    outputs=[OutputArc("permits"), OutputArc("output")],
                    action=lambda toks: toks,
                    guard=lambda toks: toks[1].payload["sym"] == "MSFT",
                    binding_policy=BindingPolicy.FIRST,
                )
            ],
        )
        net.deposit("input", Token(payload={"sym": "AAPL"}))
        net.deposit("input", Token(payload={"sym": "MSFT"}))
        net.run(deadline=time.monotonic() + 2.0)

        assert [t.payload["sym"] for t in net.places["output"].tokens] == ["MSFT"]
        assert {t.payload["sym"] for t in net.places["input"].tokens} == {"AAPL"}
        # Permit returned: pool back to full capacity.
        assert len(net.places["permits"].tokens) == 2

    def test_paced_resource_cooldown_respected(self):
        net = PetriNet(
            places=[
                PacedResourcePlace("permits", capacity=1, pacing_secs=5.0),
                Place("input"),
                Place("output"),
            ],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("permits"), InputArc("input")],
                    outputs=[OutputArc("permits"), OutputArc("output")],
                    action=lambda toks: toks,
                    binding_policy=BindingPolicy.FIRST,
                )
            ],
        )
        net.deposit("input", Token(payload={"n": 1}))
        net.deposit("input", Token(payload={"n": 2}))
        # Only one permit; after it fires and is returned it must cool down 5s,
        # so exactly one token should be processed within a short deadline.
        net.run(deadline=time.monotonic() + 0.5)

        assert len(net.places["output"].tokens) == 1
        assert len(net.places["input"].tokens) == 1


class TestExhaustionCallbackOffLock:
    """on_binding_search_exhausted fires outside the lock (may re-enter the net)."""

    def test_callback_may_deposit_without_deadlock(self):
        net = PetriNet(
            places=[Place("input"), Place("signal"), Place("output")],
            binding_search_limit=3,
            transitions=[
                Transition(
                    name="never",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=lambda toks: toks,
                    guard=lambda toks: toks[0].payload["sym"] == "NOPE",
                    binding_policy=BindingPolicy.FIRST,
                )
            ],
        )
        seen: list[str] = []

        def on_exhausted(name):
            seen.append(name)
            # Re-entering the net from the callback must not deadlock.
            net.deposit("signal", Token(payload={"name": name}))

        net.on_binding_search_exhausted = on_exhausted
        for i in range(10):
            net.deposit("input", Token(payload={"sym": f"S{i}"}))

        # A single enabling check that exhausts; must return and fire the callback.
        assert net.is_dead() is True
        assert "never" in seen
        assert len(net.places["signal"].tokens) >= 1


class TestSearchLimitValidation:
    def test_negative_limit_rejected_at_construction(self):
        import pytest

        for bad in (-2, -1, 0):
            with pytest.raises(ValueError, match="binding_search_limit must be >= 1"):
                PetriNet(binding_search_limit=bad)

    def test_limit_one_is_head_only_search(self):
        """limit=1 tries exactly the head binding; deeper matches exhaust immediately."""
        net = PetriNet(
            places=[Place("input"), Place("output")],
            binding_search_limit=1,
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
        exhausted: list[str] = []
        net.on_binding_search_exhausted = exhausted.append
        net.deposit("input", Token(payload={"sym": "AAPL"}))  # head fails
        net.deposit("input", Token(payload={"sym": "MSFT"}))  # match beyond limit
        net.run(deadline=time.monotonic() + 0.3)

        assert len(net.places["output"].tokens) == 0
        assert "t" in exhausted


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


def _single_select_net(policy, *, seed=None, n=6, guard=None, priority_key=None):
    """Build a net whose one transition selects a single token from `n` candidates."""
    net = PetriNet(
        seed=seed,
        places=[Place("input"), Place("output")],
        transitions=[
            Transition(
                name="t",
                inputs=[InputArc("input")],
                outputs=[OutputArc("output")],
                action=lambda toks: toks,
                guard=guard,
                binding_policy=policy,
                binding_priority_key=priority_key,
            )
        ],
    )
    for i in range(n):
        net.deposit("input", Token(payload={"i": i}))
    return net


def _selected_index(net):
    _fire_once(net)
    out = net.places["output"].tokens
    assert len(out) == 1
    return out[0].payload["i"]


class TestRandomPolicy:
    def test_same_seed_reproduces_selection(self):
        a = _selected_index(_single_select_net(BindingPolicy.RANDOM, seed=1234, n=8))
        b = _selected_index(_single_select_net(BindingPolicy.RANDOM, seed=1234, n=8))
        assert a == b

    def test_varying_seed_spreads_selection(self):
        """RANDOM (guard-free) selects across the token set, not just the head (index 0)."""
        picks = {_selected_index(_single_select_net(BindingPolicy.RANDOM, seed=s, n=8)) for s in range(40)}
        assert len(picks) > 1  # not degenerate to a single index
        assert picks != {0}  # not always the head

    def test_guard_free_random_is_not_head_only(self):
        """A guard-free RANDOM transition must enumerate, not take the FIRST head fast path."""
        picks = {_selected_index(_single_select_net(BindingPolicy.RANDOM, seed=s, n=6)) for s in range(30)}
        assert picks - {0}  # at least one non-head selection observed

    def test_random_reproducibility_is_probe_independent(self):
        """Interleaving is_dead()/is_quiescent() probes must not perturb the seeded RNG."""
        clean = _single_select_net(BindingPolicy.RANDOM, seed=99, n=8)
        probed = _single_select_net(BindingPolicy.RANDOM, seed=99, n=8)
        for _ in range(5):
            probed.is_dead()
            probed.is_quiescent()
        assert _selected_index(clean) == _selected_index(probed)

    def test_random_with_guard_only_samples_satisfying(self):
        net = _single_select_net(BindingPolicy.RANDOM, seed=7, n=10, guard=lambda toks: toks[0].payload["i"] % 2 == 0)
        picks = set()
        for s in range(30):
            n2 = _single_select_net(
                BindingPolicy.RANDOM, seed=s, n=10, guard=lambda toks: toks[0].payload["i"] % 2 == 0
            )
            picks.add(_selected_index(n2))
        assert picks and all(i % 2 == 0 for i in picks)  # only even indices ever chosen
        assert _selected_index(net) % 2 == 0


class TestPriorityPolicy:
    def test_default_key_selects_oldest(self):
        """Default PRIORITY key is oldest-first by created_at; the earliest token wins."""
        net = _single_select_net(BindingPolicy.PRIORITY, n=5)
        # tokens deposited i=0..4 in order → i=0 has the smallest created_at
        assert _selected_index(net) == 0

    def test_custom_key_selects_min_rank(self):
        net = PetriNet(
            places=[Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=lambda toks: toks,
                    binding_policy=BindingPolicy.PRIORITY,
                    binding_priority_key=lambda toks: toks[0].payload["rank"],
                )
            ],
        )
        for i, rank in enumerate([5, 2, 9, 2, 7]):  # min rank 2 first appears at index 1
            net.deposit("input", Token(payload={"i": i, "rank": rank}))
        assert _selected_index(net) == 1  # ties (rank 2 at idx 1 and 3) → insertion order

    def test_priority_is_deterministic(self):
        a = _selected_index(_single_select_net(BindingPolicy.PRIORITY, n=6))
        b = _selected_index(_single_select_net(BindingPolicy.PRIORITY, n=6))
        assert a == b == 0


class TestWholeNetSeed:
    def test_scheduler_tiebreak_is_seeded(self):
        """Two equal-priority transitions competing for the same token: seeded choice replays."""

        def build(seed):
            net = PetriNet(
                seed=seed,
                places=[Place("input"), Place("a"), Place("b")],
                transitions=[
                    Transition(name="ta", inputs=[InputArc("input")], outputs=[OutputArc("a")], action=lambda t: t),
                    Transition(name="tb", inputs=[InputArc("input")], outputs=[OutputArc("b")], action=lambda t: t),
                ],
            )
            net.deposit("input", Token(payload={"x": 1}))
            return net

        n1, n2 = build(2024), build(2024)
        _fire_once(n1)
        _fire_once(n2)
        winner1 = "a" if n1.places["a"].tokens else "b"
        winner2 = "a" if n2.places["a"].tokens else "b"
        assert winner1 == winner2


class TestCountAndMultiArcSearchPolicies:
    def test_priority_count_two_min_pair(self):
        net = PetriNet(
            places=[Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input", count=2)],
                    outputs=[OutputArc("output", count=2)],
                    action=lambda toks: toks,
                    binding_policy=BindingPolicy.PRIORITY,
                    binding_priority_key=lambda toks: sum(t.payload["v"] for t in toks),
                )
            ],
        )
        for v in [4, 1, 9, 2]:  # min-sum pair is (1,2) = 3
            net.deposit("input", Token(payload={"v": v}))
        _fire_once(net)
        out = sorted(t.payload["v"] for t in net.places["output"].tokens)
        assert out == [1, 2]

    def test_random_count_two_reproducible(self):
        def build(seed):
            net = PetriNet(
                seed=seed,
                places=[Place("input"), Place("output")],
                transitions=[
                    Transition(
                        name="t",
                        inputs=[InputArc("input", count=2)],
                        outputs=[OutputArc("output", count=2)],
                        action=lambda toks: toks,
                        binding_policy=BindingPolicy.RANDOM,
                    )
                ],
            )
            for v in range(6):
                net.deposit("input", Token(payload={"v": v}))
            return net

        n1, n2 = build(555), build(555)
        _fire_once(n1)
        _fire_once(n2)
        assert sorted(t.payload["v"] for t in n1.places["output"].tokens) == sorted(
            t.payload["v"] for t in n2.places["output"].tokens
        )


class TestSearchPolicyExhaustion:
    def test_random_truncated_prefix_still_selects_and_signals(self):
        """When candidates exceed the limit, RANDOM selects from the prefix AND signals."""
        net = PetriNet(
            seed=1,
            binding_search_limit=5,
            places=[Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=lambda toks: toks,
                    binding_policy=BindingPolicy.RANDOM,
                )
            ],
        )
        exhausted: list[str] = []
        net.on_binding_search_exhausted = exhausted.append
        for i in range(50):
            net.deposit("input", Token(payload={"i": i}))
        idx = _selected_index(net)
        assert idx < 5  # only the first `limit` candidates were in the sample space
        assert "t" in exhausted  # truncation signalled even though a binding fired
