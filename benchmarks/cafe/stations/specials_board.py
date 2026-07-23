"""🧾 The specials board — an [`InputArc.key`][cpnx.InputArc] that cannot be certified, and therefore cannot be indexed.

Cafe role:
    Mid-shift the lead retunes the priorities on a whiteboard behind the bar: today oat goes
    first, tomorrow maybe not. The barista does not memorise a fixed rule — every time they
    reach for the next ticket they glance up at the board and read whatever is written there
    right now.

Demonstrates:
    The **uncertified [`InputArc.key`][cpnx.InputArc] path**, deliberately paired against
    [`cafe.stations.batch_triage`][cafe.stations.batch_triage] as an A/B partner: same ordering, same single-arc
    topology, differing only in whether that ordering closes over mutable state.
    [`batch_triage_key`][cafe.stations.batch_triage.batch_triage_key] reads nothing but the token;
    [`specials_board_key`][cafe.stations.specials_board.specials_board_key] reads a module-level mutable dict, which
    `cpnx.certification` rejects as closed-world. That one difference changes everything about how the engine drains
    the queue:

    1. **No index, ever.** `_ensure_key_index` (`cpnx.engine`, ~1143-1170) refuses to build a
       key index unless `arc._key_inline_safe`. This is not a missed optimisation to fix later
       -- it is structurally impossible. Keying happens on the **deposit path**, and
       `deposit()` cannot block a producer waiting on the timeout-bounded expression executor
       just to place one token. So an uncertified key always falls to `_materialize_pool`'s
       route 3: `place.peek(len(place))` followed by a fresh filter-then-sort in
       `_order_available` (~1431) on *every single firing*. Draining a deep place this way is
       ≈O(N² log N), against ≈O(N log N) for the certified twin's persistent `(key, seq)` heap.
    2. **A per-token round trip.** Because the key is uncertified, each token's key is not
       computed inline -- it goes through `_call_expr`, one `ThreadPoolExecutor.submit` +
       `.result(timeout=...)` round trip *per token in the pool*, all while the engine's global
       lock is held. That round trip runs ~10 microseconds against a predicate that is itself
       ~0.09 microseconds -- dispatch, not computation, dominates.

    Note what does *not* bound this cost: `binding_search_limit` truncates the number of
    *candidate bindings* built in `_iter_candidate_bindings`, which only runs after this arc's
    per-token loop has already finished. Nothing bounds the per-token count, so the worst-case
    lock-held time for one firing of this arc is `len(place) * expr_timeout_secs` -- the whole
    pool, each token individually timeout-eligible.

    Because a per-token lock-held round trip is a far worse contention shape than a guard's
    per-*candidate* round trip (see `bench_enablement.py`), this station is also the right
    regime to drive through `bench_cafe_concurrency.py`: it stresses the engine lock under
    concurrent producers/consumers in a way `batch_triage`'s certified twin structurally
    cannot.
"""

from cafe.support import DOSE_TARGET_G, with_work
from cpnx import InputArc, OutputArc, Place, Token, Transition

#: The whiteboard. A shift lead's live priorities, keyed by the same two groupings
#: [`batch_triage_key`][cafe.stations.batch_triage.batch_triage_key] hardcodes: ``"milk_priority"`` (which milk group
#: sorts first) and ``"spec_priority"`` (which spec status sorts first). Defaults reproduce
#: [`batch_triage_key`][cafe.stations.batch_triage.batch_triage_key]'s fixed ordering exactly, so the two stations are
#: an apples-to-apples A/B pair.
#: ITS MUTABILITY IS LOAD-BEARING. Do not "tidy" this into a module-level constant tuple, and
#: do not inline these values into [`specials_board_key`][cafe.stations.specials_board.specials_board_key]. The entire
#: point of this station is that its key reads *external mutable state* -- that is precisely what
#: `cpnx.certification` refuses to certify, which is what forces the key onto the
#: uncertified/uninexed/per-token-round-trip path this module exists to demonstrate. Freezing
#: this dict would silently certify the key, hand it a key index for free, and quietly destroy
#: the experiment -- the benchmark would keep running and keep reporting numbers, just numbers
#: for a different (certified) code path than the one this station claims to measure.
_SPECIALS_BOARD: dict[str, int] = {
    "milk_priority": 0,  # oat-milk tickets (dairy_free) sort ahead of dairy when this is lower.
    "spec_priority": 0,  # on-target-dose tickets sort ahead of off-spec when this is lower.
}


