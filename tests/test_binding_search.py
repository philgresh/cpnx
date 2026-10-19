"""Tests for BindingPolicy — the binding search that fixes head-of-line blocking."""

import inspect
import os
import time
import warnings

import pytest

from cpnx.engine import PetriNet
from cpnx.places import PacedResourcePlace, Place, ResourcePlace
from cpnx.tokens import Token
from cpnx.transitions import (
    _PACKAGE_ROOT,
    BindingPolicy,
    CpnxDeprecationWarning,
    InputArc,
    OutputArc,
    Transition,
)

#: Selecting the deprecated `LEGACY` policy warns by design. Tests that exercise what it
#: *does* silence that here; the warning itself is asserted in `TestLegacyDeprecationWarning`.
_SILENCE_LEGACY = pytest.mark.filterwarnings("ignore::cpnx.CpnxDeprecationWarning")


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
    """Headline: FIRST fixes HoL blocking, and it is the default an unset transition inherits."""

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

    def test_unset_policy_defaults_to_first(self):
        """An unset transition inherits the net default, which is FIRST — so HoL is fixed."""
        net = self._build(None)  # inherit net default (FIRST)
        net.run(deadline=time.monotonic() + 2.0)

        out = net.places["output"].tokens
        assert [t.payload["sym"] for t in out] == ["MSFT"]
        assert {t.payload["sym"] for t in net.places["input"].tokens} == {"AAPL"}

    def test_net_default_is_first(self):
        """The net-wide default is FIRST, exposed on the `binding_policy` property."""
        assert PetriNet().binding_policy is BindingPolicy.FIRST

    @_SILENCE_LEGACY
    def test_explicit_legacy_still_opts_out_of_the_fix(self):
        """`LEGACY` survives one release as the opt-out, and still blocks head-of-line.

        ADR 0001 step 2 asked for the default flip and the removal to land a deprecation
        cycle apart, so 0.6.0 flips the default to `FIRST` while `LEGACY` keeps its exact
        pre-0.6.0 semantics for anyone who needs a release to migrate. Removal is Phase 4.
        """
        net = self._build(BindingPolicy.LEGACY)
        net.run(deadline=time.monotonic() + 0.3)

        assert len(net.places["output"].tokens) == 0
        assert {t.payload["sym"] for t in net.places["input"].tokens} == {"AAPL", "MSFT"}

    @_SILENCE_LEGACY
    def test_net_wide_legacy_opt_out_covers_unset_transitions(self):
        """The *net-level* opt-out works — the form the migration notes actually recommend.

        `PetriNet(binding_policy=LEGACY)` with transitions left on `binding_policy=None` is
        how an existing net defers the flip in one line, so it is pinned separately from the
        per-transition opt-out above: the guarded transition inherits `LEGACY` and stays
        head-only.
        """
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
                    # No per-transition policy: inherits the net-wide LEGACY opt-out.
                )
            ],
        )
        for sym in ("AAPL", "MSFT"):
            net.deposit("input", Token(payload={"sym": sym}))
        net.run(deadline=time.monotonic() + 0.3)

        assert len(net.places["output"].tokens) == 0
        assert {t.payload["sym"] for t in net.places["input"].tokens} == {"AAPL", "MSFT"}

    @_SILENCE_LEGACY
    def test_transition_can_opt_back_in_under_a_legacy_net(self):
        """A transition may take `FIRST` back even while the net default is the LEGACY opt-out."""
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
        for sym in ("AAPL", "MSFT"):
            net.deposit("input", Token(payload={"sym": sym}))
        net.run(deadline=time.monotonic() + 2.0)

        assert [t.payload["sym"] for t in net.places["output"].tokens] == ["MSFT"]
        assert {t.payload["sym"] for t in net.places["input"].tokens} == {"AAPL"}


