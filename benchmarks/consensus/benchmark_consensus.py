"""Byzantine Fault Tolerant (BFT) consensus benchmark for ``cpnx``.

Ports the Ripple-style 80%-UNL consensus of Asare, Nana & Quist-Aphetsi,
*"Modeling and Simulation of a Blockchain Consensus for IoT Node Data Validation"*
(IJACSA 13.12, 2022 — https://thesai.org/Publications/ViewPaper?Volume=13&Issue=12&Code=IJACSA&SerialNo=4),
onto ``cpnx``'s real ``ThreadPoolExecutor`` engine to show that
one small net expresses three things academic, single-threaded CPN tools cannot do
concurrently:

1. **Massive fan-out** — one transaction is broadcast (cloned) to ``N_NODES`` validators.
2. **A fractional barrier** — the commit blocks until ``UNL_THRESHOLD`` positive votes
   accumulate, via a :class:`~cpnx.ThresholdPlace`.
3. **The straggler problem** — when the barrier releases, ``consume_all=True`` sweeps
   *every* accumulated vote in one atomic firing, so late votes (81–100) cannot leak into
   the next consensus round.

No user code writes a mutex, a ``queue.Full`` try/except, or an ``asyncio.gather`` — the
back-pressure, the barrier, and the sweep are all expressed as Petri-net structure.

Topology::

    mempool --T_broadcast(x100)--> node_inboxes --T_validate--> yes_votes ---.
                                                     |                        T_commit --> ledger
                                                     `--------> rejected_votes

Two execution phases (see :func:`run_throughput` and :func:`run_quiescence`):

* **Phase 1 — concurrent throughput.** 1,000 transactions through ``net.run()`` on a large
  worker pool, to measure raw engine throughput under fan-out + fan-in. Because a single
  shared ``yes_votes`` pool intermixes votes across the concurrent transactions and
  ``consume_all`` sweeps the whole pool, ledger commits do **not** equal the transaction
  count — this phase is a deliberate raw-throughput stress test, not per-transaction
  consensus. Phase 2 proves exact correctness.
* **Phase 2 — formal quiescence.** Exactly one transaction through
  ``net.drive_to_quiescence()`` (deterministic, single-threaded logical clock), printing the
  exact final marking as a reproducible fixed point.

Run it::

    python benchmarks/consensus/benchmark_consensus.py
"""

from __future__ import annotations

import argparse
import random
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# Make the in-tree ``cpnx`` importable when run directly, mirroring the other benchmarks.
sys.path.insert(0, str(_HERE.parent.parent / "src"))

from cpnx import (  # noqa: E402
    InputArc,
    OutputArc,
    PetriNet,
    Place,
    SinkPlace,
    ThresholdPlace,
    Token,
    Transition,
)

#: Size of a node's Unique Node List — every transaction fans out to this many validators.
N_NODES = 100
#: Positive-vote quorum required to commit (80% of the UNL — the paper's f <= (n-1)/5 bound).
UNL_THRESHOLD = 80

#: A generous wall-clock ceiling for Phase 1; a healthy net finishes far inside it.
_DEADLINE_SECS = 120.0

# --- Colours -----------------------------------------------------------------------------

YES = "YES"
NO = "NO"
COMMITTED = "COMMITTED"


# --- Decision functions (injected into T_validate) ---------------------------------------

Decider = Callable[[Token], bool]


def always_yes(_token: Token) -> bool:
    """Deterministic 100%-positive validation — every node accepts."""
    return True


def rate_yes(p_yes: float, rng: random.Random) -> Decider:
    """A validation decider that accepts with probability *p_yes*, using a seeded RNG.

    The returned callable is a closure over *rng*, so seeding *rng* makes the whole
    validation outcome reproducible under the deterministic driver. Used both by the
    benchmark (10% rejection default) and by the tests (forced rates).
    """

    def decide(_token: Token) -> bool:
        return rng.random() < p_yes

    return decide


def first_k_yes(k: int) -> Decider:
    """A deterministic decider that accepts exactly the first *k* validations, then rejects.

    Unlike :func:`rate_yes` (probabilistic — a 30% rate yields ~30, not exactly 30), this
    produces an exact YES/NO split, so a test can assert precise final counts. It is stateful
    (a call counter), so use it only under the single-threaded ``drive_to_quiescence`` driver,
    where firing order is deterministic.
    """
    counter = {"n": 0}

    def decide(_token: Token) -> bool:
        i = counter["n"]
        counter["n"] += 1
        return i < k

    return decide


