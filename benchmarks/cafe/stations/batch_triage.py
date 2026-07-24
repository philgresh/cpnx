"""📋 The rush-hour triage queue — a deep place drained through an [`InputArc.key`][cpnx.InputArc].

Cafe role:
    Mid-rush the rail is twenty tickets deep and the barista stops working in
    strict arrival order. Oat-milk tickets get clustered together (switching
    milks means re-purging the wand every time), and within a milk group the
    tickets least likely to bounce through rework go first.

Demonstrates:
    The **certified [`InputArc.key`][cpnx.InputArc] fast path** — the shape the persistent
    `(key, seq)` min-heap on the place exists to serve, and the fixture's
    headline win: draining this queue went from ≈O(N² log N) to ≈O(N log N).

    It is deliberately a *different mechanism* from `T_Weigh_And_Grind`'s
    `binding_priority_key`. That one reorders whole enumerated **bindings** at
    the transition level; this one reorders one arc's **token pool** before any
    binding is formed. Having both in one net is what makes the distinction
    legible.

    Because the ordering value is per-token and pure, the engine can compute it
    once at deposit and keep it in a heap, rather than re-deriving it for the
    whole marking on every firing — which is exactly what an opaque
    `list[Token] -> list[Token]` arc expression could never allow. See
    [ADR 0004](https://github.com/philgresh/cpnx/blob/main/docs/adr/0004-arc-selection-key-filter.md).
"""

from cafe.support import DOSE_TARGET_G, with_work
from cpnx import InputArc, OutputArc, Place, Token, Transition


def batch_triage_key(token: Token) -> tuple[int, int, float]:
    """[`InputArc.key`][cpnx.InputArc] for the triage queue: how a barista triages a deep rush.

    Cafe role:
        Not a random shuffle — a real batching heuristic, in two groupings:

        1. **Oat before dairy.** Switching milks mid-rush means re-purging the steam
           wand every single time (carryover flavour), so a barista clusters every
           oat-milk ticket together before touching a dairy one rather than
           alternating.
        2. **On-spec before out-of-spec.** Within a milk group, a ticket whose dose is
           already on target is pulled ahead of one likely to bounce through
           `T_Rework_Dose` — a rush doesn't want to get stuck behind a slow ticket.

    Demonstrates:
        A **certified** per-token key. It reads only the token's own `payload` and
        `created_at` and closes over nothing mutable, so `cpnx.certification` proves
        it closed-world and the engine both (a) evaluates it inline rather than
        round-tripping it through the timeout-bounded expression pool, and (b) is
        willing to index it — an uncertified key cannot be indexed at all, because
        keying happens on the `deposit()` path, which cannot wait on an executor.

        Ties fall to `created_at`, and the engine breaks any remaining tie by
        insertion order, so the drain stays deterministic. Note this reorders the
        *groups*, not the tickets within them: every ticket is still consumed
        eventually, just not in strict arrival order.

        [`cafe.stations.specials_board`][cafe.stations.specials_board] holds the deliberately-uncertified twin of
        this function, for measuring what certification is worth.
    """
    return (
        0 if token.payload.get("dairy_free") else 1,
        0 if token.payload.get("weight_g", DOSE_TARGET_G) == DOSE_TARGET_G else 1,
        token.created_at,
    )


def serve_batch_triage(tokens: list[Token]) -> list[Token]:
    """**T_Batch_Triage_Serve**'s action: hand a triaged ticket straight out as a drink.

    Demonstrates:
        Deliberate **minimalism as experimental hygiene**. It skips the
        grind/pull/steam machinery entirely, because this queue exists to exercise
        [`InputArc.key`][cpnx.InputArc] over a deep pool and nothing else — re-modelling the full
        pipeline a second time would put unrelated engine work in the measurement.
    """
    ticket = tokens[0]
    return [ticket.evolve(payload_updates={"stage": "drink"}, color="drink")]


def places() -> list[Place]:
    """The backlog — an unbounded FIFO [`Place`][cpnx.Place], same shape as `P_Ticket_Line`."""
    return [Place("P_Batch_Triage_Queue")]


def transitions(*, work_secs: float = 0.0) -> list[Transition]:
    """**T_Batch_Triage_Serve** — pull the next ticket in triage order.

    Demonstrates:
        A single keyed input arc and nothing else: no guard, no filter, default
        `LEGACY` policy, `count=1`. Under `LEGACY` the arc is read head-only, so the
        key index is asked for just `count` tokens — the cheapest possible read of a
        deep keyed place, and the one the throughput benchmark's key-index rows
        measure.

    Args:
        work_secs: Physical seconds the station occupies a worker.
    """
    return [
        Transition(
            name="T_Batch_Triage_Serve",
            inputs=[InputArc("P_Batch_Triage_Queue", key=batch_triage_key)],
            outputs=[OutputArc("P_Served")],
            action=with_work(work_secs, serve_batch_triage),
            action_timeout_secs=0.5,
        )
    ]
