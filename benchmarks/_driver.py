"""Shared logical-clock driver for the cafe workload benchmarks.

Why not just call ``PetriNet.run()``? ``run()`` waits out cooldowns and settle windows on the
*real* wall clock, so a net with an 8-second grinder cooldown would take 8 real seconds per
grind — you'd be timing ``time.sleep``, not the engine. Instead we keep the topology's real
friction (finite scales, the grinder cooldown, the two-token tray threshold) but drive the net
on its **logical clock**: fire everything enabled *now*, let in-flight actions settle, then jump
``advance_time`` straight to the next availability boundary. Blocking/back-pressure is fully
preserved (the grinder is genuinely unavailable for 8 *logical* seconds), but the waiting costs
no wall-clock time — so the measured wall time is the engine's CPU cost, which is what we can
actually optimise.

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
