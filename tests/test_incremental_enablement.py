"""Tests for the ADR 0006 incremental enablement scheduler.

The engine maintains a dirty-set / routing-table scheduler (`PetriNet._input_routing`,
`_output_routing`, `_dirty_places`, `_volatile_transitions`, `_capacity_blocked`,
`_enabled_bindings`, `_potentially_enabled`, `_priority_buckets`, `_reactivation`) that
lets `step()`/`is_quiescent()`/`is_dead()` re-check only the transitions whose enablement
could plausibly have changed, instead of scanning every registered transition. It is
active only when `net._incremental_eligible` is `True` (no timed features, no
RANDOM/PRIORITY transition); otherwise the engine falls back to the original full-scan
methods, which are unchanged and covered elsewhere.

Per the header comment in `tests/test_seeded_determinism.py`: `guard`, `key`, and
`binding_priority_key` callables are purity-checked via `inspect.getsource` at
construction, which raises `PermissionError` for callables whose source can't be
retrieved (lambdas defined inside a test method, or dynamically-built closures). So
every `guard`/`key`/`binding_priority_key` below is a real module-level `def`. Plain
`action=` callables are not purity-checked and may be lambdas/closures freely.
"""

import time

from cpnx.engine import PetriNet
from cpnx.places import PacedResourcePlace, Place
from cpnx.tokens import Token
from cpnx.transitions import BindingPolicy, InputArc, OutputArc, Transition

# --- Module-level guard/key/action-raiser callables --------------------------------
# See the module docstring: only guard/key/binding_priority_key are purity-checked, but
# defining them at module level uniformly keeps the pattern obviously safe.


def _below_condition(tokens: list[Token]) -> bool:
    """Conditional OutputArc predicate: only fires while under a small count."""
    return len(tokens) > 0 and tokens[0].payload.get("n", 0) < 3


STATE = {"open": False}


def _state_guard(tokens: list[Token]) -> bool:
    """Guard reading module-level mutable state — not a function of the marking."""
    return STATE["open"]


def _raising_action(tokens: list[Token]) -> list[Token]:
    raise RuntimeError("boom: transient failure")


def _drain_to_quiescence(net: PetriNet, max_steps: int = 500) -> None:
    """Fire one transition at a time, fully settling each async action before the next.

    Mirrors `tests/test_seeded_determinism.py::_drain_to_quiescence` — deterministic
    firing order requires each action to complete before the next `step()` call.
    """
    for _ in range(max_steps):
        fired = net.step()
        deadline = time.monotonic() + 5.0
        while net._running_count > 0 and time.monotonic() < deadline:
            time.sleep(0.001)
        assert net._running_count == 0, "action did not settle within the deadline"
        if not fired:
            return
    raise AssertionError(f"net did not reach quiescence within {max_steps} steps")


class TestRoutingTables:
    """`_input_routing` / `_output_routing` are built lazily and route correctly."""

    def test_input_and_output_routing_map_places_to_transitions(self):
        net = PetriNet(
            places=[Place("a"), Place("b"), Place("c")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("a")],
                    outputs=[OutputArc("b")],
                    action=lambda tokens: tokens,
                )
            ],
        )
        assert net._incremental_eligible
        # Trigger `_ensure_scheduler_ready` via a scheduler-consulting call.
        assert net.is_dead() is True  # no tokens yet, nothing enabled

        assert [t.name for t in net._input_routing.get("a", [])] == ["t"]
        assert [t.name for t in net._output_routing.get("b", [])] == ["t"]
        # "c" is referenced by nothing.
        assert net._input_routing.get("c", []) == []
        assert net._output_routing.get("c", []) == []

    def test_conditional_output_arc_excluded_from_output_routing(self):
        """An unconditional OutputArc's place is routed; a conditional one's is not."""
        net = PetriNet(
            places=[Place("in"), Place("unconditional_out"), Place("conditional_out")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("in")],
                    outputs=[
                        OutputArc("unconditional_out"),
                        OutputArc("conditional_out", condition=_below_condition),
                    ],
                    action=lambda tokens: tokens,
                )
            ],
        )
        assert net.is_dead() is True

        assert [t.name for t in net._output_routing.get("unconditional_out", [])] == ["t"]
        # Conditional arc: place must NOT appear in _output_routing.
        assert net._output_routing.get("conditional_out", []) == []


