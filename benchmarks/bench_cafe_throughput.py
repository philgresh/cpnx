"""Benchmark: end-to-end throughput of the ☕ Concurrency Cafe topology.

A *macro* workload benchmark (complementing the per-call micro-benchmark in
``bench_enablement.py``). It processes N customer orders all the way through the real cafe net —
finite scales, the single grinder with its cooldown, the two-token order-tray rendezvous — and
reports how much engine CPU that costs.

The net keeps its real structural friction but runs on a **logical clock** (see ``_driver.py``),
so the grinder's 8-second cooldown blocks work (real back-pressure) without burning 8 real
seconds. Channeling failures are turned off (``channel_failure_rate=0.0``) and the net is
seeded (``NET_SEED``), so step counts are byte-identical across runs and only wall-clock timing
varies.

Turning channeling off is *not* on its own enough to make the run deterministic, and an earlier
revision of this file claimed it was ("it draws no RNG"). It does: every cafe transition shares
the default ``priority``, so each ``step()`` breaks the tie with ``_rng.choice`` over the
enabled set. Unseeded that comes from OS entropy, and the step count wandered ~2% run to run —
enough that a us/step figure silently compared runs which had done different amounts of work.

Sweeps two knobs that drive engine cost:

- ``N`` orders — does per-order cost stay flat, or grow as tokens pile up in places?
- the binding regime — guard-free, guarded, and guarded-with-retries (see ``main``).

Deliberately **not** swept: ``max_workers``. ``_driver.drive_to_quiescence`` awaits in-flight
completion after every ``step()`` (it must, so an action's outputs can enable the next firing
before the clock advances), so at most one action is ever in flight and the pool size cannot
matter. An earlier revision swept it anyway and reported the flat result as evidence that
dispatch overhead dominates parallelism; that conclusion was unfounded — the benchmark simply
never gave the pool anything to parallelise. Measuring real concurrency needs a different
driver, not a different knob.

Two more regimes, added after the three above and swept separately at ``DEEP_ORDER_COUNTS``
(500/2000/20000 — deliberately deeper than ``ORDER_COUNTS`` tops out, since both regimes exist
specifically to show what happens as their place gets genuinely deep): ``cold-brew`` exercises
``concurrency_cafe.build_cafe(cold_brew=True)``'s deep *timed* place, and ``batch-triage``
exercises ``build_cafe(batch_triage=True)``'s only ``InputArc(expression=...)``. Both are
isolated single-station workloads (they deposit straight into the new place, bypassing
``P_Ticket_Line``/the grind-pull-steam pipeline entirely) so their numbers measure exactly the
new code path, uncontaminated by the rest of the topology. See ``concurrency_cafe.py``'s module
docstring for why these two shapes were otherwise absent from the net.

Run it on ``main`` and on a candidate branch and compare the reported ``us/order`` (report the
ratio, not the raw microseconds — see benchmarks/README.md). Native stdlib only.

    python benchmarks/bench_cafe_throughput.py
"""

import sys
import time
from pathlib import Path

# Make ``src/`` (and this benchmarks/ dir, for ``_driver``) importable from a bare checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _driver import drive_to_quiescence  # noqa: E402
from concurrency_cafe import build_cafe  # noqa: E402

from cpnx import Token  # noqa: E402

# 2000 deliberately exceeds the engine's binding_search_limit (default 1000): once the ticket
# line is deeper than that cap, the per-firing PRIORITY candidate scan saturates instead of
# growing, so us/step should flatten between 500 and 2000 rather than keep climbing.
ORDER_COUNTS = (10, 100, 500, 2000)

#: Seed for the channeling regime, so retry/dead-letter runs reproduce step-for-step.
CHANNEL_SEED = 99

#: Seed for the *net's own* RNG. Not optional for a benchmark: every cafe transition shares the
#: default `priority`, so each step() picks among enabled transitions with `_rng.choice`, and
#: unseeded that draw comes from OS entropy. The step count then wanders run to run (~2% over
#: 200 orders), which turns every us/step figure into a comparison between runs that did
#: different amounts of work. This file previously claimed the run "draws no RNG"; it does.
NET_SEED = 4242


# Dose sequence: 3 of every 10 tickets (indices 3, 6, 9 of each cycle) declare a weight
# outside the default [17, 19] tolerance band, so T_Weigh_And_Grind's guard actually rejects
# ~30% of candidates instead of trivially accepting every one (which would defeat the point
# of exercising the guarded path — see ADR 0001). Deterministic, no RNG, so the benchmark
# stays reproducible.
_DOSE_CYCLE = {3: 16, 6: 21, 9: 20}


