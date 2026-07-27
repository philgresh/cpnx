"""Tests for the Ripple-style 80%-UNL BFT consensus benchmark.

These tests drive ``build_consensus_net`` (from ``benchmarks/consensus/benchmark_consensus.py``)
under the deterministic single-threaded ``drive_to_quiescence`` runner, and assert on the exact
final marking. Together they prove three CPN-semantic properties the benchmark's docstring
claims:

1. **Massive fan-out** — one deposited transaction becomes ``N_NODES`` validations.
2. **A fractional barrier** — ``T_commit`` cannot fire until ``UNL_THRESHOLD`` positive votes
   have accumulated in the ``ThresholdPlace``.
3. **Straggler-freedom** — once the barrier releases, ``consume_all=True`` sweeps the *entire*
   accumulated vote pool in one atomic firing, so no vote is left behind under the default
   (validate-first) priority regime.
"""

import random
import sys
from pathlib import Path

from cpnx import Token
from cpnx.places import SinkPlace, ThresholdPlace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks" / "consensus"))

from benchmark_consensus import (  # noqa: E402
    N_NODES,
    UNL_THRESHOLD,
    always_yes,
    build_consensus_net,
    first_k_yes,
    rate_yes,
)


def test_successful_consensus():
    """100% YES votes: all 100 votes are swept into one commit, leaving zero stragglers."""
    net = build_consensus_net(always_yes, max_workers=1)
    with net:
        net.deposit("mempool", Token(payload={"tx_id": 0}))
        net.drive_to_quiescence()

        assert net.places["ledger"].stats()["absorbed"] == 1
        # consume_all swept the FULL pool (all 100 YES votes), not just the 80-vote threshold.
        assert len(net.places["yes_votes"]) == 0
        assert net.places["rejected_votes"].stats()["absorbed"] == 0


def test_failed_consensus_blocks_commit():
    """Only 30 of 100 votes are YES: the 80-vote barrier never opens, so T_commit never fires."""
    net = build_consensus_net(first_k_yes(30), max_workers=1)
    with net:
        net.deposit("mempool", Token(payload={"tx_id": 0}))
        net.drive_to_quiescence()

        assert net.places["ledger"].stats()["absorbed"] == 0
        assert net.places["rejected_votes"].stats()["absorbed"] == 70
        # Stuck below UNL_THRESHOLD: the fractional barrier is doing its job.
        assert len(net.places["yes_votes"]) == 30
        assert net.is_dead()


def test_threshold_boundary():
    """79 YES votes stay stuck below the barrier; exactly 80 releases it (the fractional edge)."""
    net_below = build_consensus_net(first_k_yes(UNL_THRESHOLD - 1), max_workers=1)
    with net_below:
        net_below.deposit("mempool", Token(payload={"tx_id": 0}))
        net_below.drive_to_quiescence()

        assert net_below.places["ledger"].stats()["absorbed"] == 0
        assert len(net_below.places["yes_votes"]) == UNL_THRESHOLD - 1
        assert net_below.is_dead()

    net_at = build_consensus_net(first_k_yes(UNL_THRESHOLD), max_workers=1)
    with net_at:
        net_at.deposit("mempool", Token(payload={"tx_id": 0}))
        net_at.drive_to_quiescence()

        assert net_at.places["ledger"].stats()["absorbed"] == 1
        # Exactly at the barrier: consume_all sweeps all 80, leaving nothing behind.
        assert len(net_at.places["yes_votes"]) == 0


def test_builder_topology():
    """Sanity-check the wired net's shape: place types, threshold, arc semantics, priorities."""
    net = build_consensus_net(always_yes, max_workers=1)

    assert isinstance(net.places["yes_votes"], ThresholdPlace)
    assert net.places["yes_votes"].threshold == UNL_THRESHOLD
    assert isinstance(net.places["rejected_votes"], SinkPlace)
    assert isinstance(net.places["ledger"], SinkPlace)
    for name in ("mempool", "node_inboxes", "yes_votes", "rejected_votes", "ledger"):
        assert name in net.places

    t_commit = net.transitions["T_commit"]
    commit_arc = t_commit.inputs[0]
    assert commit_arc.consume_all is True
    assert commit_arc.count == UNL_THRESHOLD

    t_validate = net.transitions["T_validate"]
    t_broadcast = net.transitions["T_broadcast"]
    # Default (eager_commit=False): validate-first, so every vote lands before the sweep fires.
    assert t_validate.priority < t_commit.priority
    # Broadcast fires last among the three (only refills work, so the pipeline drains first).
    assert t_broadcast.priority > t_commit.priority
    assert t_broadcast.priority > t_validate.priority


def test_broadcast_fans_out():
    """One transaction fans out to exactly N_NODES validations (the commit's own vote tally)."""
    net = build_consensus_net(always_yes, max_workers=1)
    with net:
        net.deposit("mempool", Token(payload={"tx_id": 0}))
        net.drive_to_quiescence()

        ledger = net.places["ledger"]
        assert ledger.stats()["absorbed"] == 1
        assert ledger.tokens[-1].payload["votes"] == N_NODES


def test_validate_routes_by_color():
    """T_validate's on_color output arcs correctly split YES/NO tokens into separate places."""
    net = build_consensus_net(first_k_yes(50), max_workers=1)
    with net:
        net.deposit("mempool", Token(payload={"tx_id": 0}))
        net.drive_to_quiescence()

        assert net.places["rejected_votes"].stats()["absorbed"] == 50
        assert len(net.places["yes_votes"]) == 50
        assert net.places["ledger"].stats()["absorbed"] == 0


def test_determinism():
    """Two independently built nets with the same seed and decider reach the same fixed point."""
    seed = 20260726

    def run_once():
        net = build_consensus_net(rate_yes(0.9, random.Random(seed)), max_workers=1, seed=seed)
        with net:
            net.deposit("mempool", Token(payload={"tx_id": 0}))
            net.drive_to_quiescence()
            return (
                net.places["ledger"].stats()["absorbed"],
                net.places["rejected_votes"].stats()["absorbed"],
            )

    assert run_once() == run_once()


def test_commit_preserves_tx_identity():
    """The committed ledger token still carries the original transaction's tx_id."""
    net = build_consensus_net(always_yes, max_workers=1)
    with net:
        net.deposit("mempool", Token(payload={"tx_id": 42}))
        net.drive_to_quiescence()

        assert net.places["ledger"].tokens[-1].payload["tx_id"] == 42