class TestLegacyDeprecationWarning:
    """`LEGACY` warns at run time wherever it can enter the system.

    The 0.5.0 deprecation was documentation-only — no runtime signal at all — so these pin
    that every entry point now warns, and that the warning cannot be swallowed by Python's
    default filters.
    """

    def _transition(self, policy):
        return Transition(
            name="t",
            inputs=[InputArc("input")],
            outputs=[OutputArc("output")],
            action=lambda toks: toks,
            binding_policy=policy,
        )

    def test_net_constructor_warns(self):
        with pytest.warns(CpnxDeprecationWarning, match="removed in 0.7.0"):
            PetriNet(binding_policy=BindingPolicy.LEGACY)

    def test_transition_constructor_warns(self):
        with pytest.warns(CpnxDeprecationWarning, match="removed in 0.7.0"):
            self._transition(BindingPolicy.LEGACY)

    def test_net_policy_setter_warns(self):
        net = PetriNet()
        with pytest.warns(CpnxDeprecationWarning):
            net.binding_policy = BindingPolicy.LEGACY
        assert net.binding_policy is BindingPolicy.LEGACY  # the assignment still takes effect

    def test_transition_reassignment_warns(self):
        transition = self._transition(BindingPolicy.FIRST)
        with pytest.warns(CpnxDeprecationWarning):
            transition.binding_policy = BindingPolicy.LEGACY
        assert transition.binding_policy is BindingPolicy.LEGACY

    def test_warning_names_the_offending_transition(self):
        """The message carries the transition name, so a large net says which one to fix."""
        with pytest.warns(CpnxDeprecationWarning, match=r"Transition\(name='t'\)"):
            self._transition(BindingPolicy.LEGACY)

    @pytest.mark.parametrize("policy", [None, BindingPolicy.FIRST, BindingPolicy.RANDOM, BindingPolicy.PRIORITY])
    def test_undeprecated_policies_are_silent(self, policy):
        """Only LEGACY warns — above all the default, or every net would warn."""
        with warnings.catch_warnings():
            warnings.simplefilter("error", CpnxDeprecationWarning)
            self._transition(policy)
            if policy is not None:
                PetriNet(binding_policy=policy)
            else:
                PetriNet()

    @pytest.mark.parametrize(
        "select",
        [
            pytest.param(lambda t: PetriNet(binding_policy=BindingPolicy.LEGACY), id="net-constructor"),
            pytest.param(lambda t: t(BindingPolicy.LEGACY), id="transition-constructor"),
            pytest.param(lambda t: setattr(PetriNet(), "binding_policy", BindingPolicy.LEGACY), id="net-setter"),
            pytest.param(
                lambda t: setattr(t(BindingPolicy.FIRST), "binding_policy", BindingPolicy.LEGACY),
                id="transition-reassignment",
            ),
        ],
    )
    def test_warning_points_at_the_caller(self, select):
        """Every entry point blames the caller's own line, not a frame inside cpnx.

        The four paths sit at different stack depths — the two constructors go through an
        extra frame (`PetriNet.__init__`, or the `@dataclass`-generated `Transition.__init__`,
        which reports as `<string>`) while the two assignments do not. A warning naming
        `engine.py` or `<string>` tells the user nothing about which line of *their* net to
        fix. This covers all four entry points by file; the exact *line* is pinned by
        `test_construction_names_the_exact_line` / `test_reassignment_names_the_exact_line`,
        which call inline — here the innermost user frame is the parametrised lambda, so the
        expected line would have to be derived per-param rather than asserted directly.
        """
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            select(self._transition)

        hits = [w for w in caught if issubclass(w.category, CpnxDeprecationWarning)]
        assert len(hits) == 1
        assert hits[0].filename == __file__

    def test_construction_names_the_exact_line(self):
        """Construction reports the caller's line, not the generated `__init__`'s.

        Pinning the line matters because filename alone passes with a residual off-by-one:
        the frames in between live in this file too.
        """
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            expected_lineno = inspect.currentframe().f_lineno + 1
            PetriNet(binding_policy=BindingPolicy.LEGACY)

        hit = next(w for w in caught if issubclass(w.category, CpnxDeprecationWarning))
        assert (hit.filename, hit.lineno) == (__file__, expected_lineno)

    def test_reassignment_names_the_exact_line(self):
        """Post-construction reassignment reports the caller's line, one frame shallower."""
        net = PetriNet()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            expected_lineno = inspect.currentframe().f_lineno + 1
            net.binding_policy = BindingPolicy.LEGACY

        hit = next(w for w in caught if issubclass(w.category, CpnxDeprecationWarning))
        assert (hit.filename, hit.lineno) == (__file__, expected_lineno)

    def test_warning_reaches_synthetic_caller_frames(self):
        """User code with no real file — a REPL, a notebook cell, `exec` — still gets a line.

        The frame walk has to step over the `@dataclass`-generated `__init__`, which also has
        a synthetic filename. Rejecting *every* synthetic frame overshot the whole stack and
        made `warnings` report `sys:1` — no file and no line at all — for anyone driving a net
        from `python -c`, a REPL or a notebook. Only the generated `__init__` is skipped now.
        """
        source = "PetriNet(binding_policy=BindingPolicy.LEGACY)"
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            exec(  # noqa: S102 - exercising attribution for exec'd user code is the point
                compile(source, "<string>", "exec"),
                {"PetriNet": PetriNet, "BindingPolicy": BindingPolicy},
            )

        hits = [w for w in caught if issubclass(w.category, CpnxDeprecationWarning)]
        assert len(hits) == 1
        assert hits[0].filename == "<string>"  # the caller's synthetic frame, not `sys`
        assert hits[0].lineno == 1

    def test_package_root_cannot_swallow_sibling_paths(self):
        """The cpnx-frame test is separator-terminated, so `cpnx_user/` is not read as cpnx.

        A bare `startswith` on the package directory also matched any sibling whose name
        merely begins with it, so a user net in `.../cpnx_user/m.py` was walked straight past
        and the warning landed on *its* caller instead.
        """
        assert _PACKAGE_ROOT.endswith(os.sep)
        sibling = f"{_PACKAGE_ROOT.rstrip(os.sep)}_user{os.sep}m.py"
        assert not sibling.startswith(_PACKAGE_ROOT)

    def test_category_is_visible_under_default_filters(self):
        """Not a `DeprecationWarning` subclass, so `ignore::DeprecationWarning` cannot hide it.

        Python's default filters ignore `DeprecationWarning` outside `__main__`, which would
        make this invisible to exactly the library users it must reach — repeating 0.5.0's
        silent deprecation. Pinned because it is a deliberate choice, not an oversight.
        """
        assert issubclass(CpnxDeprecationWarning, UserWarning)
        assert not issubclass(CpnxDeprecationWarning, DeprecationWarning)


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
    def test_first_no_guard_is_fifo_head_only(self):
        """FIRST without a guard resolves head-only: the first `count` tokens, FIFO."""
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

    @_SILENCE_LEGACY
    def test_multi_arc_deep_combination(self):
        """Two input places, guard relates one token from each; heads don't match but a
        deeper combination does. FIRST finds it; the LEGACY opt-out does not."""

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
    """A transition's own `binding_policy` wins over the net default, in either direction.

    Both cases use a guard-free transition over the same three tokens, deposited so that the
    head is *not* the minimum-`rank` token. Guard-free `FIRST` resolves head-only, while
    `PRIORITY` enumerates and takes the minimum key — so which token is consumed names the
    policy actually in force.
    """

    def _build(self, net_policy, transition_policy):
        net = PetriNet(
            places=[Place("input"), Place("output")],
            binding_policy=net_policy,
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=lambda toks: toks,
                    binding_policy=transition_policy,
                    binding_priority_key=lambda toks: toks[0].payload["rank"],
                )
            ],
        )
        for rank in (2, 0, 1):  # head is rank 2; the minimum key is the second deposit
            net.deposit("input", Token(payload={"rank": rank}))
        return net

    def test_net_first_transition_overrides_to_priority(self):
        """Net default FIRST, transition overrides PRIORITY → min-key token, not the head."""
        net = self._build(BindingPolicy.FIRST, BindingPolicy.PRIORITY)
        _fire_once(net)

        assert [t.payload["rank"] for t in net.places["output"].tokens] == [0]
        assert {t.payload["rank"] for t in net.places["input"].tokens} == {1, 2}

    def test_net_priority_transition_overrides_to_first(self):
        """Net default PRIORITY, transition overrides FIRST → head-only, ignoring the key."""
        net = self._build(BindingPolicy.PRIORITY, BindingPolicy.FIRST)
        _fire_once(net)

        assert [t.payload["rank"] for t in net.places["output"].tokens] == [2]
        assert {t.payload["rank"] for t in net.places["input"].tokens} == {0, 1}


