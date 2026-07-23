"""🫘 The decaf-only barista — a deep place drained through an [`InputArc.filter`][cpnx.InputArc] alone.

Cafe role:
    One barista works a side station that serves only decaf tickets, pulled off the same
    kind of deep backlog as the rush-hour rail. There is no batching heuristic here, no
    milk-clustering, no dose-spec preference — just eligibility. A ticket either says
    decaf or it doesn't, and the barista takes the first eligible one in arrival order.

Demonstrates:
    The **filter-only performance cliff**: an [`InputArc`][cpnx.InputArc] with a `filter` but no `key`
    never gets a key index, even when the filter is fully certified. In
    `engine._materialize_pool` the three routes are tried cheapest first — bounded FIFO
    peek requires no `key` *and* no `filter`; the key-index read requires `arc.key` to be
    set at all (`_ensure_key_index` returns `False` immediately when it is `None`,
    regardless of the filter's certification). A filter-only arc fails both, so it always
    lands on the third route: `place.peek(len(place))` followed by a per-firing
    filter-then-sort over the whole available marking, on **every enabling check** for
    this transition — including checks where it does not end up firing. Draining a place
    this way is O(N) per step, i.e. an O(N^2) drain overall.

    The sharp point is that [`InputArc`][cpnx.InputArc]'s own docs tell users to certify selection
    callables on a deep place, and that advice is only half true here. Certification
    rescues a *keyed* arc, because keying happens on the `deposit()` path and certified
    keys are what the persistent min-heap indexes —
    [`cafe.stations.batch_triage.batch_triage_key`][cafe.stations.batch_triage.batch_triage_key] is that reproducer.
    Certifying a filter removes the executor round-trip per token, but it does not remove the O(N) scan:
    `_ensure_key_index` never even looks at `_filter_inline_safe` unless `arc.key` is already set.
    [`decaf_ticket`][cafe.stations.decaf.decaf_ticket] below certifies cleanly and the cliff is still there.

    The knob worth sweeping is **selectivity**, i.e. the decaf rate among the queue's
    tokens. At a 10% rate the filter still dispatches against all N tokens per check to
    find one ~10 deep; cost should come out flat across the rate and linear in N, and that
    flatness is the tell that the measured cost is the peek, not the predicate. Compare
    decaf rates of 0.5, 0.1, and 0.01 against a fixed depth to see it directly.
"""

from cafe.support import with_work
from cpnx import InputArc, OutputArc, Place, Token, Transition


def decaf_ticket(token: Token) -> bool:
    """[`InputArc.filter`][cpnx.InputArc] for the decaf line: is this ticket decaf?

    Cafe role:
        The barista's only question. No ranking among decaf tickets — arrival order
        among the eligible ones is all that's left once ineligible tickets are excluded.

    Demonstrates:
        A **certified** filter predicate: it reads only the token's own `payload` and
        closes over nothing mutable, so `cpnx.certification` proves it closed-world and
        the engine runs it inline rather than round-tripping it through the
        timeout-bounded expression pool. That certification pays off once per token
        dispatched — it does not change *how many* tokens get dispatched, which is the
        whole point of this station: see the module docstring for why a `key` is what
        would actually change that count, and this arc deliberately has none.
    """
    return bool(token.payload.get("decaf"))


def serve_decaf(tokens: list[Token]) -> list[Token]:
    """**T_Decaf_Pull**'s action: hand a decaf ticket out as a served drink.

    Demonstrates:
        The same deliberate **minimalism as experimental hygiene** used by
        [`cafe.stations.batch_triage.serve_batch_triage`][cafe.stations.batch_triage.serve_batch_triage] — no
        grind/pull/steam machinery, so the only engine work this station's benchmark can be measuring
        is the arc's own selection cost.
    """
    ticket = tokens[0]
    return [ticket.evolve(payload_updates={"stage": "drink"}, color="drink")]


def places() -> list[Place]:
    """The decaf backlog — a plain unbounded FIFO [`Place`][cpnx.Place], holding both decaf and non-decaf
    tickets so the filter has something to exclude.

    Demonstrates:
        The **shared-pool shape** the filter-only cliff needs: eligibility narrows the
        pool *within* the place rather than the place being pre-sorted, which is exactly
        what forces route 3's full-marking peek in `engine._materialize_pool`.
    """
    return [Place("P_Decaf_Line")]


def transitions(*, work_secs: float = 0.0) -> list[Transition]:
    """**T_Decaf_Pull** — pull the next decaf ticket, in plain arrival order among decaf ones.

    Demonstrates:
        A single input arc with a certified `filter` and **no `key`**, `count=1`, default
        `LEGACY` policy — the minimal shape that reproduces the filter-only cliff
        described in the module docstring. Nothing else on the transition (no guard, no
        `binding_priority_key`) so the cost measured is attributable to the arc alone.

    Args:
        work_secs: Physical seconds the station occupies a worker.
    """
    return [
        Transition(
            name="T_Decaf_Pull",
            inputs=[InputArc("P_Decaf_Line", filter=decaf_ticket, count=1)],
            outputs=[OutputArc("P_Served")],
            action=with_work(work_secs, serve_decaf),
            action_timeout_secs=0.5,
        )
    ]