# --- Topology builder --------------------------------------------------------------------


def _broadcast_action(tokens: list[Token]) -> list[Token]:
    """Clone the one consumed transaction into ``N_NODES`` per-validator copies.

    The engine deposits one action-returned token per ``OutputArc.count`` slot (it does not
    auto-clone), so the fan-out is produced *here*: each copy keeps the shared ``tx_id`` and
    is tagged with its ``node_id`` (and gets a fresh identity from :meth:`Token.evolve`).
    """
    tx = tokens[0]
    return [tx.evolve(payload_updates={"node_id": i}) for i in range(N_NODES)]


def _make_validate_action(decide: Decider, work_secs: float = 0.0) -> Callable[[list[Token]], list[Token]]:
    """Build the validation action, closing over the injected *decide* function.

    Returns exactly one token, coloured ``YES``/``NO``; ``OutputArc.on_color`` then routes it
    to ``yes_votes`` or ``rejected_votes``. Actions run on the pool and are not
    purity-verified, so a stochastic *decide* is allowed here.

    Args:
        decide: Per-node accept/reject decision.
        work_secs: Simulated per-validation cost (a real UNL node verifies a signature / HMAC).
            Modelled with ``time.sleep``, which releases the GIL — so this is what lets extra
            workers genuinely overlap validation latency, the concurrency the benchmark claims.
            ``0.0`` (the tests' default) makes validation instant and purely CPU/lock-bound.
    """

    def validate(tokens: list[Token]) -> list[Token]:
        tok = tokens[0]
        colour = YES if decide(tok) else NO
        if work_secs:
            time.sleep(work_secs)
        return [tok.evolve(color=colour)]

    return validate


def _commit_action(tokens: list[Token]) -> list[Token]:
    """Fold the swept batch of positive votes into a single committed ledger entry.

    ``tokens`` is the entire pool ``consume_all`` sweeps (>= ``UNL_THRESHOLD``); it collapses
    to one ledger token recording the winning ``tx_id`` and how many votes backed it.
    """
    winner = tokens[0]
    return [
        winner.evolve(
            payload_updates={"votes": len(tokens), "tx_id": winner.payload.get("tx_id")},
            color=COMMITTED,
        )
    ]


def build_consensus_net(
    decide: Decider,
    *,
    max_workers: int = 4,
    seed: int | None = None,
    eager_commit: bool = False,
    work_secs: float = 0.0,
) -> PetriNet:
    """Construct the 80%-UNL consensus net.

    Args:
        decide: Per-node validation decision function (see :func:`always_yes`, :func:`rate_yes`).
        max_workers: Size of the engine's action thread pool.
        seed: Optional net seed (affects tie-breaking among equal-priority transitions).
        eager_commit: Chooses the transition-priority regime, which trades straggler-cleanliness
            against throughput:

            * ``False`` (default) — **validate-first**: while any inbox token remains, ``step``
              prefers ``T_validate``, so under the deterministic driver *every* vote lands before
              the sweep fires → a clean, straggler-free commit. Used by Phase 2 and the tests.
            * ``True`` — **eager commit**: ``T_commit`` outranks ``T_validate``, so it fires the
              moment the 80-vote barrier is met instead of waiting for all 100 votes. This keeps
              the shared ``yes_votes`` pool small under a 1,000-tx flood, so ``consume_all``'s
              full-pool scan stays cheap and throughput does not collapse. Used by Phase 1.
              (It also means late votes leak as stragglers — irrelevant to a throughput stress
              test, and exactly why Phase 2 uses the other regime.)

    Returns:
        A wired :class:`~cpnx.PetriNet`. Places: ``mempool``, ``node_inboxes``, ``yes_votes``
        (a :class:`~cpnx.ThresholdPlace`), ``rejected_votes`` and ``ledger`` (sinks).
    """
    # Lower number = higher priority. Broadcast always fires last (it only refills work), so the
    # pipeline drains before more is pulled from the mempool. The validate/commit order flips
    # with `eager_commit` (see above).
    broadcast_priority = 20
    validate_priority = 10
    commit_priority = 0 if eager_commit else 15
    places = [
        Place("mempool"),
        Place("node_inboxes"),
        ThresholdPlace("yes_votes", threshold=UNL_THRESHOLD),
        SinkPlace("rejected_votes"),
        SinkPlace("ledger", keep_last=8),
    ]

    transitions = [
        Transition(
            name="T_broadcast",
            inputs=[InputArc("mempool")],
            outputs=[OutputArc("node_inboxes", count=N_NODES)],
            action=_broadcast_action,
            priority=broadcast_priority,
        ),
        Transition(
            name="T_validate",
            inputs=[InputArc("node_inboxes")],
            outputs=[
                OutputArc.on_color(YES, "yes_votes"),
                OutputArc.on_color(NO, "rejected_votes"),
            ],
            action=_make_validate_action(decide, work_secs=work_secs),
            priority=validate_priority,
        ),
        Transition(
            name="T_commit",
            # The fractional barrier: ThresholdPlace gates enablement at 80, and consume_all
            # then sweeps the *entire* accumulated pool in one firing (81–100 included), so no
            # straggler survives into the next round.
            inputs=[InputArc("yes_votes", count=UNL_THRESHOLD, consume_all=True)],
            outputs=[OutputArc("ledger", count=1)],
            action=_commit_action,
            priority=commit_priority,
        ),
    ]

    return PetriNet(places=places, transitions=transitions, max_workers=max_workers, seed=seed)


