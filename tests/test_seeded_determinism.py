"""Pin the exact firing sequence a seeded net produces, across every order-sensitive
binding-resolution path, so a silent RNG-stream shift becomes a loud test failure.

Why this file exists
---------------------
Existing reproducibility tests (see `test_binding_search.py::TestRandomPolicy` etc.)
only assert "same seed twice -> same result". That stays TRUE even if the seeded RNG
*stream* shifts wholesale — e.g. a reorder of candidate-binding enumeration changes
which draw goes to which decision, but both runs still reorder identically, so an
A==A comparison can't see it. This file instead pins the full ordered sequence of
firings (transition name + consumed payload ids) against a literal, committed golden
value. Any change to binding-resolution order — deliberate or accidental — shows up
as a diff here.

Four order-sensitive paths are exercised by one net, in one seeded run, so all four
have to independently agree with history for this test to pass:

1. BindingPolicy.RANDOM   -> `_reservoir_pick` draws `self._rng.random()` per
   satisfying candidate; the draw sequence depends on enumeration order.
2. BindingPolicy.PRIORITY -> `_reduce_min_key` breaks ties by first-encountered,
   which is enumeration order.
3. BindingPolicy.FIRST + guard -> the winning binding is whichever satisfies the
   guard first in enumeration order.
4. Scheduler tie-break -> `_select_transition_to_fire` does `self._rng.choice(...)`
   among transitions sharing the lowest `priority`; this draws from the SAME seeded
   RNG stream as (1), so it is sensitive to upstream draws shifting too.

Each policy gets its own lower-priority-number transition so it fires to exhaustion
before the next policy's transition becomes the scheduler's pick — this keeps the
recorded sequence cleanly segmented (RANDOM block, then PRIORITY block, then FIRST
block, then the tie-break block) while still drawing from one shared `_rng` stream.
"""

import hashlib
import time

from cpnx.engine import PetriNet
from cpnx.places import Place
from cpnx.tokens import Token
from cpnx.transitions import BindingPolicy, InputArc, OutputArc, Transition

SEED = 20260719


# --- Module-level guards / keys -------------------------------------------------
# Transition.__setattr__ purity-checks `guard` and `binding_priority_key` via
# `inspect.getsource`, which raises PermissionError for dynamically-defined
# (e.g. locally-nested) callables. These must be real module-level defs.


def _even_guard(toks: list[Token]) -> bool:
    """Satisfied by even-`i` payloads only — RANDOM must scan/skip odd candidates."""
    return toks[0].payload["i"] % 2 == 0


def _ok_guard(toks: list[Token]) -> bool:
    """Satisfied by tag == 'ok' — FIRST must skip 'no' candidates in enumeration order."""
    return toks[0].payload["tag"] == "ok"


def _rank_key(toks: list[Token]) -> int:
    """PRIORITY sort key with deliberate duplicate ranks, forcing tie-breaks."""
    return toks[0].payload["rank"]


def _drain_to_quiescence(net: PetriNet, max_steps: int = 200) -> None:
    """Fire one transition at a time, fully settling each async action before the next.

    `step()` only submits the action to the thread pool and returns; firing order is
    only deterministic if each action fully completes before the next `step()` call
    (otherwise concurrent commits could interleave). Spin on `_running_count` rather
    than using `run()`, which loops on wall-clock pacing we don't want here.
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


def _make_recording_action(name: str, firings: list[tuple[str, tuple]]):
    """Build an action that records (transition_name, consumed 'i' payloads) and passes
    tokens through unchanged. A closure is fine here — only `guard`/`binding_priority_key`
    are purity-checked, not `action`."""

    def action(toks: list[Token]) -> list[Token]:
        firings.append((name, tuple(t.payload["i"] for t in toks)))
        return toks

    return action


def _build_net(firings: list[tuple[str, tuple]]) -> PetriNet:
    net = PetriNet(
        seed=SEED,
        max_workers=1,
        places=[
            Place("random_in"),
            Place("random_out"),
            Place("priority_in"),
            Place("priority_out"),
            Place("first_in"),
            Place("first_out"),
            Place("tie_in"),
            Place("tie_a_out"),
            Place("tie_b_out"),
        ],
        transitions=[
            # (1) RANDOM — reservoir-picks among even-i candidates each round.
            Transition(
                name="random",
                priority=1,
                inputs=[InputArc("random_in")],
                outputs=[OutputArc("random_out")],
                action=_make_recording_action("random", firings),
                guard=_even_guard,
                binding_policy=BindingPolicy.RANDOM,
            ),
            # (2) PRIORITY — custom rank key with deliberate ties.
            Transition(
                name="priority",
                priority=2,
                inputs=[InputArc("priority_in")],
                outputs=[OutputArc("priority_out")],
                action=_make_recording_action("priority", firings),
                binding_policy=BindingPolicy.PRIORITY,
                binding_priority_key=_rank_key,
            ),
            # (3) FIRST + guard — first 'ok' in enumeration order wins each round.
            Transition(
                name="first",
                priority=3,
                inputs=[InputArc("first_in")],
                outputs=[OutputArc("first_out")],
                action=_make_recording_action("first", firings),
                guard=_ok_guard,
                binding_policy=BindingPolicy.FIRST,
            ),
            # (4) Scheduler tie-break — ta/tb share priority 4 and compete for the
            # same head token of "tie_in"; _select_transition_to_fire's
            # rng.choice(candidates) decides the winner each round.
            Transition(
                name="tie_a",
                priority=4,
                inputs=[InputArc("tie_in")],
                outputs=[OutputArc("tie_a_out")],
                action=_make_recording_action("tie_a", firings),
            ),
            Transition(
                name="tie_b",
                priority=4,
                inputs=[InputArc("tie_in")],
                outputs=[OutputArc("tie_b_out")],
                action=_make_recording_action("tie_b", firings),
            ),
        ],
    )

    # (1) RANDOM: 8 tokens, only even i satisfy the guard (0,2,4,6).
    for i in range(8):
        net.deposit("random_in", Token(payload={"i": i}))

    # (2) PRIORITY: deliberate duplicate ranks (2 appears three times) to force
    # first-encountered tie-breaks.
    ranks = [5, 2, 9, 2, 7, 2]
    for i, rank in enumerate(ranks):
        net.deposit("priority_in", Token(payload={"i": i, "rank": rank}))

    # (3) FIRST + guard: 'ok' tokens are interleaved among 'no' tokens so the
    # winning candidate is never simply the head.
    tags = ["no", "ok", "no", "ok", "no", "ok"]
    for i, tag in enumerate(tags):
        net.deposit("first_in", Token(payload={"i": i, "tag": tag}))

    # (4) Scheduler tie-break: 6 tokens; ta/tb race for each one in turn.
    for i in range(6):
        net.deposit("tie_in", Token(payload={"i": i}))

    return net


# Golden sequence for SEED = 20260719, max_workers=1, produced by the current
# engine. If binding resolution changes deliberately (e.g. issue #18 budget
# accounting, expression inlining), regenerate this literal in the SAME commit
# as the engine change and explain why in the commit message. If you did NOT
# intend to touch binding resolution and this test fails, treat it as a real
# regression: the engine is choosing different bindings than before.
EXPECTED_FIRINGS = [
    ("random", (6,)),
    ("random", (2,)),
    ("random", (4,)),
    ("random", (0,)),
    ("priority", (1,)),
    ("priority", (3,)),
    ("priority", (5,)),
    ("priority", (0,)),
    ("priority", (4,)),
    ("priority", (2,)),
    ("first", (1,)),
    ("first", (3,)),
    ("first", (5,)),
    ("tie_b", (0,)),
    ("tie_a", (1,)),
    ("tie_a", (2,)),
    ("tie_a", (3,)),
    ("tie_b", (4,)),
    ("tie_b", (5,)),
]

EXPECTED_SHA256 = "6bd4a9da3ab18937d7e3c60a76208f0cc38b64a77d35a8ae8eb0424cff52bbe9"


def _fingerprint(firings: list[tuple[str, tuple]]) -> str:
    return hashlib.sha256(repr(firings).encode("utf-8")).hexdigest()


FAILURE_MESSAGE = """
Seeded RANDOM/PRIORITY/FIRST/tie-break firing sequence changed.

