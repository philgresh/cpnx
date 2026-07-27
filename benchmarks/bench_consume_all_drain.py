"""Benchmark: enablement-probe cost for a ``consume_all`` transition over a deep place.

A ``consume_all=True`` input arc sweeps a place's *entire* pool in a single firing. The
subtlety is not the firing -- it is every enablement probe that happens *before* it. When a
deep place fills one token at a time (e.g. votes accumulating under a ``ThresholdPlace``
barrier), the engine re-checks whether the ``consume_all`` transition is enabled on each
step. If that check materializes the whole pool (``peek(len(place))``) just to answer "can
it fire?", then a place that grows to N tokens pays ``1 + 2 + ... + N = O(N^2)`` copying
across the run, even though the transition ultimately fires once.

The lazy-drain fast path (`engine._try_count_only_binding`) answers the probe with a
*count-only* check (``can_retrieve`` + timing settle) and defers materialization to consume
time. So the per-probe cost drops from ``O(N)`` to ``O(1)``, turning the aggregate accumulate
-then-drain from ``O(N^2)`` into ``O(N)``.

This benchmark isolates that per-probe cost with an in-build A/B on the *same* engine:

- **fast path** -- a plain guard-free ``consume_all`` transition (uses the count-only probe);
- **eager path** -- the identical transition with a trivial always-true guard. A guard
  disqualifies the fast path (the guard must see the materialized batch), so the probe falls
  back to the pre-fix full-pool ``peek``. The guard predicate is ``True`` for every pool, so
  it never changes *which* bindings are enabled -- it only forces materialization, isolating
  copy cost from predicate cost.

Both arms probe with ``_is_transition_enabled`` on a place pre-filled to depth N. The fast
arm should stay flat as N grows; the eager arm should climb linearly, and the reported ratio
should widen with N. Report the *shape* (flat vs linear) and the ratio, not raw microseconds
-- absolute figures are hardware- and interpreter-specific. Native stdlib only.

    python benchmarks/bench_consume_all_drain.py
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

# Pool depths to sweep. Doubling makes the growth law legible: an O(N) per-probe cost
# doubles each row, an O(1) cost holds flat.
DEPTHS = (1_000, 2_000, 4_000, 8_000, 16_000)

# Probes per timing sample. The probe is pure/read-only (it does not fire), so the pool
# depth is stable across all repeats within a sample.
PROBES = 2_000


# Guard callables MUST be module-level `def`s: `Transition.__setattr__` runs
# `verify_callable_purity`, which uses `inspect.getsource` and rejects callables whose
# source it cannot retrieve (e.g. lambdas defined dynamically).
def guard_always_true(tokens: list[Token]) -> bool:
    """Always-true guard: never changes enablement, only forces the eager full-pool peek.

    Its presence disqualifies the `consume_all` fast path (a guard must see the materialized
    batch), so the probe falls back to the pre-fix behavior -- exactly the arm we want to
    contrast against.
    """
    return True


def _drain_action(tokens: list[Token]) -> list[Token]:
    """Fold a swept batch into one token; never actually run here (probe-only benchmark)."""
    return [Token(payload={"count": len(tokens)})]


def build_net(depth: int, *, eager: bool) -> tuple[PetriNet, Transition]:
    """A one-transition net whose ``consume_all`` arc drains a place pre-filled to ``depth``.

    ``eager=True`` attaches ``guard_always_true`` to force the pre-fix full-pool
    materialization on every enablement probe; ``eager=False`` leaves the transition
    guard-free so it takes the count-only fast path.
    """
    net = PetriNet()
    net.add_place(Place("inbox"))
    net.add_place(Place("out"))

    transition = Transition(
        name="drain",
        inputs=[InputArc("inbox", consume_all=True)],
        outputs=[OutputArc("out", count=1)],
        action=_drain_action,
        guard=guard_always_true if eager else None,
    )
    net.add_transition(transition)

    for i in range(depth):
        net.deposit("inbox", Token(payload={"i": i}))

    return net, transition


def _time_probe(depth: int, *, eager: bool) -> float:
    """Time ``_is_transition_enabled`` on a pool of ``depth`` tokens; return microseconds/probe."""
    net, transition = build_net(depth, eager=eager)

    # Warm up and assert the transition really is enabled, so the probe exercises the full
    # resolution path (not an early "unmet arc" bail-out).
    assert net._is_transition_enabled(transition), "transition should be enabled"

    elapsed = timeit.timeit(lambda: net._is_transition_enabled(transition), number=PROBES)
    return elapsed / PROBES * 1e6


def main() -> None:
    print("-- consume_all enablement probe: fast (count-only) vs eager (full-pool peek) --")
    print(f"   {PROBES} probes/sample; per-probe microseconds; ratio = eager / fast")
    print(f"   {'depth N':>8}  {'fast us':>10}  {'eager us':>10}  {'ratio':>7}")

    prev_eager: float | None = None
    for depth in DEPTHS:
        fast_us = _time_probe(depth, eager=False)
        eager_us = _time_probe(depth, eager=True)
        ratio = eager_us / fast_us if fast_us else float("inf")
        growth = f"  (eager x{eager_us / prev_eager:.2f} vs prev)" if prev_eager else ""
        print(f"   {depth:>8}  {fast_us:>10.3f}  {eager_us:>10.3f}  {ratio:>6.1f}x{growth}")
        prev_eager = eager_us

    print()
    print("   Expected: 'fast us' holds ~flat (O(1) per probe); 'eager us' ~doubles per")
    print("   doubling of N (O(N) per probe). Since a real accumulate-then-drain performs")
    print("   O(N) such probes, this is the O(N) vs O(N^2) aggregate difference per probe.")


if __name__ == "__main__":
    main()