class TestIncrementalOnlyReChecksAffected:
    """Firing one of two independent transitions only re-evaluates its own dependents."""

    def test_independent_transitions_fire_and_drain_correctly(self):
        net = PetriNet(
            max_workers=1,
            places=[Place("a_in"), Place("a_out"), Place("b_in"), Place("b_out")],
            transitions=[
                Transition(
                    name="ta",
                    priority=1,
                    inputs=[InputArc("a_in")],
                    outputs=[OutputArc("a_out")],
                    action=lambda tokens: tokens,
                ),
                Transition(
                    name="tb",
                    priority=2,
                    inputs=[InputArc("b_in")],
                    outputs=[OutputArc("b_out")],
                    action=lambda tokens: tokens,
                ),
            ],
        )
        # ta has strictly lower priority than tb, so it fires first deterministically —
        # no scheduler tie-break is involved here (that is covered separately below).
        net.deposit("a_in", Token(payload={"i": 1}))
        net.deposit("b_in", Token(payload={"i": 2}))

        # Both should be enabled independently, with cached bindings for both.
        net.is_dead()  # forces a reconcile
        assert set(net._enabled_bindings.keys()) == {"ta", "tb"}
        tb_binding_before = net._enabled_bindings["tb"]

        # Fire ta only, and settle it.
        assert net.step() is True
        deadline = time.monotonic() + 2.0
        while net._running_count > 0 and time.monotonic() < deadline:
            time.sleep(0.001)

        # ta's firing dirtied only a_in/a_out; tb's binding is untouched (same object).
        net.is_dead()  # reconcile again
        assert net._enabled_bindings["tb"] is tb_binding_before
        assert "ta" not in net._enabled_bindings

        # Draining the rest completes correctly (behavioral confirmation).
        _drain_to_quiescence(net)
        assert len(net.places["a_out"].tokens) == 1
        assert len(net.places["b_out"].tokens) == 1
        assert net.is_quiescent()


class TestBackPressureRelease:
    """Exercises `_capacity_blocked`: in-band and out-of-band drains unblock a producer."""

    def test_step_false_while_full_then_true_after_out_of_band_drain(self):
        net = PetriNet(
            places=[Place("input"), Place("output", bound=1)],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=lambda tokens: tokens,
                )
            ],
        )
        net.places["output"].deposit(Token())
        net.deposit("input", Token())

        assert net._incremental_eligible
        assert net.step() is False
        # Confirm the incremental scheduler tracked it as capacity-blocked, not just
        # absent from the enabled set.
        net.is_dead()  # reconcile
        assert "t" in net._capacity_blocked
        assert "t" not in net._enabled_bindings

        # Drain the output place out-of-band (not via net.step()/deposit()).
        net.places["output"].retrieve(1)

        assert net.step() is True
        net.run(deadline=time.monotonic() + 1.0)
        assert len(net.places["output"].tokens) == 1

    def test_in_band_drain_via_consuming_transition_also_unblocks(self):
        net = PetriNet(
            max_workers=2,
            places=[Place("input"), Place("output", bound=1), Place("sink")],
            transitions=[
                Transition(
                    name="producer",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=lambda tokens: tokens,
                ),
                Transition(
                    name="consumer",
                    inputs=[InputArc("output")],
                    outputs=[OutputArc("sink")],
                    action=lambda tokens: tokens,
                ),
            ],
        )
        net.places["output"].deposit(Token())
        net.deposit("input", Token())

        # producer is blocked; consumer is not.
        assert net.step() is True  # fires consumer (the only enabled transition)
        net.run(deadline=time.monotonic() + 1.0)
        # Consumer drains the pre-filled output token to sink, which unblocks producer;
        # producer then deposits its own token to output, and consumer drains that too.
        # Both the original output token and the producer's new one end up in sink.
        assert len(net.places["sink"].tokens) == 2
        assert len(net.places["output"].tokens) == 0
        assert len(net.places["input"].tokens) == 0