class TestGuardedPolicyOverride:
    """The override also holds for a **guarded** transition — the shape the v0.6.0 flip moved.

    A guarded transition never takes the head-only fast path, so both arms search. The guard
    admits only `ok` tokens, and the `ok` ones are deposited out of rank order, so `FIRST`
    (first satisfying in insertion order → rank 3) and `PRIORITY` (minimum key *among the
    satisfying* bindings → rank 1) pick different tokens. The lowest rank overall sits on a
    guard-failing token, so these also pin that the guard filters *before* the key ranks.
    """

    def _build(self, net_policy, transition_policy):
        net = PetriNet(
            places=[Place("input"), Place("output")],
            binding_policy=net_policy,
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=lambda toks: toks,
                    guard=lambda toks: toks[0].payload["ok"] is True,
                    binding_policy=transition_policy,
                    binding_priority_key=lambda toks: toks[0].payload["rank"],
                )
            ],
        )
        # rank 0 is the global minimum but fails the guard; the satisfying ranks are 3 then 1.
        for ok, rank in ((False, 0), (True, 3), (True, 1), (False, 2)):
            net.deposit("input", Token(payload={"ok": ok, "rank": rank}))
        return net

    def test_guarded_net_priority_transition_overrides_to_first(self):
        """Net default PRIORITY, guarded transition overrides FIRST → first satisfying."""
        net = self._build(BindingPolicy.PRIORITY, BindingPolicy.FIRST)
        _fire_once(net)

        assert [t.payload["rank"] for t in net.places["output"].tokens] == [3]
        assert {t.payload["rank"] for t in net.places["input"].tokens} == {0, 1, 2}

    def test_guarded_net_first_transition_overrides_to_priority(self):
        """Net default FIRST, guarded transition overrides PRIORITY → min key among satisfying."""
        net = self._build(BindingPolicy.FIRST, BindingPolicy.PRIORITY)
        _fire_once(net)

        assert [t.payload["rank"] for t in net.places["output"].tokens] == [1]
        assert {t.payload["rank"] for t in net.places["input"].tokens} == {0, 2, 3}

    def test_guarded_unset_transition_inherits_net_policy(self):
        """A guarded transition with no policy of its own follows the net default (FIRST)."""
        net = self._build(BindingPolicy.FIRST, None)
        _fire_once(net)

        assert [t.payload["rank"] for t in net.places["output"].tokens] == [3]