def _install_firing_counter(net: PetriNet) -> Counter:
    """Attach a thread-safe per-transition firing counter via ``on_transition_fired``.

    ``on_transition_fired`` fires once per successful firing, including internal transitions,
    so the counts give exact fan-out/validation/commit totals even under concurrent
    ``run()``.
    """
    counts: Counter = Counter()
    lock = threading.Lock()

    def _count(name: str, _duration: float) -> None:
        with lock:
            counts[name] += 1

    net.on_transition_fired = _count
    return counts


def _deposit_transactions(net: PetriNet, n_tx: int) -> None:
    """Deposit *n_tx* fresh transactions into the mempool, each carrying a unique ``tx_id``."""
    for i in range(n_tx):
        net.deposit("mempool", Token(payload={"tx_id": i}))


# --- Phase 1: concurrent throughput ------------------------------------------------------


def _time_one_throughput_run(
    n_tx: int, workers: int, p_yes: float, work_secs: float, seed: int
) -> tuple[float, Counter]:
    """One Phase-1 run at a given worker count; return ``(wall_secs, firing_counts)``.

    A fresh net and a freshly-seeded RNG each call, so the validation stream is identical
    across worker counts and repeat trials — only the concurrency (and scheduler noise)
    varies, keeping the throughput comparison apples-to-apples.
    """
    net = build_consensus_net(
        rate_yes(p_yes, random.Random(seed)),
        max_workers=workers,
        seed=seed,
        eager_commit=True,
        work_secs=work_secs,
    )
    counts = _install_firing_counter(net)
    with net:
        _deposit_transactions(net, n_tx)
        start = time.perf_counter()
        net.run(deadline=time.monotonic() + _DEADLINE_SECS)
        wall = time.perf_counter() - start
    return wall, counts


def run_throughput(
    n_tx: int = 1000,
    worker_counts: tuple[int, ...] = (4, 8, 16, 32),
    *,
    p_yes: float = 0.90,
    work_secs: float = 0.0005,
    seed: int = 20260726,
    trials: int = 3,
) -> None:
    """Phase 1 — drive *n_tx* transactions concurrently through ``net.run()`` and report throughput.

    Sweeps ``max_workers`` to show the pool overlapping validation latency. Each validation
    simulates *work_secs* of signature-verification cost (via ``time.sleep``, which releases
    the GIL), so adding workers overlaps that latency. Reports validations/sec, commits/sec and
    total-tokens/sec.

    Each worker count is run *trials* times and the **best (min) wall time** is reported — the
    standard defence against scheduler noise, which is severe once the worker count exceeds the
    machine's physical cores. Read the curve as a *shape*, not absolute figures: throughput
    rises with workers up to the useful concurrency, then **saturates and can regress** as
    contention on the engine's single lock — every worker funnels its output commit through it —
    outweighs added parallelism. That saturation point is a property of the machine *and* the
    executor-based design, not a defect in the net.

    Note (documented simplification): the single shared ``yes_votes`` pool intermixes votes
    across transactions and ``consume_all`` sweeps the whole pool, so ``commits != n_tx``; this
    measures engine throughput, not per-transaction consensus (see :func:`run_quiescence`).
    """
    print(f"\n=== Phase 1 — concurrent throughput: {n_tx} transactions, {int(p_yes * 100)}% YES ===")
    print(f"Each validation simulates {work_secs * 1e3:.1f} ms of signature-verification work.")
    print(f"Best of {trials} trials per worker count (min wall); votes pool across txs by design,")
    print("so `commits` is the number of 80-vote batches swept, not the transaction count.\n")
    print(f"{'workers':>8} {'wall (s)':>10} {'validations/s':>15} {'commits/s':>12} {'tokens/s':>13} {'speedup':>9}")
    print("-" * 72)

    baseline_wall: float | None = None
    for workers in worker_counts:
        # Best-of-`trials`: keep the fastest run's wall and its firing counts together.
        wall, counts = min(
            (_time_one_throughput_run(n_tx, workers, p_yes, work_secs, seed) for _ in range(trials)),
            key=lambda wc: wc[0],
        )
        validations = counts["T_validate"]
        commits = counts["T_commit"]
        # Tokens moved ~= one output token per firing plus the fan-out amplification.
        tokens_moved = validations + commits + counts["T_broadcast"] * N_NODES
        if baseline_wall is None:
            baseline_wall = wall
        speedup = baseline_wall / wall
        print(
            f"{workers:>8} {wall:>10.3f} {validations / wall:>15,.0f} "
            f"{commits / wall:>12,.1f} {tokens_moved / wall:>13,.0f} {speedup:>8.2f}x"
        )

    print("\n(Throughput rises with workers as the pool overlaps validation latency, then saturates")
    print(" — and past the useful concurrency can regress as contention on the engine's single lock")
    print(" outweighs added parallelism. All under real threads with no user-written locks: the net")
    print(" fans out (1->100) and fans in (>=80->1) throughout.)")