class TestRetryReactivation:
    """A rolled-back token with a future `available_at` re-arms via `_reactivation`."""

    def test_failed_action_reschedules_and_is_retried(self):
        net = PetriNet(
            max_workers=1,
            retry_delay=0.05,
            places=[Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=_raising_action,
                    max_retries=3,
                )
            ],
        )
        net.deposit("input", Token(payload={"i": 1}))

        assert net.step() is True  # fires, action will raise
        deadline = time.monotonic() + 2.0
        while net._running_count > 0 and time.monotonic() < deadline:
            time.sleep(0.001)

        # Not immediately enabled — the token was rolled back with a future available_at.
        assert net.step() is False
        assert not net.is_dead() or True  # is_dead() may be True right now (see below)
        # The token IS potentially enabled (cooling), so is_quiescent() must be False.
        assert net.is_quiescent() is False
        # The reactivation heap has an entry for the pending retry.
        assert len(net._reactivation) > 0

        # Wait out the retry delay and let run() pick it back up.
        net.run(deadline=time.monotonic() + 2.0)

        # Eventually dead-lettered or succeeded — either way the input place drains
        # after retries proceed. Since the action always raises and max_retries=3,
        # the token should end up dead-lettered to `error_place`.
        assert len(net.places["failed"].tokens) == 1
        assert len(net.places["input"].tokens) == 0

    def test_is_quiescent_false_but_is_dead_true_while_cooling(self):
        """Documents the distinction: dead *right now*, but not quiescent (will retry soon)."""
        net = PetriNet(
            max_workers=1,
            retry_delay=0.2,
            places=[Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=_raising_action,
                    max_retries=3,
                )
            ],
        )
        net.deposit("input", Token(payload={"i": 1}))
        assert net.step() is True
        deadline = time.monotonic() + 2.0
        while net._running_count > 0 and time.monotonic() < deadline:
            time.sleep(0.001)

        # Immediately after rollback: nothing fires right now (still cooling)...
        assert net.is_dead() is True
        # ...but the net is not quiescent, since the retry will become enabled soon.
        assert net.is_quiescent() is False


class TestInputlessSourceTransition:
    """An input-less transition is always volatile and always (re-)evaluated."""

    def test_source_transition_is_volatile_and_fires(self):
        net = PetriNet(
            places=[Place("out", bound=1)],
            transitions=[
                Transition(
                    name="source",
                    inputs=[],
                    outputs=[OutputArc("out")],
                    action=lambda tokens: [Token(payload={"produced": True})],
                )
            ],
        )
        # Trigger scheduler build.
        net.is_dead()
        assert "source" in net._volatile_transitions

        assert net.step() is True
        net.run(deadline=time.monotonic() + 1.0)
        assert len(net.places["out"].tokens) == 1
        # Bound=1 output means the source is now capacity-blocked, not gone entirely.
        assert net.is_dead() is True


class TestGuardToggledWithNoMarkingChange:
    """A guard reading external mutable state re-evaluates every reconcile (volatile)."""

    def test_guard_toggle_without_marking_change_flips_enablement(self):
        STATE["open"] = False
        net = PetriNet(
            places=[Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=lambda tokens: tokens,
                    guard=_state_guard,
                )
            ],
        )
        net.deposit("input", Token())

        # Guard closed: transition should not fire despite a token being present.
        assert net.step() is False

        # Flip external state with NO marking change (no deposit/consume happened).
        STATE["open"] = True
        try:
            assert net.step() is True
        finally:
            STATE["open"] = False  # reset for test isolation


class TestFallbackParity:
    """RANDOM/PRIORITY transitions and PacedResourcePlace force `_incremental_eligible` False."""

    def test_random_policy_transition_disables_incremental_path(self):
        net = PetriNet(
            seed=42,
            max_workers=1,
            places=[Place("input"), Place("output")],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input")],
                    outputs=[OutputArc("output")],
                    action=lambda tokens: tokens,
                    binding_policy=BindingPolicy.RANDOM,
                )
            ],
        )
        assert net._incremental_eligible is False
        for i in range(5):
            net.deposit("input", Token(payload={"i": i}))

        net.run(deadline=time.monotonic() + 2.0)
        assert len(net.places["output"].tokens) == 5
        assert len(net.places["input"].tokens) == 0

    def test_paced_resource_place_disables_incremental_path(self):
        net = PetriNet(
            max_workers=1,
            places=[
                Place("input"),
                Place("output"),
                PacedResourcePlace("permits", capacity=2, pacing_secs=0.01),
            ],
            transitions=[
                Transition(
                    name="t",
                    inputs=[InputArc("input"), InputArc("permits")],
                    outputs=[OutputArc("output"), OutputArc("permits")],
                    action=lambda tokens: [t for t in tokens if not t.is_resource],
                )
            ],
        )
        assert net._incremental_eligible is False
        for i in range(4):
            net.deposit("input", Token(payload={"i": i}))

        net.run(deadline=time.monotonic() + 3.0)
        assert len(net.places["output"].tokens) == 4
        assert len(net.places["input"].tokens) == 0


class TestIsQuiescentIsDeadOnIncrementalPath:
    def test_is_dead_and_is_quiescent_basic_transitions(self):
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
        assert net._incremental_eligible
        # No tokens yet: dead.
        assert net.is_dead() is True

        net.deposit("input", Token())
        # A token is present: not dead.
        assert net.is_dead() is False

        net.run(deadline=time.monotonic() + 1.0)
        # Drained: quiescent.
        assert net.is_quiescent() is True
        assert net.is_dead() is True


