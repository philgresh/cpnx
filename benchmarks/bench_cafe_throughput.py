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
- ``max_workers`` — thread-pool dispatch overhead vs. parallelism for trivial actions.

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
WORKER_COUNTS = (1, 4)


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


def _run_once(n_orders: int, max_workers: int, dose_tolerance_g: float | None) -> None:
    # Real friction (grinder cooldown, finite scales, tray threshold) but no random failures.
    with build_cafe(
        channel_failure_rate=0.0, max_workers=max_workers, dose_tolerance_g=dose_tolerance_g
    ) as net:
        for payload in _order_payloads(n_orders):
            net.deposit("P_Ticket_Line", Token(payload=payload))

        result = drive_to_quiescence(net)

        served = net.places["P_Served"].stats()["absorbed"]
        us_per_order = result.wall_secs / n_orders * 1e6
        us_per_step = result.wall_secs / result.steps * 1e6 if result.steps else 0.0
        print(
            f"  orders={n_orders:<4} workers={max_workers}  "
            f"served={served:<4} steps={result.steps:<5} ticks={result.ticks:<4}  "
            f"{result.wall_secs * 1e3:8.2f} ms  "
            f"({us_per_order:8.1f} us/order, {us_per_step:6.2f} us/step)"
        )


def main() -> None:
    # Sweep both regimes: `None` leaves T_Weigh_And_Grind guard-free (and drops T_Rework_Dose),
    # 1.0 puts the [17, 19] dose guard in front of the PRIORITY candidate scan. Comparing the
    # two isolates the per-candidate guard-evaluation cost ADR 0001 calls the search's main
    # expense. Note the guarded run also does strictly more *work* (the extra T_Rework_Dose
    # firings), so compare us/step, not us/order, to read the guard's cost per candidate.
    for label, tolerance in (("guard-free", None), ("guarded (dose 17-19g)", 1.0)):
        print(f"Concurrency Cafe throughput — {label} (logical clock, channeling off):")
        for workers in WORKER_COUNTS:
            for n in ORDER_COUNTS:
                _run_once(n, workers, tolerance)
            print()


if __name__ == "__main__":
    main()