# --- Phase 2: formal quiescence ----------------------------------------------------------


def run_quiescence(*, seed: int = 20260726) -> dict:
    """Phase 2 — one transaction through ``net.drive_to_quiescence()``; print the exact marking.

    Uses a seeded 90%-YES decider, so ~90 positive votes accumulate, the 80-vote barrier
    releases, and ``consume_all`` sweeps the batch into a single ledger commit. The logical
    driver keeps at most one action in flight, so the reported final marking is a true,
    reproducible fixed point.
    """
    print("\n=== Phase 2 — formal quiescence: 1 transaction, deterministic driver ===")
    net = build_consensus_net(rate_yes(0.90, random.Random(seed)), max_workers=1, seed=seed)
    counts = _install_firing_counter(net)
    with net:
        _deposit_transactions(net, 1)
        start = time.perf_counter()
        result = net.drive_to_quiescence()
        wall = time.perf_counter() - start

        ledger = net.places["ledger"].stats()
        rejected = net.places["rejected_votes"].stats()
        marking = {
            "ledger": ledger["absorbed"],
            "rejected_votes": rejected["absorbed"],
            "yes_votes": len(net.places["yes_votes"]),
            "node_inboxes": len(net.places["node_inboxes"]),
            "mempool": len(net.places["mempool"]),
            "votes_in_commit": (net.places["ledger"].tokens[-1].payload.get("votes") if ledger["absorbed"] else 0),
        }

    print(f"  drove to quiescence in {wall * 1e3:.1f} ms ({result.steps} firings, {result.ticks} clock ticks)")
    print(f"  firings: {dict(counts)}")
    print("  final marking (exact fixed point):")
    for name, value in marking.items():
        print(f"    {name:>16} = {value}")
    print(
        f"  => 1 tx fanned out to {N_NODES} validators; {marking['votes_in_commit']} YES votes swept into "
        f"{marking['ledger']} commit, {marking['rejected_votes']} rejected, {marking['yes_votes']} stragglers left."
    )
    return marking


def main() -> None:
    """CLI entry point: parse args, then run Phase 1 (throughput) and Phase 2 (quiescence)."""
    parser = argparse.ArgumentParser(description="cpnx BFT (80% UNL) consensus benchmark")
    parser.add_argument("--n-tx", type=int, default=1000, help="Phase 1 transaction count (default: 1000)")
    parser.add_argument(
        "--workers",
        type=int,
        nargs="+",
        default=[4, 8, 16, 32],
        help="Phase 1 max_workers sweep (default: 4 8 16 32)",
    )
    parser.add_argument("--seed", type=int, default=20260726, help="RNG / net seed (default: 20260726)")
    args = parser.parse_args()

    print(f"⛓️  cpnx BFT consensus benchmark — N_NODES={N_NODES}, UNL_THRESHOLD={UNL_THRESHOLD}, seed={args.seed}")
    run_throughput(n_tx=args.n_tx, worker_counts=tuple(args.workers), seed=args.seed)
    run_quiescence(seed=args.seed)


if __name__ == "__main__":
    main()
