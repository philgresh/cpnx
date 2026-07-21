"""Shared drivers for the cafe workload benchmarks.

Two drivers, two different questions. They are not interchangeable, and picking the wrong one
produces numbers that look fine and mean nothing (see the ``max_workers`` note below).

``drive_to_quiescence`` — **logical clock, deliberately serialized.** Answers "how much engine
CPU does this workload cost?". Why not just call ``PetriNet.run()``? ``run()`` waits out
cooldowns and settle windows on the *real* wall clock, so a net with an 8-second grinder
cooldown would take 8 real seconds per grind — you'd be timing ``time.sleep``, not the engine.
Instead we keep the topology's real friction (finite scales, the grinder cooldown, the
two-token tray threshold) but drive the net on its **logical clock**: fire everything enabled
*now*, let in-flight actions settle, then jump ``advance_time`` straight to the next
availability boundary. Blocking/back-pressure is fully preserved (the grinder is genuinely
unavailable for 8 *logical* seconds), but the waiting costs no wall-clock time — so the
measured wall time is the engine's CPU cost, which is what we can actually optimise.

``drive_saturating`` — **wall clock, deliberately concurrent.** Answers "does more workers make
this finish sooner?". Keeps up to ``max_workers`` actions in flight, so it measures makespan
rather than engine CPU. Requires actions that actually take time; against instant pure-Python
actions it measures nothing but the GIL.

Native stdlib only — no dependencies, no test runner.
"""

import time
from dataclasses import dataclass

from cpnx import PetriNet


@dataclass
class RunResult:
    """Outcome of driving a net to logical quiescence."""

    steps: int  #: number of successful ``step()`` firings
    ticks: int  #: number of logical-clock advances (cooldown/settle boundaries jumped)
    wall_secs: float  #: wall-clock seconds spent — dominated by engine CPU, not sleeping


def drive_to_quiescence(net: PetriNet, *, max_ticks: int = 1_000_000) -> RunResult:
    """Run ``net`` to logical quiescence and return timing/step counters.

    Delegates to ``PetriNet.drive_to_quiescence`` (the engine's own logical-clock driver, which
    this benchmark driver's loop was extracted into) and just wraps the wall-clock timing around
    the call, so the reported ``wall_secs`` is still the engine's CPU cost rather than time spent
    waiting on anything.
    """
    start = time.perf_counter()
    result = net.drive_to_quiescence(max_ticks=max_ticks)
    wall = time.perf_counter() - start
    return RunResult(steps=result.steps, ticks=result.ticks, wall_secs=wall)


def drive_saturating(net: PetriNet, *, max_secs: float = 300.0, poll_secs: float = 2e-4) -> RunResult:
    """Run ``net`` on the **wall** clock, keeping as many actions in flight as it will allow.

    The counterpart to ``drive_to_quiescence``: that one measures engine CPU by serializing
    everything, this one measures *makespan* by serializing nothing. ``step()`` returns as soon
    as it has submitted the action (it consumes inputs and submits under the engine lock, then
    returns), so simply not awaiting lets firings stack up to ``max_workers``.

    Deliberately does **not** touch ``advance_time``. Mixing a logical clock into a wall-clock
    run is where this gets subtly wrong: an in-flight failure's rollback reads the model clock to
    stamp the retried token's ``available_at``, so a clock jump racing a failure silently skips a
    retry delay. Nets driven here should express their friction in real time (small
    ``pacing_secs``, real ``work_secs``) rather than in logical time.

    Two caveats on the numbers this produces:

    - **The actions must actually take time.** cpnx actions run as Python callables in a thread
      pool, so instant pure-Python actions are GIL-bound and will show no speedup at any worker
      count — you would be measuring CPython, not cpnx. ``time.sleep`` releases the GIL, which is
      why the cafe models station work that way.
    - **Polling costs a core.** When nothing is enabled we spin rather than block, because the
      engine's own ``_work_available`` wait has 0.05 s granularity — far too coarse against
      millisecond actions. ``poll_secs`` trades measurement granularity against that cost.

    Args:
        max_secs: wall-clock ceiling, so a deadlocked net fails loudly instead of hanging.
        poll_secs: how long to sleep when no transition is currently enabled.

    Raises:
        RuntimeError: if ``max_secs`` elapses before the net reaches quiescence.
    """
    steps = 0
    start = time.perf_counter()
    while True:
        if net.step():
            steps += 1
            continue
        # Nothing fireable this instant. Either in-flight actions will enable more work when
        # they commit, or the net is genuinely done — `is_quiescent()` distinguishes the two
        # (it reports False while `_running_count > 0`).
        if net.is_quiescent():
            break
        if time.perf_counter() - start > max_secs:
            raise RuntimeError(f"drive_saturating exceeded max_secs={max_secs} after {steps} steps — net stuck?")
        time.sleep(poll_secs)
    wall = time.perf_counter() - start
    # `ticks` is meaningless here: this driver never advances the logical clock.
    return RunResult(steps=steps, ticks=0, wall_secs=wall)