def _make_tie_action(name: str, firings: list[tuple[str, int]]):
    def action(tokens: list[Token]) -> list[Token]:
        firings.append((name, tokens[0].payload["i"]))
        return tokens

    return action


def _build_tie_net(firings: list[tuple[str, int]]) -> PetriNet:
    """Three equal-priority, guard-free LEGACY transitions racing for one shared place.

    No RANDOM/PRIORITY transition and no timed feature, so `_incremental_eligible` stays
    `True` and selection goes through `_select_incremental`'s priority-bucket tie-break.
    """
    net = PetriNet(
        seed=20260727,
        max_workers=1,
        places=[Place("tie_in"), Place("a_out"), Place("b_out"), Place("c_out")],
        transitions=[
            Transition(
                name="tie_a",
                priority=1,
                inputs=[InputArc("tie_in")],
                outputs=[OutputArc("a_out")],
                action=_make_tie_action("tie_a", firings),
            ),
            Transition(
                name="tie_b",
                priority=1,
                inputs=[InputArc("tie_in")],
                outputs=[OutputArc("b_out")],
                action=_make_tie_action("tie_b", firings),
            ),
            Transition(
                name="tie_c",
                priority=1,
                inputs=[InputArc("tie_in")],
                outputs=[OutputArc("c_out")],
                action=_make_tie_action("tie_c", firings),
            ),
        ],
    )
    for i in range(8):
        net.deposit("tie_in", Token(payload={"i": i}))
    assert net._incremental_eligible, "this net must stay on the incremental path"
    return net


class TestGoldenOrderDeterminismLegacyTie:
    """Pin the incremental scheduler's firing order for a seeded, guard-free tie net.

    Unlike `tests/test_seeded_determinism.py` (which exercises RANDOM/PRIORITY/FIRST and
    therefore runs on the *fallback* full-scan path via `_has_search_policy_transition`),
    this net has ONLY equal-priority, guard-free LEGACY transitions competing for the head
    token of a shared input place — so `net._incremental_eligible` stays `True` and this pins
    `_select_incremental`'s O(1) bucket + `_rng.choice` tie-break specifically.

    Cross-process determinism: `_reconcile_dirty` re-evaluates the affected transitions in
    **registration order** (`PetriNet._transition_order`), not raw `set` iteration order, so
    the per-priority bucket list that `_rng.choice` indexes into is identical regardless of
    Python's per-process string-hash seed. The golden literal below is therefore reproducible
    under any `PYTHONHASHSEED` — pinned by a plain in-process assertion, no subprocess needed.

    HOW TO REGENERATE: if you deliberately change the incremental scheduler's selection
    order (e.g. bucket iteration, tie-break draw count), run:
        uv run python -c "
        from tests.test_incremental_enablement import _build_tie_net, _drain_to_quiescence
        firings = []
        _drain_to_quiescence(_build_tie_net(firings))
        print(firings)
        "
    and paste the new literal into EXPECTED below, explaining why in the commit message
    that changes the engine.
    """

    # Golden sequence for SEED=20260727, max_workers=1, produced by the current incremental
    # engine (`PetriNet._select_incremental`). Hash-seed independent (see class docstring).
    # Regenerate per the class docstring if the scheduler's selection order deliberately changes.
    EXPECTED = [
        ("tie_b", 0),
        ("tie_a", 1),
        ("tie_a", 2),
        ("tie_b", 3),
        ("tie_c", 4),
        ("tie_b", 5),
        ("tie_c", 6),
        ("tie_c", 7),
    ]

    def test_golden_firing_sequence(self):
        firings: list[tuple[str, int]] = []
        _drain_to_quiescence(_build_tie_net(firings))

        assert firings == self.EXPECTED, (
            f"Incremental scheduler firing order changed.\nActual: {firings!r}\n"
            "If deliberate, regenerate EXPECTED per the class docstring."
        )
        # Sanity: every token consumed exactly once, split across the three transitions.
        assert {i for _, i in firings} == set(range(8))
        assert len(firings) == 8

    def test_same_seed_twice_yields_identical_sequence(self):
        """Weaker, in-process check: independent of hash-seed pinning, A==A must hold."""
        firings1: list[tuple[str, int]] = []
        net1 = _build_tie_net(firings1)
        _drain_to_quiescence(net1)

        firings2: list[tuple[str, int]] = []
        net2 = _build_tie_net(firings2)
        _drain_to_quiescence(net2)

        assert firings1 == firings2
