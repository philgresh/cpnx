"""Benchmark: logical-clock cost when matured timed tokens back up in a place (#59).

A net used as a discrete-event simulator builds backlogs by design: work arrives as timed
tokens and waits for a scarce resource. Every token in that backlog has *matured* (its
`available_at` is in the past) but has not been consumed. `drive_to_quiescence` asks every
place for its next future boundary on each clock advance. If the store answers by walking
past the matured backlog to reach the first future token, each tick costs O(backlog) and
a drive that pushes N tokens through costs O(N^2).

Two views:

- **boundary** -- per-call `Place.earliest_available_boundary` on a place holding N matured
  tokens and one future token. This is the cost the matured heap removes: it should stay
  flat as N doubles.
- **drive** -- `drive_to_quiescence` on N timed arrivals spaced 0.01 apart, served by one
  worker that is busy for 1.0 after each firing, so the matured backlog grows to about N.
  `untimed` is the identical net with the arrivals deposited `AVAILABLE_NOW`, as a control.
  With the matured heap and the head-only FIFO peek (#59 causes A and B) the timed arm
  tracks the control.

Report the *shape* (flat vs linear growth per doubling) and the ratio, not raw microseconds.
Native stdlib only.

    python benchmarks/bench_timed_backlog.py
"""

import sys
import time
import timeit
from pathlib import Path

# Make ``src/`` importable when run from a checkout without installation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cpnx.engine import PetriNet  # noqa: E402
from cpnx.places import Place  # noqa: E402
from cpnx.tokens import AVAILABLE_NOW, Token  # noqa: E402
from cpnx.transitions import InputArc, OutputArc, Transition  # noqa: E402

BOUNDARY_DEPTHS = (1_000, 2_000, 4_000, 8_000, 16_000)
DRIVE_DEPTHS = (500, 1_000, 2_000, 4_000)
CALLS = 500


def _boundary_us(depth: int) -> float:
    """Microseconds per `earliest_available_boundary` call over `depth` matured tokens."""
    place = Place("backlog")
    for i in range(depth):
        place.deposit(Token(available_at=1.0 + i), model_time=0.0)
    place.deposit(Token(available_at=10.0 * depth), model_time=0.0)
    now = float(depth + 1)
    assert place.earliest_available_boundary(now) == 10.0 * depth
    return timeit.timeit(lambda: place.earliest_available_boundary(now), number=CALLS) / CALLS * 1e6


def _drive_us_per_step(depth: int, *, timed: bool) -> float:
    """Microseconds per firing to drain `depth` arrivals through one worker on the logical clock."""
    net = PetriNet(seed=0)
    for name in ("inbox", "worker", "out"):
        net.add_place(Place(name))

    def serve(tokens: list[Token]) -> list[Token]:
        return [tokens[0], tokens[1].evolve(available_at=net.model_time + 1.0)]

    net.add_transition(
        Transition(
            name="serve",
            inputs=[InputArc("inbox"), InputArc("worker")],
            outputs=[OutputArc("out"), OutputArc("worker")],
            action=serve,
        )
    )
    net.advance_time(1.0)
    net.deposit("worker", Token())
    for i in range(depth):
        net.deposit("inbox", Token(available_at=1.0 + 0.01 * (i + 1) if timed else AVAILABLE_NOW))

    started = time.perf_counter()
    result = net.drive_to_quiescence()
    elapsed = time.perf_counter() - started
    assert len(net.places["out"]) == depth
    return elapsed / result.steps * 1e6


def main() -> None:
    print("-- boundary: earliest_available_boundary over a matured backlog --")
    print(f"   {CALLS} calls/sample; per-call microseconds")
    print(f"   {'depth N':>8}  {'us/call':>10}  {'growth':>7}")
    prev: float | None = None
    for depth in BOUNDARY_DEPTHS:
        us = _boundary_us(depth)
        growth = f"x{us / prev:.2f}" if prev else "-"
        print(f"   {depth:>8}  {us:>10.3f}  {growth:>7}")
        prev = us

    print()
    print("-- drive: drive_to_quiescence through one worker, timed vs untimed arrivals --")
    print(f"   {'depth N':>8}  {'timed us/step':>14}  {'untimed us/step':>16}  {'timed growth':>13}")
    prev = None
    for depth in DRIVE_DEPTHS:
        timed_us = _drive_us_per_step(depth, timed=True)
        untimed_us = _drive_us_per_step(depth, timed=False)
        growth = f"x{timed_us / prev:.2f}" if prev else "-"
        print(f"   {depth:>8}  {timed_us:>14.1f}  {untimed_us:>16.1f}  {growth:>13}")
        prev = timed_us

    print()
    print("   Expected: 'us/call' holds ~flat (O(log n) amortized per boundary query). Before the")
    print("   matured heap it ~doubled per doubling of N, which made each clock advance O(backlog).")
    print("   'timed us/step' tracks 'untimed us/step' and grows only slowly with N. Before #59's")
    print("   fixes it grew roughly linearly with N, far above the untimed control.")


if __name__ == "__main__":
    main()