class TestHeadMatchSanity:
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


class TestPriorityKeyErrorHandling:
    def test_non_callable_key_rejected_at_construction(self):
        with pytest.raises(TypeError, match="binding_priority_key must be a callable or None"):
            Transition(
                name="t",
                inputs=[InputArc("input")],
                outputs=[OutputArc("output")],
                action=lambda toks: toks,
                binding_policy=BindingPolicy.PRIORITY,
                binding_priority_key="tokens[0].payload['rank']",  # string not supported
            )

    def test_key_raising_for_every_candidate_falls_back_not_livelock(self):
        """A key that raises for all candidates must fire the first satisfying binding,
        keeping the probe (enabled) and firing paths consistent — no silent livelock."""

        def boom(toks):
            raise ValueError("key blows up")

        net = PetriNet(
            places=[Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=lambda toks: toks,
                    binding_policy=BindingPolicy.PRIORITY,
                    binding_priority_key=boom,
                )
            ],
        )
        for i in range(3):
            net.deposit("input", Token(payload={"i": i}))

        assert net.is_dead() is False  # probe says enabled
        _fire_once(net)  # ...and it actually fires (fallback to first satisfying binding)
        assert [t.payload["i"] for t in net.places["output"].tokens] == [0]
        # drains to quiescence without spinning
        net.run(deadline=time.monotonic() + 2.0)
        assert net.is_quiescent() is True
        assert len(net.places["input"].tokens) == 0

    def test_incomparable_keys_do_not_crash_step(self):
        """A key returning values that aren't mutually comparable skips the bad compare
        rather than raising out of step()/run()."""
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
        for rank in [1, None, 2, None]:  # None vs int is not orderable
            net.deposit("input", Token(payload={"rank": rank}))

        # Must not raise; drains everything.
        net.run(deadline=time.monotonic() + 2.0)
        assert net.is_quiescent() is True
        assert len(net.places["output"].tokens) == 4


