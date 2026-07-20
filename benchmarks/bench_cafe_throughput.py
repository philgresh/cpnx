"""Benchmark: end-to-end throughput of the ☕ Concurrency Cafe topology.

A *macro* workload benchmark (complementing the per-call micro-benchmark in
``bench_enablement.py``). It processes N customer orders all the way through the real cafe net —
finite scales, the single grinder with its cooldown, the two-token order-tray rendezvous — and
reports how much engine CPU that costs.

The net keeps its real structural friction but runs on a **logical clock** (see ``_driver.py``),
so the grinder's 8-second cooldown blocks work (real back-pressure) without burning 8 real
seconds. Channeling failures are turned off (``channel_failure_rate=0.0``) so the run is
deterministic — it draws no RNG, so step counts are byte-identical across runs and only
wall-clock timing varies.

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

Run it on ``main`` and on a candidate branch and compare the reported ``us/order`` (report the
ratio, not the raw microseconds — see benchmarks/README.md). Native stdlib only.

    python benchmarks/bench_cafe_throughput.py
"""

import sys
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


if __name__ == "__main__":
    main()
