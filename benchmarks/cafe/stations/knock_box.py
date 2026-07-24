"""🥁 The knock box — a `consume_all` drain gated behind a guard that is usually `False`.

Cafe role:
    Every spent puck gets knocked out into a bin under the bar. The bin fills all
    through the rush — nobody stops to empty it between orders — and only in a lull,
    with a group head free, does the barista pick it up and empty the whole thing into
    the trash in one motion.

Demonstrates:
    The **pathological interaction between `consume_all` and a guard**. A `consume_all`
    arc always takes route 3 of `engine._materialize_pool` — `place.peek(len(place))` —
    because routes 1 and 2 both require `not arc.consume_all`. That alone just makes the
    read O(marking depth) instead of O(1)/O(log N). The trap is *when* it pays that cost:
    `_is_transition_enabled` resolves a binding (which gathers every arc's pool, including
    this one's full-place peek) *before* it evaluates the guard, not after. So on every
    single `step()` while the lull guard is `False`, the engine still peeks the entire
    knock box, builds the binding, and only then discards it because the guard said no.

    The consequence worth stating plainly: **the less often this transition fires, the
    more it costs.** A knock box that is emptied every few orders is scanned shallow, over
    and over. One that is emptied only during rare lulls is scanned at its deepest, over
    and over — the guard's whole job is to make firing rare, and rarity is exactly what
    makes each rejected check expensive. Nothing about a low firing rate makes this
    station cheap; it makes it expensive more often per unit of useful work.

    This station deliberately does **not** trigger the documented `consume_all`
    footgun — draining ignores `key`/`filter` and a `UserWarning` fires if either is set
    (see the `Warning` block on [`InputArc`][cpnx.InputArc]) — because neither is set here. Worth noting
    anyway: "drain only the eligible pucks" is not a supported combination with
    `consume_all`; the workaround the docs point to is a large `count` instead of
    `consume_all`, which this station does not need since it always wants everything in
    the bin.

    Sweep **lull frequency** (`min_pucks`, larger = rarer firings) crossed with **knock-box
    depth** (how many pucks are stocked before the run) — cost per rejected check should
    scale with depth, and total cost should climb as `min_pucks` rises even though fewer
    firings occur.
"""

from collections.abc import Callable

from cafe.support import with_work
from cpnx import InputArc, OutputArc, Place, Token, Transition


def make_lull_guard(min_pucks: int) -> Callable[[list[Token]], bool]:
    """Build **T_Empty_Knock_Box**'s guard: only empty the bin once it is worth the trip.

    Cafe role:
        A barista doesn't stoop to empty the knock box for two pucks — that's a lull
        worth spending on, not a rush-hour interruption. The guard stands in for "things
        have quieted down enough that this is worth doing now," which in a real rush is
        true rarely and for the rest of the time is false.

    Demonstrates:
        A **certified guard factory**: `_dense_enough` closes over `min_pucks` — an
        immutable `int` captured at construction — and nothing else, so
        `cpnx.certification` proves it closed-world and the engine evaluates it inline
        under the lock rather than round-tripping it through the timeout-bounded
        executor. It is legitimate here to count the *whole* bound token list rather than
        inspect one token, because `consume_all=True` on the knock-box arc guarantees the
        binding already contains every available puck — there is no partial view to worry
        about, unlike a guard written against an arc with `count` set to something less
        than the pool.

        The bound list also carries the `P_Espresso_Machine` permit token, so the count
        excludes resource tokens via [`Token.is_resource`][cpnx.Token] — only spent pucks count toward
        the threshold.

    Args:
        min_pucks: Minimum number of pucks that must be in the bin for the guard to
            allow firing. This is the lull-frequency knob: a higher value makes firing
            rarer, and rarity is exactly what this station's cost model punishes.

    Returns:
        A guard `Callable[[list[Token]], bool]` suitable for [`Transition.guard`][cpnx.Transition].
    """

    def _dense_enough(tokens: list[Token]) -> bool:
        pucks = [t for t in tokens if not t.is_resource]
        return len(pucks) >= min_pucks

    return _dense_enough


def empty_knock_box(tokens: list[Token]) -> list[Token]:
    """**T_Empty_Knock_Box**'s action: tip the whole bin into the trash.

    Cafe role:
        One motion, whatever is in the bin — there is no sorting or salvaging spent
        pucks, so the action just forwards every consumed puck token straight through to
        `P_Trash_Can` unchanged (aside from the resource permit, which is excluded here
        and released back to `P_Espresso_Machine` by the engine's own resource-arc
        bookkeeping, not by this action).

    Demonstrates:
        The same **minimalism as experimental hygiene** used throughout this fixture
        (see [`cafe.stations.batch_triage.serve_batch_triage`][cafe.stations.batch_triage.serve_batch_triage]): the
        action does no work that isn't the point of the station, so the benchmark cost is attributable to the
        arc/guard interaction described in the module docstring, not to the action body.
    """
    return [t for t in tokens if not t.is_resource]


def places() -> list[Place]:
    """The bin — a plain unbounded [`Place`][cpnx.Place] that accumulates spent pucks all through the
    rush.

    Demonstrates:
        The **deep, ungated accumulator** this station's guard is built to stall against.
        Nothing here caps how deep the bin gets between lulls; depth is entirely a
        function of how long the benchmark lets the rush run before the guard admits a
        lull, which is what makes it a controllable experimental knob rather than a fixed
        property of the fixture.
    """
    return [Place("P_Knock_Box")]


def transitions(*, work_secs: float = 0.0, min_pucks: int = 25) -> list[Transition]:
    """**T_Empty_Knock_Box** — drain the bin in one atomic motion, but only during a lull.

    Demonstrates:
        The full pathological combination in one transition: a `consume_all=True` input
        arc (forcing the O(marking-depth) full-place peek on every enabling check) paired
        with a `guard` that is `False` most of the time (so most of those peeks are
        thrown away unused). The second input arc, a permit on `P_Espresso_Machine`,
        models "a group head is free" — the barista needs both a full bin and a spare
        hand before emptying it. See the module docstring for why this makes rarer
        firings *more* expensive overall, not less.

    Args:
        work_secs: Physical seconds the station occupies a worker.
        min_pucks: Minimum bin depth before the lull guard allows firing — the lull
            frequency knob. Defaults to 25.
    """
    return [
        Transition(
            name="T_Empty_Knock_Box",
            inputs=[
                InputArc("P_Knock_Box", consume_all=True),
                InputArc("P_Espresso_Machine", count=1),
            ],
            outputs=[OutputArc("P_Trash_Can")],
            guard=make_lull_guard(min_pucks),
            action=with_work(work_secs, empty_knock_box),
            action_timeout_secs=0.5,
        )
    ]