def specials_board_key(token: Token) -> tuple[int, int, float]:
    """[`InputArc.key`][cpnx.InputArc] for the specials queue:
    [`batch_triage_key`][cafe.stations.batch_triage.batch_triage_key]'s ordering, read off a whiteboard.

    Cafe role:
        Computes the identical two-tier grouping as `batch_triage.batch_triage_key` -- oat
        before dairy, on-spec before out-of-spec, ties broken by arrival time -- but instead of
        hardcoding which side of each grouping sorts first, it looks up `_SPECIALS_BOARD` each
        time. The board can be repainted between firings (a real shift lead would), and the
        very next ticket read honours the new priorities immediately.

    Demonstrates:
        An **uncertified** per-token key: it reads only the token's own `payload` and
        `created_at`, so `verify_callable_purity` still passes (no I/O), but it also reads a
        module-level *mutable* dict, so `cpnx.certification.is_inline_safe` returns `False`.
        That single distinction is the entire experiment -- see the module docstring for what
        it costs.

    Args:
        token: The candidate ticket, as deposited on `P_Specials_Queue`.

    Returns:
        A 3-tuple ``(milk_priority_group, spec_priority_group, created_at)`` sorted ascending,
        identical in shape (and, for the default board, in value) to
        [`batch_triage_key`][cafe.stations.batch_triage.batch_triage_key]'s return.
    """
    dairy_free = bool(token.payload.get("dairy_free"))
    on_spec = token.payload.get("weight_g", DOSE_TARGET_G) == DOSE_TARGET_G
    milk_group = 0 if dairy_free else 1
    spec_group = 0 if on_spec else 1
    # XOR against the board's priority bits: priority 0 keeps the natural (0-first) ordering,
    # priority 1 flips which side of the grouping sorts first -- so the board can retune
    # priorities without this function ever needing an `if/else` per knob.
    return (
        milk_group ^ _SPECIALS_BOARD["milk_priority"],
        spec_group ^ _SPECIALS_BOARD["spec_priority"],
        token.created_at,
    )


def serve_specials_board(tokens: list[Token]) -> list[Token]:
    """**T_Specials_Serve**'s action: hand the board's next pick straight out as a drink.

    Demonstrates:
        The same deliberate minimalism as `batch_triage.serve_batch_triage` -- no
        grind/pull/steam machinery -- so this station's measurements stay isolated to the
        uncertified-key dispatch path, not diluted by unrelated pipeline work.
    """
    ticket = tokens[0]
    return [ticket.evolve(payload_updates={"stage": "drink"}, color="drink")]


def places() -> list[Place]:
    """The whiteboard queue -- an unbounded FIFO [`Place`][cpnx.Place], same shape as `P_Batch_Triage_Queue`.

    Cafe role:
        `P_Specials_Queue` holds the tickets waiting on the specials board's current
        priorities; nothing about the place itself differs from a plain ticket rail.

    Demonstrates:
        Structural symmetry with `batch_triage.places`: this station's cost lives entirely in
        how its arc's key is dispatched, not in any special place behaviour.

    Args:
    """
    return [Place("P_Specials_Queue")]


def transitions(*, work_secs: float = 0.0) -> list[Transition]:
    """**T_Specials_Serve** -- pull the board's next pick, one uncertified-key round trip at a time.

    Cafe role:
        The barista reads the specials board and pulls the next ticket it names.

    Demonstrates:
        A single input arc whose `key` is [`specials_board_key`][cafe.stations.specials_board.specials_board_key] --
        uncertified, so `_ensure_key_index` never indexes `P_Specials_Queue` and every firing re-materialises
        and re-sorts the whole available pool via `_order_available`, with each token's key
        dispatched through the timeout-bounded expression executor rather than evaluated
        inline. No guard, no filter, default `LEGACY` policy, `count=1` -- identical shape to
        `T_Batch_Triage_Serve`, so the only variable between the two stations is certification.

    Args:
        work_secs: Physical seconds the station occupies a worker.
    """
    return [
        Transition(
            name="T_Specials_Serve",
            inputs=[InputArc("P_Specials_Queue", key=specials_board_key)],
            outputs=[OutputArc("P_Served")],
            action=with_work(work_secs, serve_specials_board),
            action_timeout_secs=0.5,
        )
    ]