class TestSearchPoliciesWithResourcePlace:
    def _build(self, policy, *, seed=None, n_data=6, capacity=3):
        net = PetriNet(
            seed=seed,
            places=[ResourcePlace("permits", capacity=capacity), Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("permits"), InputArc("input")],  # resource arc first
                    outputs=[OutputArc("permits"), OutputArc("output")],
                    action=lambda toks: toks,
                    binding_policy=policy,
                )
            ],
        )
        for i in range(n_data):
            net.deposit("input", Token(payload={"i": i}))
        return net

    def test_random_with_permit_arc_is_reproducible(self):
        a = self._build(BindingPolicy.RANDOM, seed=321)
        b = self._build(BindingPolicy.RANDOM, seed=321)
        _fire_once(a)
        _fire_once(b)
        ai = [t.payload["i"] for t in a.places["output"].tokens]
        bi = [t.payload["i"] for t in b.places["output"].tokens]
        assert ai == bi and len(ai) == 1
        assert len(a.places["permits"].tokens) == 3  # permit returned

    def test_random_permit_inflated_space_truncates_and_signals(self):
        """A permit arc multiplies the candidate space; over the limit RANDOM still fires
        and signals truncation."""
        net = PetriNet(
            seed=5,
            binding_search_limit=4,
            places=[ResourcePlace("permits", capacity=3), Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("permits"), InputArc("input")],
                    outputs=[OutputArc("permits"), OutputArc("output")],
                    action=lambda toks: toks,
                    binding_policy=BindingPolicy.RANDOM,
                )
            ],
        )
        exhausted: list[str] = []
        net.on_binding_search_exhausted = exhausted.append
        for i in range(20):  # 3 permits x 20 data = 60 candidates >> limit 4
            net.deposit("input", Token(payload={"i": i}))
        _fire_once(net)
        assert len(net.places["output"].tokens) == 1
        assert "t" in exhausted


