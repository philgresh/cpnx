"""Macro benchmark for the ☕ cafe's opt-in stations — what each selection shape costs at depth.

Every station added in the API-combination audit exists because some `_materialize_pool`
route was previously unmeasured. This script drains one deep queue per regime on the logical
clock (`_driver.drive_to_quiescence`, so the measured wall time is engine CPU) and reports
µs/order against queue depth. What matters is the **shape** of each column — flat means the
drain is linear, rising means it is not — and the **ratio** between a regime and its control.

Regimes, grouped by the question they answer:

**Selection shape** (all single-arc, all draining one deep place, so the only difference is
the arc's `key`/`filter`/`consume_all`):

| regime | arc | expected route |
| --- | --- | --- |
| `batch_triage` | certified `key` | 2 — key index (the control) |
| `specials_board` | **uncertified** `key` | 3 — full peek + per-firing sort |
| `eighty_six` | certified `key` + **uncertified** `filter` | 3 — one uncertified callable disqualifies the arc |
| `decaf` | certified `filter`, **no** `key` | 3 — `_ensure_key_index` bails when `arc.key is None` |
| `cold_brew` | no selection, **timed** tokens | 1 — bounded FIFO peek |
| `cold_brew_key` | certified `key` + timed tokens | 3 — the timed×key residual (#25) |

**Search budget** — `binding_search_limit` and arc ordering against the real pipeline, which
is where `T_Weigh_And_Grind`'s three-dimensional Cartesian product actually bites.

Note the depth ladder is deliberately modest (250-2000, where the deep throughput sweep goes
to 20 000). The uncertified regimes pay a thread round-trip **per token per firing**, so their
cost grows as N²·(10 µs) — at 20 000 that is a multi-hour run, not a benchmark. Certified
regimes are re-run at 20 000 separately in `bench_cafe_throughput.py`.

    python benchmarks/bench_station_costs.py
    python benchmarks/bench_station_costs.py selection   # just the selection-shape table
    python benchmarks/bench_station_costs.py budget      # just the search-budget experiments
"""

import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src"))
sys.path.insert(0, str(_HERE))

from _driver import drive_to_quiescence  # noqa: E402
from cafe import build_cafe  # noqa: E402
from cafe.support import DOSE_TARGET_G  # noqa: E402

from cpnx import Token  # noqa: E402

#: Fixed seed. Every cafe transition shares the default `priority`, so an unseeded net breaks
#: each step()'s tie with OS entropy and the step count wanders ~2% run to run — which would
#: make every µs/order figure a comparison between runs that did different amounts of work.
NET_SEED = 20260723

#: Depth ladder. Kept modest because the uncertified regimes are quadratic in N with a ~10 µs
#: constant; see the module docstring.
DEPTHS = (250, 500, 1000, 2000)


def _payloads(n: int) -> list[dict]:
    """Order tickets with the same ~30% out-of-spec dose mix the throughput benchmark uses."""
    return [
        {
            "ratio": "1:2",
            "weight_g": DOSE_TARGET_G + (2.0 if i % 10 < 3 else 0.0),
            "dairy_free": i % 3 == 0,
            "mobile_pickup": i % 7 == 0,
            "decaf": True,
            "syrup": "vanilla",
            "cup_oz": 12 + (i % 3) * 4,
        }
        for i in range(n)
    ]


#: Each selection regime: build_cafe flags, and the place its queue is stocked into.
SELECTION_REGIMES = {
    "batch_triage": (dict(batch_triage=True), "P_Batch_Triage_Queue"),
    "specials_board": (dict(specials_board=True), "P_Specials_Queue"),
    "eighty_six": (dict(eighty_six=True), "P_Eighty_Six_Queue"),
    "decaf": (dict(decaf=True), "P_Decaf_Line"),
    "cold_brew": (dict(cold_brew=True), "P_Cold_Brew_Steeping"),
    "cold_brew_key": (dict(cold_brew=True, cold_brew_key=True), "P_Cold_Brew_Steeping"),
}


def _run_selection(regime: str, depth: int) -> float:
    """Stock one station's queue to *depth*, drain it, return µs/order.

    Everything is deposited before a single token is drained, so the queue is at its peak
    depth for the first firing — which is the point: a drain whose per-firing cost scales
    with remaining depth shows up as a rising µs/order, and one served from an index does not.
    """
    flags, place = SELECTION_REGIMES[regime]
    with build_cafe(**flags, seed=NET_SEED, max_workers=1) as net:
        timed = place == "P_Cold_Brew_Steeping"
        base = time.monotonic()
        for i, payload in enumerate(_payloads(depth)):
            # Cold-brew tokens are what make that place *timed* — the place class is plain.
            # Stagger them so the cooling heap holds every token at once, which is the shape
            # that makes the timed×key residual reachable.
            token = (
                Token(color="cold_brew", payload=payload, available_at=base + 0.001 * (i + 1))
                if timed
                else Token(payload=payload)
            )
            net.deposit(place, token)

        assert len(net.places[place]) == depth, "queue drained before the run started"
        result = drive_to_quiescence(net)
        served = net.places["P_Served"].stats()["absorbed"]
        assert served == depth, f"{regime}: served {served} of {depth} — regime did not fully drain"
        return result.wall_secs / depth * 1e6


