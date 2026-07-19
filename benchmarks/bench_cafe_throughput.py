"""Benchmark: end-to-end throughput of the ☕ Concurrency Cafe topology.

A *macro* workload benchmark (complementing the per-call micro-benchmark in
``bench_enablement.py``). It processes N customer orders all the way through the real cafe net —
finite scales, the single grinder with its cooldown, the two-token order-tray rendezvous — and
reports how much engine CPU that costs.

The net keeps its real structural friction but runs on a **logical clock** (see ``_driver.py``),
so the grinder's 8-second cooldown blocks work (real back-pressure) without burning 8 real
seconds. Channeling failures are turned off (``channel_failure_rate=0.0``) so the run is
deterministic and avoids the wall-clock ``retry_delay`` path.

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

ORDER_COUNTS = (10, 100, 500)
WORKER_COUNTS = (1, 4)


def _order_payloads(n: int) -> list[dict]:
    """Deterministic mix of mobile/walk-in and oat/dairy orders."""
    return [
        {
            "ratio": "1:2",
            "weight_g": 18,
            "dairy_free": (i % 2 == 0),
            "mobile_pickup": (i % 3 == 0),
        }
        for i in range(n)
    ]


def _run_once(n_orders: int, max_workers: int) -> None:
    # Real friction (grinder cooldown, finite scales, tray threshold) but no random failures.
    with build_cafe(channel_failure_rate=0.0, max_workers=max_workers) as net:
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
    print("Concurrency Cafe throughput (logical clock, channeling off):")
    for workers in WORKER_COUNTS:
        for n in ORDER_COUNTS:
            _run_once(n, workers)
        print()


if __name__ == "__main__":
    main()
