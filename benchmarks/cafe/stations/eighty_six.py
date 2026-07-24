"""🚫 The 86 board — a certified [`InputArc.key`][cpnx.InputArc] paired with an uncertified
[`InputArc.filter`][cpnx.InputArc].

Cafe role:
    "86 the lavender" — a syrup runs out mid-shift and goes up on the 86 board, so any
    ticket calling for it can't be made until the syrup is restocked. The board changes
    through the day as things sell out and get replenished, so nothing in the net can
    treat it as fixed at construction time.

Demonstrates:
    The **asymmetry in `PetriNet._ensure_key_index`**: certifying an arc's `key` buys
    nothing if the same arc's `filter` is uncertified. Indexing requires *both*
    callables to be certified, so one uncertified filter disqualifies the whole arc from
    the key-index path and it falls back to `_materialize_pool` route 3 — a full peek of
    every token in the place, followed by a per-firing filter-then-sort. That is exactly
    the cost profile the key would have had if it had never been certified at all.

    Crucially this is **not a dispatch-cost decision, it is a correctness one**. Read
    `PetriNet._ensure_key_index`'s docstring: a capped index read returns only the
    leading `cap` tokens in key order. Applying an uncertified filter *after* that read
    would be wrong — if the filter rejects every one of those `cap` tokens while an
    eligible token sits deeper in the index, the arc would silently under-select and the
    transition would report itself disabled even though a valid binding exists. There
    are only two correct arrangements: the filter runs at pop time so the scan can
    continue past rejected tokens (which requires the filter to be inline-safe), or the
    index is not consulted at all. There is no third option that lets a certified key
    partially help — which is why this station's `filter` disqualifies the arc even
    though its `key` alone would qualify.

    The user-facing shape of this bug is "I certified my key and got no speedup", and
    this station is that report's minimal reproducer. Its A/B partner is
    [`cafe.stations.batch_triage`][cafe.stations.batch_triage], which reuses the exact same key with no filter at all
    and *does* get indexed. Note also the selectivity interaction: because a rejected
    token stays in the index and the scan simply continues past it, a highly selective
    filter degrades this arc toward a full ordered walk of the place on every firing —
    so a *long* 86 board is worse for this station than a short one, on top of the
    baseline cost of not being indexed at all.
"""

from cafe.stations.batch_triage import batch_triage_key
from cafe.support import with_work
from cpnx import InputArc, OutputArc, Place, Token, Transition

#: The 86 board: syrup names currently sold out, keyed by nothing but their own presence.
#:
#: MUTABILITY IS LOAD-BEARING. [`not_86ed`][cafe.stations.eighty_six.not_86ed] closes over this set by reference, and
#: `cpnx.certification` rejects any callable that closes over external mutable state —
#: that rejection is the entire point of this station. Freezing this into a `frozenset`
#: module constant would make [`not_86ed`][cafe.stations.eighty_six.not_86ed] closed-world, certification would then
#: pass it, `_ensure_key_index` would index the arc, and the experiment this module exists to
#: demonstrate would silently vanish. Do not "clean this up" into an immutable constant.
_EIGHTY_SIX_BOARD: set[str] = set()


def not_86ed(token: Token) -> bool:
    """[`InputArc.filter`][cpnx.InputArc] for the 86 board queue: is this ticket's syrup still in stock?

    Cafe role:
        Rejects any ticket calling for a syrup currently up on the 86 board. The board
        is mutable through the shift — restocking a syrup should immediately let its
        tickets flow again, without rebuilding the net.

    Demonstrates:
        The **uncertified** half of this station's key/filter pair. It reads the
        module-level mutable `_EIGHTY_SIX_BOARD` set, which is exactly the kind of
        external mutable state `cpnx.certification` refuses to certify — so this
        function passes `verify_callable_purity` (it performs no I/O, so construction
        succeeds) but fails certification (so `PetriNet._ensure_key_index` cannot use
        it). Pairing it with the certified [`batch_triage_key`][cafe.stations.batch_triage.batch_triage_key] on the
        same arc is what proves a certified key alone cannot rescue an arc from an uncertified filter.
    """
    return token.payload.get("syrup") not in _EIGHTY_SIX_BOARD


def serve_eighty_six(tokens: list[Token]) -> list[Token]:
    """**T_Eighty_Six_Serve**'s action: hand an in-stock ticket straight out as a drink.

    Demonstrates:
        Minimal action, matching `batch_triage.serve_batch_triage`. This station exists
        to exercise arc-level indexing eligibility, not action machinery, so the action
        does the least possible work beyond marking the ticket served.
    """
    ticket = tokens[0]
    return [ticket.evolve(payload_updates={"stage": "drink"}, color="drink")]


def places() -> list[Place]:
    """The 86-board queue — an unbounded FIFO [`Place`][cpnx.Place], same shape as `P_Batch_Triage_Queue`.

    Cafe role:
        Holds tickets waiting on whatever syrup they need, regardless of whether that
        syrup is currently 86'd — the filter, not the place, is what withholds them.
    """
    return [Place("P_Eighty_Six_Queue")]


def transitions(*, work_secs: float = 0.0) -> list[Transition]:
    """**T_Eighty_Six_Serve** — pull the next in-stock ticket in triage order.

    Cafe role:
        Serves tickets in the same oat-before-dairy, on-spec-before-out-of-spec order
        as `T_Batch_Triage_Serve`, but skips anything currently 86'd.

    Demonstrates:
        A single input arc carrying both a certified `key`
        ([`batch_triage_key`][cafe.stations.batch_triage.batch_triage_key], reused unchanged from
        [`cafe.stations.batch_triage`][cafe.stations.batch_triage]) and an uncertified `filter`
        ([`not_86ed`][cafe.stations.eighty_six.not_86ed]). That combination is this station's whole point: see the
        module docstring for why the certified key cannot rescue the arc from the uncertified filter, and why that is a
        correctness requirement rather than a missed optimization.

    Args:
        work_secs: Physical seconds the station occupies a worker.
    """
    return [
        Transition(
            name="T_Eighty_Six_Serve",
            inputs=[InputArc("P_Eighty_Six_Queue", key=batch_triage_key, filter=not_86ed)],
            outputs=[OutputArc("P_Served")],
            action=with_work(work_secs, serve_eighty_six),
            action_timeout_secs=0.5,
        )
    ]