def run_selection_table() -> None:
    print("\n=== Selection shape: µs/order against queue depth (one deep place, single arc) ===")
    header = f"{'regime':<16}" + "".join(f"{d:>12}" for d in DEPTHS) + f"{'growth':>10}"
    print(header)
    print("-" * len(header))
    baseline: dict[int, float] = {}
    for regime in SELECTION_REGIMES:
        cells = []
        for depth in DEPTHS:
            # Min of two: measurement noise only ever *adds* time, so min is the right estimator.
            us = min(_run_selection(regime, depth) for _ in range(2))
            cells.append(us)
            if regime == "batch_triage":
                baseline[depth] = us
        growth = cells[-1] / cells[0]
        print(f"{regime:<16}" + "".join(f"{c:>12.1f}" for c in cells) + f"{growth:>9.1f}x")
    print(f"\n(depth ladder {DEPTHS[0]}->{DEPTHS[-1]} is {DEPTHS[-1] // DEPTHS[0]}x; "
          "'growth' is last÷first — ~1x means the drain is linear in N)")
    print("\nRatio vs. `batch_triage` (the certified-key control, route 2):")
    for regime in SELECTION_REGIMES:
        if regime == "batch_triage":
            continue
        ratios = [min(_run_selection(regime, d) for _ in range(2)) / baseline[d] for d in DEPTHS]
        print(f"  {regime:<16}" + "".join(f"{r:>11.1f}x" for r in ratios))


def run_budget_experiments() -> None:
    """The two search-budget questions, both against the real pipeline rather than a lone arc."""
    print("\n=== Arc ordering: does listing permit arcs first widen the effective search depth? ===")
    print("`itertools.product` varies the LAST arc fastest, so a deep data arc listed last is")
    print("the dimension that changes first. BindingPolicy's docs recommend permits-first; the")
    print("cafe does the opposite. Rank = where the single deep mobile ticket got ground;")
    print("0 means the preference reached it, n-1 means it never saw it.\n")
    print(f"{'orders':>8} {'arc order':<16} {'µs/order':>10} {'deep-mobile rank':>18}")
    print("-" * 56)
    for n in (100, 200, 400, 800):
        for label, first in (("data first", False), ("permits first", True)):
            us, rank = _run_pipeline(n, resource_arcs_first=first)
            print(f"{n:>8} {label:<16} {us:>10.1f} {rank:>18.0f}")

    print("\n=== binding_search_limit sweep (engine default 1000) ===")
    print("Cost of the scan vs. how deep the preference can still see, on the cafe's own")
    print("data-first arc order.\n")
    print(f"{'orders':>8} {'limit':>8} {'µs/order':>10} {'deep-mobile rank':>18}")
    print("-" * 48)
    for n in (400, 800):
        for limit in (100, 1000, 10000):
            us, rank = _run_pipeline(n, binding_search_limit=limit)
            print(f"{n:>8} {limit:>8} {us:>10.1f} {rank:>18.0f}")


def _run_pipeline(n_orders: int, **flags) -> tuple[float, float]:
    """Drain `n_orders` through the pipeline; return (µs/order, deep-mobile-ticket serve rank).

    The second number is what the cost figure alone cannot tell you. `binding_priority_key`
    pulls mobile-pickup tickets ahead of walk-ins — but only while the search budget still
    *reaches* them, and when it stops reaching, nothing errors or warns: the scan still runs,
    still costs, and quietly falls back to insertion order.

    To make that visible, exactly **one** ticket in the line is mobile-pickup, and it is the
    **last** one deposited — i.e. the deepest. Its rank among tickets ground is then a direct
    read on how far into the line the preference can see:

    - rank ≈ 0 → the search reached the bottom of the line and the preference held;
    - rank ≈ `n_orders` → it never saw the ticket, and the ticket was ground in plain FIFO
      order like everything else.

    A population-wide mobile rate cannot measure this: at one mobile ticket in seven, *every*
    candidate window contains one, so the preference looks like it is working at any depth.

    Sampled at `P_Ground_Coffee`, deliberately, not at `P_Served`: grounds are produced by
    `order.evolve(...)` so they still carry the ticket's payload, whereas `serve_drink` mints a
    brand-new token and the flag is gone by the time anything reaches the sink.
    `P_Ground_Coffee` is the direct output of `T_Weigh_And_Grind` — the transition whose
    `binding_priority_key` is under test — so it is the earliest honest observation point.
    """
    with build_cafe(seed=NET_SEED, max_workers=1, channel_failure_rate=0.0, **flags) as net:
        ground: list[bool] = []

        def _record(place_name: str, token: Token) -> None:
            if place_name == "P_Ground_Coffee":
                ground.append(bool(token.payload.get("mobile_pickup")))

        net.on_token_deposited = _record
        payloads = _payloads(n_orders)
        for i, payload in enumerate(payloads):
            # Exactly one mobile ticket, deposited last so it sits deepest in the line.
            payload = {**payload, "mobile_pickup": i == n_orders - 1}
            net.deposit("P_Ticket_Line", Token(payload=payload))
        result = drive_to_quiescence(net)

    rank = ground.index(True) if True in ground else float("nan")
    return result.wall_secs / n_orders * 1e6, rank


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    print(f"☕ Station cost benchmark — seed={NET_SEED}, max_workers=1, logical clock")
    if which in ("all", "selection"):
        run_selection_table()
    if which in ("all", "budget"):
        run_budget_experiments()


if __name__ == "__main__":
    main()