This test pins the exact binding-resolution order for a fixed seed across all
four order-sensitive paths (RANDOM reservoir picks, PRIORITY tie-breaks, FIRST
enumeration-order wins, and the scheduler's priority tie-break). It exists
because "same seed twice -> same result" checks stay green even when the RNG
*stream* shifts wholesale (e.g. candidate enumeration reordered) — a shifted
stream still reproduces itself, so only a pinned sequence like this one can
catch it.

If you DELIBERATELY changed binding resolution (e.g. issue #18 budget
accounting, expression inlining, arc enumeration order), this failure is
EXPECTED: verify the new sequence below is correct, then update
EXPECTED_FIRINGS and EXPECTED_SHA256 in tests/test_seeded_determinism.py in
the SAME commit, noting why in the commit message.

If you did NOT intend to change binding resolution, this is a REAL
regression: the engine is now choosing different bindings for the same seed.

Actual firing sequence:
{actual!r}

Actual sha256: {actual_hash}
"""


class TestSeededDeterminismAcrossPolicies:
    def test_full_firing_sequence_matches_golden(self):
        firings: list[tuple[str, tuple]] = []
        net = _build_net(firings)
        _drain_to_quiescence(net)

        actual_hash = _fingerprint(firings)
        msg = FAILURE_MESSAGE.format(actual=firings, actual_hash=actual_hash)

        assert firings == EXPECTED_FIRINGS, msg
        assert actual_hash == EXPECTED_SHA256, msg

    def test_all_tokens_were_eventually_consumed_by_the_expected_transitions(self):
        """Sanity check independent of ordering: exactly the tokens the golden sequence
        implies should be consumed end up in the corresponding output places, and the
        guard-failing tokens are left behind untouched."""
        firings: list[tuple[str, tuple]] = []
        net = _build_net(firings)
        _drain_to_quiescence(net)

        # RANDOM: all four even tokens fired; all four odd tokens remain (guard never
        # satisfied for them).
        assert {t.payload["i"] for t in net.places["random_out"].tokens} == {0, 2, 4, 6}
        assert {t.payload["i"] for t in net.places["random_in"].tokens} == {1, 3, 5, 7}

        # PRIORITY: all six tokens eventually fire (min-key search always finds one).
        assert {t.payload["i"] for t in net.places["priority_out"].tokens} == {0, 1, 2, 3, 4, 5}
        assert len(net.places["priority_in"].tokens) == 0

        # FIRST + guard: only the three 'ok' tokens fire; 'no' tokens are never consumed.
        assert {t.payload["i"] for t in net.places["first_out"].tokens} == {1, 3, 5}
        assert {t.payload["i"] for t in net.places["first_in"].tokens} == {0, 2, 4}

        # Tie-break: all six tokens fire, split across ta/tb, none left behind.
        tie_a_ids = {t.payload["i"] for t in net.places["tie_a_out"].tokens}
        tie_b_ids = {t.payload["i"] for t in net.places["tie_b_out"].tokens}
        assert tie_a_ids | tie_b_ids == {0, 1, 2, 3, 4, 5}
        assert tie_a_ids.isdisjoint(tie_b_ids)
        assert len(net.places["tie_in"].tokens) == 0
