"""Dev tool: cProfile a large ☕ Concurrency Cafe run and rank the engine hotspots.

This is *not* a committed benchmark (its numbers are profiler-inflated and machine-specific) —
it is a pointer, run during development to see which engine functions dominate a realistic
workload before deciding what to optimise. It drives the same logical-clock cafe workload as
``bench_cafe_throughput.py``, under ``cProfile``, and prints the top functions by cumulative and
by total (own) time, filtered to ``cpnx`` frames.

    python benchmarks/profile_cafe.py [n_orders]

Native stdlib only.
"""

import cProfile
import io
import pstats
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _driver import drive_to_quiescence  # noqa: E402
from concurrency_cafe import build_cafe  # noqa: E402

from cpnx import Token  # noqa: E402

DEFAULT_ORDERS = 500


def _workload(n_orders: int) -> None:
    with build_cafe(channel_failure_rate=0.0, max_workers=1) as net:
        for i in range(n_orders):
            net.deposit(
                "P_Ticket_Line",
                Token(payload={"weight_g": 18, "dairy_free": i % 2 == 0, "mobile_pickup": i % 3 == 0}),
            )
        drive_to_quiescence(net)


_REPO_ROOT = str(Path(__file__).resolve().parent.parent) + "/"


def _print_top(profiler: cProfile.Profile, sort: str, label: str, limit: int = 12) -> None:
    buf = io.StringIO()
    # Keep full paths (no strip_dirs) so the regex can restrict to engine/library frames only.
    stats = pstats.Stats(profiler, stream=buf).sort_stats(sort)
    stats.print_stats("src/cpnx/", limit)  # library frames only, not the benchmark harness
    print(f"\n=== Top {limit} cpnx engine functions by {label} ===")
    for line in buf.getvalue().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("Ordered by", "List reduced")):
            continue
        # Shorten only the absolute path prefix; keep the numeric columns intact.
        print(line.replace(_REPO_ROOT, ""))


def main() -> None:
    n_orders = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ORDERS
    print(f"Profiling cafe workload: {n_orders} orders (logical clock, channeling off, workers=1)")

    profiler = cProfile.Profile()
    profiler.enable()
    _workload(n_orders)
    profiler.disable()

    _print_top(profiler, "cumulative", "cumulative time")
    _print_top(profiler, "tottime", "total (own) time")


if __name__ == "__main__":
    main()
