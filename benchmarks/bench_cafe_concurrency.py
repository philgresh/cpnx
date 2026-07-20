"""Benchmark: does `max_workers` actually make the Concurrency Cafe finish sooner?

The companion to ``bench_cafe_throughput.py``, asking the opposite question. That one
serializes everything on a logical clock to measure **engine CPU**; this one serializes nothing
on the wall clock to measure **makespan**. Neither can answer the other's question, and an
earlier revision of this suite learned that the expensive way: it swept ``max_workers`` against
the *logical* driver, found the result perfectly flat, and published it as evidence that
dispatch overhead dominates cpnx's parallelism. The conclusion was unfounded — that driver
awaits in-flight completion after every ``step()``, so at most one action is ever in flight and
the pool size cannot matter. The benchmark had never given the pool anything to parallelise.

Two things are required to make the question meaningful at all, and both are easy to get wrong:

1. **The actions must take real time.** cpnx runs transition actions as Python callables in a
   thread pool, so instant pure-Python actions are GIL-bound — a flat scaling curve would
   measure CPython, not cpnx. The cafe's ``work_secs`` makes each station sleep, and
   ``time.sleep`` releases the GIL, so the speedup on offer is real.
2. **The topology must admit concurrency.** With a single grinder the whole pipeline is fed by
   one transition gated at one firing per ``pacing_secs``, and nothing downstream can overlap.
   The two-group espresso machine, the two steam wands and ``grinders=2`` exist so there is
   genuine parallel work to find. ``pacing_secs`` is dropped well below the default here so the
   grinder is not the binding constraint.

What this is really testing
---------------------------
Guard evaluation happens **under the engine's single global lock**: ``step()`` runs
``_select_transition_to_fire()`` — which evaluates a guard per candidate binding — entirely
inside ``with self._lock``, and commit/deposit take the same lock. A callable guard is
dispatched through a ThreadPoolExecutor round-trip, so it holds that lock for microseconds per
candidate. The hypothesis is therefore:

    guard-free, the cafe should scale with max_workers; guarded, the speedup should collapse —
    not because of the GIL, but because the engine serialises the binding search.

That turns out to be true, but **only past a certain queue depth**, which is why this
benchmark sweeps ``N`` and not just workers. Guard cost is charged per *candidate binding*, so
lock-hold time per ``step()`` grows with the ticket line, while the interval each worker needs
serving in (``work_secs / workers``) does not. Below the crossover the guarded arm scales
essentially as well as the guard-free one; above it, added workers stop helping and then start
hurting. Measured at 60 orders the penalty looks minor (5.8x vs 6.9x at 8 workers) and it would
be easy to conclude there is no problem — at 300 orders the same code manages 1.7x against
6.9x, and going from 2 workers to 8 makes it *slower*. Sweep the depth before concluding
anything.

    python benchmarks/bench_cafe_concurrency.py
"""

import sys
from pathlib import Path

# Make ``src/`` (and this benchmarks/ dir, for ``_driver``) importable from a bare checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _driver import drive_saturating  # noqa: E402
from concurrency_cafe import build_cafe  # noqa: E402

from cpnx import Token  # noqa: E402

#: Ticket-line depths to sweep. Depth is the controlling variable: guard cost is charged per
#: candidate binding, so it grows with the queue while the per-worker service interval does
#: not. 60 sits below the crossover and 300 well above it, which is the whole point — a
#: single-depth run at 60 would suggest guards cost almost nothing in a concurrent net.
#: Kept small in absolute terms because every station sleeps: the serial floor is roughly
#: N * WORK_SECS * (stations per order).
ORDER_COUNTS = (60, 300)

#: Per-station physical work. Must sit well above the engine's own per-step cost or the
#: measurement is swamped by it, and well above the OS scheduler's timer granularity.
WORK_SECS = 0.005

#: Worker counts to sweep. 1 is the serial reference every speedup is computed against.
WORKER_COUNTS = (1, 2, 4, 8)

#: Below the default 8.0 so the grinder cooldown is not the binding constraint — otherwise
#: every configuration would be pinned at one drink per pacing_secs and the sweep would be
#: flat for a reason that has nothing to do with workers.
PACING_SECS = 0.0

#: Same deterministic dose mix as the throughput benchmark: 3 of every 10 tickets land outside
#: the [17, 19] tolerance band, so the guard actually rejects ~30% of candidates rather than
#: trivially accepting every one.
_DOSE_CYCLE = {3: 16, 6: 21, 9: 20}


def _order_payloads(n: int) -> list[dict]:
    return [
        {
            "ratio": "1:2",
            "weight_g": _DOSE_CYCLE.get(i % 10, 18),
            "dairy_free": (i % 2 == 0),
            "mobile_pickup": (i % 3 == 0),
        }
        for i in range(n)
    ]


def _run_once(n_orders: int, workers: int, dose_tolerance_g: float | None) -> float:
    """Drive one saturating run and return its wall-clock makespan in seconds."""
    # Channeling is off: a retry path would add nondeterministic work per run and make the
    # worker-to-worker comparison noisier without testing anything about concurrency.
    with build_cafe(
        pacing_secs=PACING_SECS,
        channel_failure_rate=0.0,
        max_workers=workers,
        dose_tolerance_g=dose_tolerance_g,
        work_secs=WORK_SECS,
        # The tray settle window is a *wall-clock* wait here (no logical clock is driving), so
        # leave it at zero — otherwise every serve pays it and it dominates the makespan.
        tray_settle_secs=0.0,
    ) as net:
        for payload in _order_payloads(n_orders):
            net.deposit("P_Ticket_Line", Token(payload=payload))
        return drive_saturating(net).wall_secs


def _sweep(label: str, dose_tolerance_g: float | None) -> None:
    print(f"{label} (work_secs={WORK_SECS}, saturating wall-clock driver):")
    for n_orders in ORDER_COUNTS:
        baseline: float | None = None
        cells = []
        for workers in WORKER_COUNTS:
            # Min of two: scheduler noise and CPU contention only ever *add* time, so the
            # minimum is the best estimator of the achievable makespan.
            secs = min(_run_once(n_orders, workers, dose_tolerance_g) for _ in range(2))
            if baseline is None:
                baseline = secs
            cells.append(f"w={workers}: {baseline / secs:5.2f}x")
        print(f"  orders={n_orders:<4} serial={baseline * 1e3:7.0f} ms   " + "  ".join(cells))
    print()


def main() -> None:
    """Sweep depth x workers, guard-free and guarded, and compare the two speedup surfaces.

    Read the *speedup* cells, not the milliseconds: the guarded arm does strictly more work per
    order (a guard per candidate binding, plus T_Rework_Dose firings for out-of-spec tickets),
    so its absolute times are not comparable with the guard-free arm. What is comparable is how
    each responds to more workers, and how that response changes with depth.

    The guard-free rows should stay flat across depths — near-linear scaling at every N. Any
    decay *there* is a problem with the measurement (machine under load, work_secs too small
    relative to engine cost), not a finding, so check it before reading the guarded rows.
    """
    _sweep("guard-free", None)
    _sweep("guarded (dose 17-19g)", 1.0)


if __name__ == "__main__":
    main()