def _order_payloads(n: int) -> list[dict]:
    """Deterministic mix of mobile/walk-in, oat/dairy, and in/out-of-spec-dose orders."""
    return [
        {
            "ratio": "1:2",
            "weight_g": _DOSE_CYCLE.get(i % 10, 18),
            "dairy_free": (i % 2 == 0),
            "mobile_pickup": (i % 3 == 0),
        }
        for i in range(n)
    ]


def _run_once(n_orders: int, dose_tolerance_g: float | None, channel_rate: float) -> None:
    # Real friction (grinder cooldown, finite scales, tray threshold) throughout; `channel_rate`
    # decides whether the retry/dead-letter path is exercised too.
    with build_cafe(
        channel_failure_rate=channel_rate,
        channel_seed=CHANNEL_SEED,
        seed=NET_SEED,
        max_workers=1,
        dose_tolerance_g=dose_tolerance_g,
    ) as net:
        for payload in _order_payloads(n_orders):
            net.deposit("P_Ticket_Line", Token(payload=payload))

        result = drive_to_quiescence(net)

        served = net.places["P_Served"].stats()["absorbed"]
        trashed = net.places["P_Trash_Can"].stats()["absorbed"]
        us_per_order = result.wall_secs / n_orders * 1e6
        us_per_step = result.wall_secs / result.steps * 1e6 if result.steps else 0.0
        print(
            f"  orders={n_orders:<4} served={served:<4} trashed={trashed:<3} "
            f"steps={result.steps:<5} ticks={result.ticks:<4}  "
            f"{result.wall_secs * 1e3:8.2f} ms  "
            f"({us_per_order:8.1f} us/order, {us_per_step:7.2f} us/step)"
        )


# Deliberately deeper than ORDER_COUNTS: both regimes below exist specifically to demonstrate
# what happens once their place is genuinely deep (dozens-to-hundreds of concurrently-steeping
# cold-brew batches; a thousands-deep triage backlog re-sorted every firing), so their sweep
# needs a scale ORDER_COUNTS never reaches.
DEEP_ORDER_COUNTS = (500, 2000, 20000)

# Cold brew realistically steeps 12-20 real hours — on a wall clock that would make this
# benchmark useless, but drive_to_quiescence's logical clock jumps straight to the next
# availability boundary (see benchmarks/_driver.py), so the wait costs no wall-clock time at
# all. What *does* cost real time is the engine re-scanning every still-steeping token's
# `available_at` on every retrieval and every clock-boundary lookup (see
# `Place.retrieve`/`PetriNet._earliest_cooldown_boundary`) — that linear scan, at depth, is
# exactly what this regime measures.
_STEEP_BASE_SECS = 12 * 3600
_STEEP_SPREAD_SECS = 8 * 3600
#: Distinct maturity times spread across the steep window. Coarser than "every token gets its
#: own timestamp" on purpose: with N buckets the clock only needs N boundary jumps to drain the
#: whole batch (each jump maturing ~n_orders/N tokens at once — a genuine cohort "co-steeping"),
#: instead of one jump per token.
_STEEP_BUCKETS = 40


def _cold_brew_tokens(n_orders: int, base_time: float) -> list[Token]:
    """`n_orders` cold-brew tokens, staggered across `_STEEP_BUCKETS` distinct future times.

    `base_time` is captured once by the caller (not re-read per token) so depositing 20000
    tokens doesn't smear the stagger against wall-clock drift accrued while building them.
    """
    spread_step = _STEEP_SPREAD_SECS / _STEEP_BUCKETS
    return [
        Token(
            color="cold_brew",
            payload={"batch": i},
            available_at=base_time + _STEEP_BASE_SECS + (i % _STEEP_BUCKETS) * spread_step,
        )
        for i in range(n_orders)
    ]


