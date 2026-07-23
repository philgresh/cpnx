"""🥄 The cupping table — a certified keyed arc at `count > 1`, searched under `PRIORITY`.

Cafe role:
    Green-coffee samples pile up on a bench for quality control. The roaster cups them in
    *flights* — several cups tasted side by side in one sitting, scored together against
    each other rather than one at a time. A flight is only meaningful if the cups are
    comparable, so every cup in a flight must share an origin; cupping an Ethiopian sample
    next to a Colombian one tells the roaster nothing. Within a flight, higher-scoring
    roasts and older samples (waiting longest for a verdict) are preferred.

Demonstrates:
    **This station is not about the `_materialize_pool` route-3 fallback** —
    [`cafe.stations.specials_board`][cafe.stations.specials_board] and [`cafe.stations.decaf`][cafe.stations.decaf]
    already cover that ground, and this station's key is fully certified, so it never goes near route 3.
    [`cup_score`][cafe.stations.cupping.cup_score] is pure and closes over nothing mutable, so `_ensure_key_index`
    (`cpnx.engine`) happily builds the persistent `(key, seq)` heap for it even at `count=4` — the token *pool* read
    off that heap stays bounded to `binding_search_limit + arc.count`, exactly as
    `_gather_arc_pools` documents. Reading that cap short would silently truncate the
    candidate set, not merely cost time; this station's whole point is showing what still
    happens even when it is read correctly.

    What actually blows up here is the **candidate space**, not the pool scan. `_arc_options`
    (`cpnx.engine`) yields every `count`-sized combination of the ordered pool — `C(pool,
    count)` groups — and `_iter_candidate_bindings` truncates each arc's option stream to
    `binding_search_limit + 1` groups before handing it to `itertools.product`. At `count=1`
    the arc yields one option per token, so that cap collapses to the familiar `limit + 1`.
    At `count=4` the `(limit + 1)`-th combination reaches all the way to index
    `limit + count - 1` in the pool ordering — which is exactly why `_gather_arc_pools` caps
    the pool read at `binding_search_limit + arc.count` rather than `binding_search_limit +
    1`: the candidate truncation reaches further into the pool than the combination count
    alone would suggest.

    [`same_origin`][cafe.stations.cupping.same_origin] is evaluated once per *candidate binding*, so raising `count`
    multiplies how many guard evaluations sit behind a single firing — `C(pool, count)` of them, capped
    by the search limit. `binding_policy=BindingPolicy.PRIORITY` forces the engine to
    actually enumerate that space (searching for the min-[`cup_score`][cafe.stations.cupping.cup_score] satisfying
    flight) rather than accepting the head group the way `LEGACY`/guard-free `FIRST` would. If the
    bench holds mixed origins and every satisfying combination happens to live beyond the
    first `binding_search_limit + 1` candidates the truncated prefix covers, the transition
    reads as **disabled** for that check — a stall reachable purely from candidate-space
    truncation, with a correctly-sized, correctly-indexed pool sitting right there un-scanned
    past the prefix. `on_binding_search_exhausted` is the signal to watch for it.

    Recommend sweeping `count` in `{1, 2, 4, 8}` against several queue depths, watching where
    `on_binding_search_exhausted` starts firing and where flights stop forming despite a
    valid same-origin flight existing deeper in the bench than the search looked.
"""

from cafe.support import with_work
from cpnx import BindingPolicy, InputArc, OutputArc, Place, Token, Transition


def cup_score(token: Token) -> tuple[float, float]:
    """[`InputArc.key`][cpnx.InputArc] for the cupping bench: which sample gets tasted next.

    Cafe role:
        Higher roast scores earn a slot first — the roaster wants strong candidates
        confirmed early in the session, while the palate is freshest. Among samples
        scoring equally, the one that has waited longest on the bench goes first, so
        nothing sits indefinitely while newer arrivals keep cutting the line.

    Demonstrates:
        A **certified per-token key** used at `count > 1`. It reads only the token's own
        `payload` and `created_at` and closes over nothing mutable, so
        `cpnx.certification` proves it closed-world and `_ensure_key_index` (`cpnx.engine`)
        indexes it with the persistent `(key, seq)` min-heap — the same fast path
        [`cafe.stations.batch_triage.batch_triage_key`][cafe.stations.batch_triage.batch_triage_key] demonstrates at
        `count=1`. This module exists to show that certification alone does not make a keyed, guarded,
        `count > 1` search cheap: the pool this key orders is bounded and cheap to read,
        but the *combinations* `_arc_options` builds over that ordered pool are not.
    """
    return (-token.payload.get("roast_score", 0.0), token.created_at)


