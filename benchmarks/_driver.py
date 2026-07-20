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

from cpnx import PetriNet, SinkPlace


@dataclass
class RunResult:
    """Outcome of driving a net to logical quiescence."""

    steps: int  #: number of successful ``step()`` firings
    ticks: int  #: number of logical-clock advances (cooldown/settle boundaries jumped)
    wall_secs: float  #: wall-clock seconds spent — dominated by engine CPU, not sleeping


def _next_boundary(net: PetriNet) -> float | None:
    """Smallest future logical time at which a currently-blocked token/window becomes available.

    Scans token cooldowns (``available_at``, set by ``PacedResourcePlace`` and retries) and
    input-arc settle windows. Returns ``None`` if nothing is time-gated (a genuine deadlock or
    completion), so the driver stops instead of advancing the clock forever.
    """
    now = net.model_time
    best: float | None = None
    for place in net.places.values():
        if isinstance(place, SinkPlace):
            continue
        for tok in place.tokens:
            if tok.available_at > now and (best is None or tok.available_at < best):
                best = tok.available_at
    for transition in net.transitions.values():
        for arc in transition.inputs:
            if arc.settle_secs <= 0:
                continue
            place = net.places.get(arc.place)
            if place is None or isinstance(place, SinkPlace) or len(place) == 0:
                continue
            boundary = getattr(place, "last_deposit_time_model", 0.0) + arc.settle_secs
            if boundary > now and (best is None or boundary < best):
                best = boundary
    return best


def _await_inflight(net: PetriNet) -> None:
    """Block until no transition action is mid-flight (actions here are ~microseconds).

    Reads the ``_running_count`` counter directly rather than via ``snapshot()``: snapshot
    deep-copies the whole marking, which in a tight spin loop would swamp the measurement with
    O(tokens) copying and hide the engine cost we're trying to time. A momentarily stale read
    is harmless — the outer loop re-checks enablement anyway.

    Safe as a barrier: ``_execute_transition`` decrements ``_running_count`` *after* committing
    its output deposits, so observing zero implies every completed action's outputs are visible.
    """
    while net._running_count > 0:
        time.sleep(0)  # yield the GIL to the worker thread without a real sleep


def drive_to_quiescence(net: PetriNet, *, max_ticks: int = 1_000_000) -> RunResult:
    """Run ``net`` to logical quiescence and return timing/step counters.

    Starts the logical clock at the current model time, then repeats: fire every transition
    enabled at the current instant (waiting for each action to settle so its outputs can enable
    the next), and once nothing more fires, jump the clock to the next availability boundary.
    Stops when the net is quiescent (all time-gated work has drained) or nothing is left to wait
    for.
    """
    # Anchor the logical clock so cooldowns/settle windows are measured against it from here on.
    net.advance_time(net.model_time + 1e-9)

    steps = 0
    ticks = 0
    start = time.perf_counter()
    while ticks < max_ticks:
        # Fire everything enabled at the current logical instant.
        #
        # The await here is DELIBERATE, not an oversight. `step()` returns as soon as the action
        # is submitted, so awaiting after each one means at most a single action is ever in
        # flight — this driver is single-threaded by construction, and `max_workers` cannot
        # affect its numbers. That is the intent: it measures engine CPU, not makespan. Two
        # things break if you remove it:
        #   1. Determinism. The seeded channeling regime reproduces step-for-step *because*
        #      everything is serialized; concurrent retries would reorder the RNG draws.
        #   2. Instant accounting. Without the barrier the inner loop exits early, while inputs
        #      are consumed-but-not-yet-committed, so a "logical instant" fires short.
        # Use `drive_saturating` to measure concurrency. An earlier revision swept `max_workers`
        # against *this* driver, found it flat, and published that as evidence about cpnx's
        # parallelism; the conclusion was unfounded and had to be retracted.
        while net.step():
            steps += 1
            _await_inflight(net)
        # Nothing fires right now. Done, or just waiting out a cooldown/settle window?
        if net.is_quiescent():
            break
        boundary = _next_boundary(net)
        if boundary is None or boundary <= net.model_time:
            break
        net.advance_time(boundary)
        ticks += 1
    wall = time.perf_counter() - start
    return RunResult(steps=steps, ticks=ticks, wall_secs=wall)


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
