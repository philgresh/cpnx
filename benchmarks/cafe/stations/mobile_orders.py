"""📱 The mobile-order rush — a deep **matured** backlog behind a scarce resource.

Cafe role:
    App customers order ahead for a pickup slot. At the top of the morning rush a wave of
    slots comes due at once, far more than the one barista on the mobile-order rail can
    make, so due orders pile up on the rail and are made one at a time. The barista needs
    `make_secs` per drink before the next one can start.

Demonstrates:
    The **matured timed backlog**: many tokens whose `available_at` has passed but which
    cannot be consumed yet, because the resource they wait on is busy. The cold-brew tower
    is the opposite shape. Its batches are poured the moment they mature, so its matured
    backlog stays near zero, and it matures in a few dozen cohorts, so the clock advances
    only a few dozen times however deep the tower is.

    Here the rail stays deep *and* the clock advances once per drink, because the barista
    is a [`PacedResourcePlace`][cpnx.PacedResourcePlace] with capacity 1. Every advance asks
    every place for its next future boundary, so a store that re-walks its matured tokens to
    answer pays O(backlog) per drink and O(N²) for the rush. That is cause A of
    [#59](https://github.com/philgresh/cpnx/issues/59).

    `T_Make_Mobile_Order` is a plain guard-free FIFO, so after A the remaining per-drink cost
    is the FIFO pool read itself, which peeks up to `binding_search_limit + 1` tokens per
    evaluation (cause B of #59). Deposit the same orders as `AVAILABLE_NOW` instead of timed
    and the run does identical work with an untimed rail, which is the control arm the
    throughput benchmark reports alongside it.

    Like every station, nothing here deposits: the benchmark decides when orders come due.
"""

from cafe.support import has_payload, with_work
from cpnx import InputArc, OutputArc, PacedResourcePlace, Place, Token, Transition


def make_mobile_order(tokens: list[Token]) -> list[Token]:
    """**T_Make_Mobile_Order**'s action: make the oldest due app order and hand it off.

    Cafe role:
        The rail is first come, first served by due time; the drink goes straight to the
        pickup shelf rather than through the shot and milk rendezvous.

    Demonstrates:
        Nothing beyond the timed store, deliberately. The barista permit arrives alongside the
        order and is returned by the engine (a synthesized self-loop, ADR 0009), so the action
        only produces the drink.
    """
    order = next(t for t in tokens if not t.is_resource)
    return [order.evolve(payload_updates={"stage": "mobile_pickup"}, color="drink")]


def places(*, make_secs: float = 30.0) -> list[Place]:
    """The mobile-order rail and the one barista who works it.

    Args:
        make_secs: Logical seconds the barista needs per drink before starting the next one.
            Defaults to 30.0.
    """
    return [
        Place("P_Mobile_Rail", color_set={"mobile"}, schema=has_payload),
        PacedResourcePlace("P_Mobile_Barista", capacity=1, pacing_secs=make_secs),
    ]


def transitions(*, work_secs: float = 0.0) -> list[Transition]:
    """**T_Make_Mobile_Order** — the barista makes the next due order.

    Demonstrates:
        A guard-free, keyless, default-`FIRST` transition with one data arc and one permit arc,
        so the only cost that grows with the rush is the timed store under `P_Mobile_Rail`.

    Args:
        work_secs: Physical seconds the station occupies a worker.
    """
    return [
        Transition(
            name="T_Make_Mobile_Order",
            inputs=[InputArc("P_Mobile_Rail"), InputArc("P_Mobile_Barista")],
            outputs=[OutputArc("P_Served")],
            action=with_work(work_secs, make_mobile_order),
            action_timeout_secs=0.5,
        )
    ]