def same_origin(tokens: list[Token]) -> bool:
    """**T_Cupping_Flight**'s guard: every cup in the flight must be comparable.

    Cafe role:
        Cupping is a side-by-side comparison. Scoring an Ethiopian sample against a
        Colombian one in the same flight produces a meaningless number — the guard is the
        roaster's rule that a flight only forms when every cup on the tray shares a
        single-origin lot.

    Demonstrates:
        A **certified guard evaluated per candidate binding**, under
        [`BindingPolicy.PRIORITY`][cpnx.BindingPolicy]. It reads only each bound token's `payload["origin"]`,
        so `cpnx.certification` proves it closed-world and the engine runs it inline under
        the lock rather than round-tripping it through the timeout-bounded executor. What
        it costs is not the per-evaluation price — it is *how many* evaluations happen:
        one per candidate `count`-sized combination `_iter_candidate_bindings` yields, up
        to `binding_search_limit + 1` of them. A bench with several origins mixed together
        forces the search to actually walk combinations looking for one that satisfies
        this guard, rather than accepting the head group outright — the genuine search
        this station exists to exercise.
    """
    origins = {t.payload.get("origin") for t in tokens}
    return len(origins) == 1


def score_flight(tokens: list[Token]) -> list[Token]:
    """**T_Cupping_Flight**'s action: record one score for the flight.

    Cafe role:
        The roaster tastes every cup in the flight and writes down a single verdict for
        the lot, rather than a per-cup note — cupping judges the flight as a group.

    Demonstrates:
        Deliberate **minimalism as experimental hygiene**, matching
        [`cafe.stations.batch_triage.serve_batch_triage`][cafe.stations.batch_triage.serve_batch_triage]: this station
        exists to exercise the keyed-arc/guard/`PRIORITY` candidate-space search, and re-modelling any real
        scoring logic here would put unrelated engine work in the measurement. It reports
        the count of cups actually tasted, which lets a caller confirm `count` samples
        were bound (not merely that the shared origin held) without inspecting the raw
        binding.
    """
    origin = tokens[0].payload.get("origin")
    return [
        Token(
            payload={"origin": origin, "cups_tasted": len(tokens)},
            color="cupping_score",
        )
    ]


def places() -> list[Place]:
    """The bench — an unbounded, unordered [`Place`][cpnx.Place] holding green-coffee samples.

    Cafe role:
        Where samples sit until a flight is called. Nothing about the place itself
        enforces same-origin grouping; that constraint lives entirely in
        `T_Cupping_Flight`'s guard, so the bench can hold as many mixed origins at once
        as a real cupping session would.

    Demonstrates:
        A plain [`Place`][cpnx.Place], same shape as every other cafe queue — the interesting engine
        behavior in this station lives in the arc's `key`/`count` and the transition's
        guard/`binding_policy`, not in the place.

    Args:

    Returns:
        A single-element list containing `P_Sample_Queue`.
    """
    return [Place("P_Sample_Queue")]


def transitions(*, work_secs: float = 0.0, count: int = 4) -> list[Transition]:
    """**T_Cupping_Flight** — cup `count` same-origin samples together and score the flight.

    Cafe role:
        The roaster calls a flight: pull `count` samples off the bench, all one origin,
        taste them side by side, write down one score. Raising `count` models a larger
        cupping table (more cups tasted per sitting); a deeper, more mixed-origin bench
        models a busier QC queue.

    Demonstrates:
        The full combination this station exists to isolate: a **certified [`InputArc.key`][cpnx.InputArc]
        at `count > 1`**, gating a **certified guard evaluated per candidate binding**,
        under `binding_policy=BindingPolicy.PRIORITY` so the engine actually enumerates
        rather than accepting the head group. See the module docstring for why this is a
        candidate-space cost, not a pool-scan one, and for the sweep this station is meant
        to drive.

    Args:
        work_secs: Physical seconds the station occupies a worker.
        count: Number of samples per flight. Defaults to 4. Sweeping this against queue
            depth and origin mix is what exposes the candidate-space truncation described
            in the module docstring.

    Returns:
        A single-element list containing `T_Cupping_Flight`.
    """
    return [
        Transition(
            name="T_Cupping_Flight",
            inputs=[InputArc("P_Sample_Queue", count=count, key=cup_score)],
            outputs=[OutputArc("P_Served")],
            guard=same_origin,
            binding_policy=BindingPolicy.PRIORITY,
            action=with_work(work_secs, score_flight),
            action_timeout_secs=0.5,
        )
    ]