def _run_cold_brew(n_orders: int) -> None:
    """Stock `P_Cold_Brew_Steeping` with `n_orders` staggered-future tokens and drain it.

    Deposited directly into the steeping place (bypassing P_Ticket_Line and the grind/pull/
    steam pipeline entirely — see the module docstring), so this measures exactly the deep
    timed-place code path, nothing else.
    """
    with build_cafe(cold_brew=True, seed=NET_SEED, max_workers=1) as net:
        base_time = time.monotonic()
        for token in _cold_brew_tokens(n_orders, base_time):
            net.deposit("P_Cold_Brew_Steeping", token)

        # Every token was deposited future-dated before a single one could possibly have
        # matured, so this is the run's peak concurrent-steeping depth — retrieval can only
        # shrink it from here. No need to sample mid-drive.
        max_depth = len(net.places["P_Cold_Brew_Steeping"])

        result = drive_to_quiescence(net)

        served = net.places["P_Served"].stats()["absorbed"]
        us_per_order = result.wall_secs / n_orders * 1e6
        print(
            f"  orders={n_orders:<5} served={served:<5} max_steeping_depth={max_depth:<6} "
            f"ticks={result.ticks:<5}  "
            f"{result.wall_secs * 1e3:9.2f} ms  ({us_per_order:8.1f} us/order)"
        )


def _run_batch_triage(n_orders: int) -> None:
    """Stock `P_Batch_Triage_Queue` with `n_orders` tickets (deep, all at once) and drain it.

    Deposited directly into the triage queue (bypassing P_Ticket_Line entirely — see the
    module docstring), so this measures exactly the `InputArc.expression` code path re-sorting
    a deep pool every firing, nothing else. Records the order tokens actually reach `P_Served`
    in (via `on_token_deposited`) so the printed line can confirm the barista triage
    (`_batch_triage_order`) genuinely reordered them rather than passing FIFO through unchanged.
    """
    with build_cafe(batch_triage=True, seed=NET_SEED, max_workers=1) as net:
        served_order: list[int] = []

        def _record(place_name: str, token: Token) -> None:
            if place_name == "P_Served":
                served_order.append(token.payload["triage_idx"])

        net.on_token_deposited = _record

        for i, payload in enumerate(_order_payloads(n_orders)):
            net.deposit("P_Batch_Triage_Queue", Token(payload={**payload, "triage_idx": i}))
        max_depth = len(net.places["P_Batch_Triage_Queue"])  # all deposited before any drain

        result = drive_to_quiescence(net)

        served = net.places["P_Served"].stats()["absorbed"]
        reordered = served_order != list(range(n_orders))
        us_per_order = result.wall_secs / n_orders * 1e6
        print(
            f"  orders={n_orders:<5} served={served:<5} max_queue_depth={max_depth:<6} "
            f"reordered_vs_fifo={'yes' if reordered else 'NO (unexpected)':<16} "
            f"{result.wall_secs * 1e3:9.2f} ms  ({us_per_order:8.1f} us/order)"
        )


def main() -> None:
    """Sweep three binding regimes, all single-worker (see the module docstring).

    - guard-free: `dose_tolerance_g=None` drops the guard and `T_Rework_Dose` entirely.
    - guarded: the [17, 19] dose guard sits in front of the PRIORITY candidate scan, so the
      engine evaluates it once per *candidate binding* — ADR 0001's stated main expense.
    - guarded + channeling: adds the retry/dead-letter path. Only measurable on the logical
      clock since #20 made `retry_delay` model-clock-aware; before that a rolled-back token
      got a wall-clock deadline it could never reach and the run stranded.

    Compare us/step rather than us/order across regimes — each does a different amount of
    work per order (rework firings, retries), so us/order conflates cost with workload.
    """
    regimes = (
        ("guard-free", None, 0.0),
        ("guarded (dose 17-19g)", 1.0, 0.0),
        ("guarded + channeling (15%)", 1.0, 0.15),
    )
    for label, tolerance, channel_rate in regimes:
        print(f"Concurrency Cafe throughput — {label} (logical clock, workers=1):")
        for n in ORDER_COUNTS:
            _run_once(n, tolerance, channel_rate)
        print()

    # Two more regimes, each isolating one of the two shapes ORDER_COUNTS never exercised: a
    # genuinely deep *timed* place, and this net's only InputArc(expression=...). See the
    # module docstring and concurrency_cafe.py's for what each demonstrates and why.
    print("Concurrency Cafe throughput — cold-brew steeping tower (logical clock, workers=1):")
    for n in DEEP_ORDER_COUNTS:
        _run_cold_brew(n)
    print()

    print("Concurrency Cafe throughput — batch-triage (expression-ordered) (logical clock, workers=1):")
    for n in DEEP_ORDER_COUNTS:
        _run_batch_triage(n)
    print()


if __name__ == "__main__":
    main()