class TestPriorityDefaultKeyWithResources:
    def test_default_key_ignores_ancient_permit_token(self):
        """The default oldest-first key must select the oldest DATA token even when an
        ancient resource permit is part of the binding (regression: the permit's tiny
        created_at otherwise dominates min() and collapses selection to insertion order)."""
        net = PetriNet(
            places=[ResourcePlace("permits", capacity=2), Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("permits"), InputArc("input")],
                    outputs=[OutputArc("permits"), OutputArc("output")],
                    action=lambda toks: toks,
                    binding_policy=BindingPolicy.PRIORITY,
                )
            ],
        )
        permit_ts = net.places["permits"].tokens[0].created_at
        # Both data tokens are newer than the permit; insert the newer one FIRST so
        # insertion-order != oldest-first. Oldest data = "older".
        net.deposit("input", Token(payload={"i": "newer"}, created_at=permit_ts + 100.0))
        net.deposit("input", Token(payload={"i": "older"}, created_at=permit_ts + 50.0))

        binding = net._resolve_binding(net.transitions["t"], net._get_model_time_under_lock())
        chosen = [t.payload["i"] for _, toks in binding for t in toks if not t.is_resource]
        assert chosen == ["older"]

    def test_resource_only_binding_does_not_crash(self):
        """A binding with only resource tokens (no data) still resolves (fallback to all tokens)."""
        net = PetriNet(
            places=[ResourcePlace("permits", capacity=2), Place("output")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("permits")],
                    outputs=[OutputArc("permits"), OutputArc("output")],
                    action=lambda toks: toks,
                    binding_policy=BindingPolicy.PRIORITY,
                )
            ],
        )
        binding = net._resolve_binding(net.transitions["t"], net._get_model_time_under_lock())
        assert binding is not None  # resolves without raising on the resource-only binding


class TestPriorityKeyFailureDiagnostic:
    def test_all_candidates_failing_key_signals_on_error(self):
        """A key that raises for every candidate must fire on_error (off-lock, token=None) —
        the PRIORITY fallback stays silent otherwise."""
        errors: list[tuple[str, str, object]] = []

        net = PetriNet(
            places=[Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="boom",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=lambda toks: toks,
                    binding_policy=BindingPolicy.PRIORITY,
                    binding_priority_key=lambda toks: toks[0].payload["missing"],  # KeyError
                )
            ],
        )
        # on_error may safely re-enter the net (dispatched off the engine lock).
        net.on_error = lambda name, exc, tok: errors.append((name, type(exc).__name__, tok))
        for i in range(2):
            net.deposit("input", Token(payload={"i": i}))

        net.run(deadline=time.monotonic() + 1.0)

        assert errors, "on_error should fire when the priority key fails for every candidate"
        name, exc_type, tok = errors[0]
        assert name == "boom"
        assert exc_type == "RuntimeError"  # descriptive wrapper
        assert tok is None
        # Fallback still fired the transition (no livelock): input drains.
        assert net.is_quiescent() is True
        assert len(net.places["output"].tokens) == 2

    def test_working_key_does_not_signal_on_error(self):
        errors: list = []
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
        net.on_error = lambda *a: errors.append(a)
        for rank in [3, 1, 2]:
            net.deposit("input", Token(payload={"rank": rank}))
        net.run(deadline=time.monotonic() + 1.0)
        assert errors == []  # a working key never triggers the diagnostic


class TestGuardedSearchPolicyStall:
    def test_random_stalls_when_no_satisfier_in_prefix(self):
        """A guarded RANDOM whose only satisfying binding lies beyond the limit stalls like
        FIRST (documents that RANDOM/PRIORITY do NOT universally avoid the over-limit stall)."""
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
                    guard=lambda toks: toks[0].payload["i"] >= 100,
                    binding_policy=BindingPolicy.RANDOM,
                )
            ],
        )
        exhausted: list[str] = []
        net.on_binding_search_exhausted = exhausted.append
        for i in range(5):  # fail guard, fill the prefix
            net.deposit("input", Token(payload={"i": i}))
        net.deposit("input", Token(payload={"i": 100}))  # satisfies, but beyond the limit

        assert net.is_quiescent() is True  # stalls, exactly like FIRST
        net.run(deadline=time.monotonic() + 0.3)
        assert len(net.places["output"].tokens) == 0
        assert any(t.payload["i"] == 100 for t in net.places["input"].tokens)
        assert "t" in exhausted
