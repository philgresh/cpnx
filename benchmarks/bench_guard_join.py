"""Benchmark: binding-search cost of a guard join over a deep place (#59 cause C, #64).

A guarded transition takes the searching path. When the satisfying binding sits near the
head of the candidate order, `FIRST` should pay for the candidates it inspects, not for
the depth of the places behind them. If the search builds every arc's whole (capped)
option list before trying the first combination, each evaluation costs O(depth) and a
drive that pushes N tokens through costs O(N^2).

The net: N untimed jobs wait in `waiting`, each tagged with one of 8 worker ids. A
`match` transition joins a job with the worker whose `wid` equals the job's, and the
worker is busy for 1.0 time unit after each match. `binding_search_limit` is raised to
1 000 000 so the search cap does not hide the depth effect.

Two views:

- **probe** -- per-call `_resolve_binding` on a net holding N jobs and 8 free workers.
  `hit`: the head job matches a free worker, so the first satisfying binding is the
  first candidate. It should stay flat as N doubles. `miss`: no job matches any worker,
  so `FIRST` walks the whole product. It stays linear in N; removing that needs an
  index on the join key, not a lazier search.
- **drive** -- `drive_to_quiescence` until all N jobs are matched.

Each view runs twice: `guard`, where the guard tests `wid` equality, and `join_key`, where
both arcs declare `join_key` on `wid` instead (#64, ADR 0010) and the transition has no
guard. With `join_key`, `miss` is a handful of index lookups and should stay flat too.

Report the *shape* (flat vs linear growth per doubling) and the ratio, not raw microseconds.
Native stdlib only.

    python benchmarks/bench_guard_join.py
"""

import sys
import time
import timeit
from pathlib import Path

# Make ``src/`` importable when run from a checkout without installation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cpnx.engine import PetriNet  # noqa: E402
from cpnx.places import Place  # noqa: E402
from cpnx.tokens import Token  # noqa: E402
from cpnx.transitions import InputArc, OutputArc, Transition  # noqa: E402

WORKERS = 8
SEARCH_LIMIT = 1_000_000
PROBE_DEPTHS = (1_000, 2_000, 4_000, 8_000, 16_000)
DRIVE_DEPTHS = (500, 1_000, 2_000, 4_000, 8_000)
CALLS = 200


def _wid(token: Token) -> object:
    return token.payload["wid"]


def _build(depth: int, wid_of, *, join: bool = False) -> PetriNet:
    """A job/worker join net holding `depth` jobs, job `i` tagged `wid_of(i)`, and 8 free workers.

    With `join`, the arcs declare `join_key` on `wid` in place of the equality guard.
    """
    net = PetriNet(seed=11, binding_search_limit=SEARCH_LIMIT)
    for name in ("waiting", "workers", "out"):
        net.add_place(Place(name))

    def match(tokens: list[Token]) -> list[Token]:
        return [tokens[0], tokens[1].evolve(available_at=net.model_time + 1.0)]

    net.add_transition(
        Transition(
            name="match",
            inputs=[
                InputArc("waiting", join_key=_wid if join else None),
                InputArc("workers", join_key=_wid if join else None),
            ],
            outputs=[OutputArc("out"), OutputArc("workers")],
            guard=None if join else lambda tokens: tokens[0].payload["wid"] == tokens[1].payload["wid"],
            action=match,
        )
    )
    net.advance_time(1.0)
    for w in range(WORKERS):
        net.deposit("workers", Token(payload={"wid": w}))
    for i in range(depth):
        net.deposit("waiting", Token(payload={"job": i, "wid": wid_of(i)}))
    return net


def _probe_us(depth: int, *, hit: bool, join: bool = False) -> float:
    """Microseconds per `_resolve_binding` call over `depth` waiting jobs."""
    net = _build(depth, (lambda i: i % WORKERS) if hit else (lambda i: WORKERS + i % WORKERS), join=join)
    transition = net.transitions["match"]
    m_time = net.model_time
    calls = CALLS if hit or join else max(3, CALLS * 1_000 // depth)
    assert (net._resolve_binding(transition, m_time) is not None) is hit
    return timeit.timeit(lambda: net._resolve_binding(transition, m_time), number=calls) / calls * 1e6


def _drive_us_per_step(depth: int, *, join: bool = False) -> float:
    """Microseconds per firing to match `depth` jobs against 8 workers on the logical clock."""
    net = _build(depth, lambda i: (i * 7) % WORKERS, join=join)
    started = time.perf_counter()
    result = net.drive_to_quiescence()
    elapsed = time.perf_counter() - started
    assert len(net.places["out"]) == depth
    return elapsed / result.steps * 1e6


def main() -> None:
    for join in (False, True):
        arm = "join_key" if join else "guard"
        print(f"-- probe ({arm}): _resolve_binding over N waiting jobs, {WORKERS} free workers --")
        print(f"   {'depth N':>8}  {'hit us/call':>12}  {'growth':>7}  {'miss us/call':>13}  {'growth':>7}")
        prev_hit: float | None = None
        prev_miss: float | None = None
        for depth in PROBE_DEPTHS:
            hit_us = _probe_us(depth, hit=True, join=join)
            miss_us = _probe_us(depth, hit=False, join=join)
            hit_growth = f"x{hit_us / prev_hit:.2f}" if prev_hit else "-"
            miss_growth = f"x{miss_us / prev_miss:.2f}" if prev_miss else "-"
            print(f"   {depth:>8}  {hit_us:>12.2f}  {hit_growth:>7}  {miss_us:>13.1f}  {miss_growth:>7}")
            prev_hit, prev_miss = hit_us, miss_us
        print()

    print("-- drive: drive_to_quiescence matching N jobs against the workers --")
    print(f"   {'depth N':>8}  {'guard us/step':>14}  {'growth':>7}  {'join_key us/step':>17}  {'growth':>7}")
    prev_guard: float | None = None
    prev_join: float | None = None
    for depth in DRIVE_DEPTHS:
        guard_us = _drive_us_per_step(depth)
        join_us = _drive_us_per_step(depth, join=True)
        guard_growth = f"x{guard_us / prev_guard:.2f}" if prev_guard else "-"
        join_growth = f"x{join_us / prev_join:.2f}" if prev_join else "-"
        print(f"   {depth:>8}  {guard_us:>14.1f}  {guard_growth:>7}  {join_us:>17.1f}  {join_growth:>7}")
        prev_guard, prev_join = guard_us, join_us

    print()
    print("   Expected: 'hit us/call' and the drive's 'us/step' hold ~flat as N doubles. Before")
    print("   the lazy candidate product both grew roughly linearly with N. With the guard,")
    print("   'miss us/call' grows linearly by design: with no satisfying binding, FIRST inspects")
    print("   every candidate. With join_key it holds ~flat: the miss is a few index lookups.")


if __name__ == "__main__":
    main()
