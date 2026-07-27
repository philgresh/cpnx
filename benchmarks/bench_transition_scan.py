"""Benchmark: per-step transition-scan cost as the *transition count* grows.

Every ``step()`` re-derives enablement from scratch. `_select_transition_to_fire` calls
`_enabled_transition_bindings`, which loops over **every** transition in the net and resolves
a fresh binding for each -- then fires exactly one. So a single step is ``O(T)`` in the
transition count `T`, and a run that fires `K` times pays ``O(K * T)`` binding resolutions,
even though any one firing changes only the places it touched and therefore can only flip the
enablement of the transitions reading *those* places.

Most of cpnx's benchmarks sweep marking *depth* (how many tokens a place holds). This one
sweeps the orthogonal axis -- how many *transitions* the net has -- which is exactly the cost
an incremental-enablement scheduler (a per-place dirty set) would remove, and which nothing
here currently measures.

Two views of the same cost:

- **scan (micro).** A net of `T` independent transitions with exactly **one** enabled. Time a
  single `_select_transition_to_fire` probe (it resolves all `T`, fires nothing). Per-probe
  time should climb linearly with `T`: the ``O(T)`` scan, dominated by the ``T - 1``
  transitions that cannot fire and would be skipped entirely under dirty-set scheduling.
- **drive (macro).** A net of `T` independent transitions, each fed exactly one token, driven
  to quiescence. That is ``K = T`` firings, each preceded by an ``O(T)`` scan, so total engine
  CPU is ``O(T^2)``. Reported as wall time and microseconds-per-step; per-step should rise
  linearly with `T` (=> quadratic overall).

Report the *shape* (flat vs linear per step) and the growth factor, not raw microseconds --
absolute figures are hardware- and interpreter-specific. Native stdlib only.

    python benchmarks/bench_transition_scan.py
"""

import sys
import timeit
from pathlib import Path

# Make ``src/`` importable when run from a checkout without installation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cpnx.engine import PetriNet  # noqa: E402
from cpnx.places import Place  # noqa: E402
from cpnx.tokens import Token  # noqa: E402
from cpnx.transitions import InputArc, OutputArc, Transition  # noqa: E402

# Transition counts to sweep. Doubling makes the growth law legible: an O(T) per-step cost
# doubles each row; O(T^2) total drive time quadruples.
COUNTS = (50, 100, 200, 400, 800, 1_600)

# Probes per scan sample. The probe is read-only (it selects but does not fire), so the net's
# marking is stable across every repeat within a sample.
PROBES = 500


def _identity(tokens: list[Token]) -> list[Token]:
    """Pass the consumed token straight through to the output arc."""
    return tokens


def build_independent_net(n_transitions: int, *, seed_all: bool) -> PetriNet:
    """A net of ``n_transitions`` fully independent transitions.

    Each transition ``t{i}`` reads its own private ``in{i}`` and writes its own ``out{i}``; no
    two transitions share a place, so firing one can never enable or disable another. That
    isolation is the point: it makes the per-step scan cost attributable purely to the
    transition *count*, with no cascade of secondary enablement changes to muddy the measure.

    ``seed_all=True`` deposits one token into every input place (used by the drive benchmark:
    ``K = n`` firings). ``seed_all=False`` seeds only the *first* input place (used by the scan
    benchmark: exactly one transition enabled, the other ``n - 1`` scanned-but-disabled).
    """
    net = PetriNet(max_workers=1)
    for i in range(n_transitions):
        net.add_place(Place(f"in{i}"))
        net.add_place(Place(f"out{i}"))
        net.add_transition(
            Transition(
                name=f"t{i}",
                inputs=[InputArc(f"in{i}", count=1)],
                outputs=[OutputArc(f"out{i}", count=1)],
                action=_identity,
            )
        )
    if seed_all:
        for i in range(n_transitions):
            net.deposit(f"in{i}", Token(payload={"i": i}))
    else:
        net.deposit("in0", Token(payload={"i": 0}))
    return net


def _time_scan(n_transitions: int) -> float:
    """Microseconds per ``_select_transition_to_fire`` probe with exactly one enabled of `n`."""
    net = build_independent_net(n_transitions, seed_all=False)
    # Warm up and prove exactly one transition is selectable (the probe does real work).
    assert net._select_transition_to_fire() is not None, "one transition should be enabled"
    elapsed = timeit.timeit(net._select_transition_to_fire, number=PROBES)
    return elapsed / PROBES * 1e6


def _time_drive(n_transitions: int) -> tuple[float, int]:
    """Drive `n` independent one-shot transitions to quiescence; return (wall_secs, steps)."""
    net = build_independent_net(n_transitions, seed_all=True)
    with net:
        start = timeit.default_timer()
        result = net.drive_to_quiescence()
        wall = timeit.default_timer() - start
    assert result.steps == n_transitions, f"expected {n_transitions} firings, got {result.steps}"
    return wall, result.steps


def main() -> None:
    print("-- scan: cost of one _select_transition_to_fire with exactly 1 of T enabled --")
    print(f"   {PROBES} probes/sample; per-probe microseconds; growth = this / previous row")
    print(f"   {'T':>6}  {'scan us':>10}  {'growth':>7}")
    prev: float | None = None
    for n in COUNTS:
        scan_us = _time_scan(n)
        growth = f"x{scan_us / prev:.2f}" if prev else "  --"
        print(f"   {n:>6}  {scan_us:>10.2f}  {growth:>7}")
        prev = scan_us

    print()
    print("-- drive: fire T independent one-shot transitions to quiescence (K = T firings) --")
    print(f"   {'T':>6}  {'wall ms':>10}  {'us/step':>10}  {'step growth':>12}")
    prev_per_step: float | None = None
    for n in COUNTS:
        wall, steps = _time_drive(n)
        per_step_us = wall / steps * 1e6
        growth = f"x{per_step_us / prev_per_step:.2f}" if prev_per_step else "  --"
        print(f"   {n:>6}  {wall * 1e3:>10.2f}  {per_step_us:>10.2f}  {growth:>12}")
        prev_per_step = per_step_us

    print()
    print("   Expected: 'scan us' and 'us/step' both ~double per doubling of T (O(T) per step).")
    print("   Since the drive does K = T steps, total drive wall time is O(T^2) -- the ceiling")
    print("   an incremental-enablement (per-place dirty-set) scheduler would collapse to O(T).")


if __name__ == "__main__":
    main()
