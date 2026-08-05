"""Benchmark: cost of *structural* resource-permit returns (ADR 0009) on the ☕ cafe topology.

ADR 0009 makes a borrowed resource permit's return a real `OutputArc` (an auto-synthesized
self-loop) instead of the engine's off-arc "implicit leftover-return". This benchmark answers
the only question that matters for shipping it: **does making the return structural cost
anything at run time?**

It processes N identical customer orders through the real cafe net (finite scales, the grinder
with its cooldown, the two-token order-tray rendezvous). The shipped cafe deliberately mixes all
three return modes (see its module docstring), so for a clean A/B/C this benchmark *normalizes*
every resource borrow to a single mode per variant and compares engine CPU:

- ``explicit``    — every borrow declares its own return `OutputArc` (the #50 CPN-faithful
                    style; synthesis is a no-op here).
- ``synthesized`` — no return arcs; the engine synthesizes them at ``validate`` (the ADR 0009
                    default path).
- ``implicit``    — no return arcs and ``auto_return_resources=False``, so permits return
                    through the pre-#50 off-arc leftover-sweep (the old behavior).

All three do byte-identical work — same seed, channeling off — so ``steps`` and ``served`` must
match across variants (asserted); only wall time varies. The net runs on a **logical clock**
(see ``_driver.py``) so the grinder's 8s cooldown is real back-pressure that costs no
wall-clock time, leaving the measurement as pure engine CPU.

Expectation: ``explicit`` ≈ ``synthesized`` (the synthesized arc IS the same arc, plus a
one-time injection at validate and a single bool check per ``step``), and ``implicit`` within
noise of both (a raw sweep vs. a validated deposit for ~4 permit returns per order). A large
gap in either direction is the "detrimental effect" to investigate. Report the ratio, not raw
microseconds (see benchmarks/README.md).

    python benchmarks/bench_resource_return.py

Native stdlib only.
"""

import sys
from pathlib import Path

# Make ``src/`` (and this benchmarks/ dir, for ``_driver``) importable from a bare checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _driver import drive_to_quiescence  # noqa: E402
from concurrency_cafe import build_cafe  # noqa: E402

from cpnx import OutputArc, Token  # noqa: E402
from cpnx.places import ResourcePlace  # noqa: E402

ORDER_COUNTS = (100, 500, 2000)
NET_SEED = 4242  #: same seed as bench_cafe_throughput — deterministic step counts.
REPEATS = 5  #: best-of, to squeeze out scheduler/GC noise.


def _order_payloads(n: int) -> list[dict]:
    return [
        {"ratio": "1:2", "weight_g": 18, "dairy_free": (i % 2 == 0), "mobile_pickup": (i % 3 == 0)} for i in range(n)
    ]


def _normalize_returns(net, variant: str) -> None:
    """Force every resource borrow in `net` onto a single return mode, so the three variants
    differ only in *how* the permit gets home. Mutates the net in place before it is driven.
    """
    for transition in net.transitions.values():
        pools = [a.place for a in transition.inputs if isinstance(net.places.get(a.place), ResourcePlace)]
        # Start from a clean slate: drop any resource-return arcs the fixture declared.
        transition.outputs[:] = [
            a for a in transition.outputs if not isinstance(net.places.get(a.place), ResourcePlace)
        ]
        if variant == "explicit":
            transition.outputs.extend(OutputArc(pool) for pool in pools)
            transition.auto_return_resources = True
        elif variant == "synthesized":
            transition.auto_return_resources = True  # engine adds the arc at validate
        elif variant == "implicit":
            transition.auto_return_resources = False  # raw off-arc leftover-sweep
    # The topology changed after construction; re-arm synthesis so `validate`/`step` re-runs it.
    net._resource_returns_synthesized = False


def _build(variant: str, n_orders: int):
    net = build_cafe(channel_failure_rate=0.0, seed=NET_SEED, max_workers=1)
    _normalize_returns(net, variant)
    for payload in _order_payloads(n_orders):
        net.deposit("P_New_Order", Token(payload=payload))
    return net


def _best_run(variant: str, n_orders: int) -> tuple[float, int, int]:
    """Best-of-REPEATS wall time for `variant`; returns (wall_secs, steps, served)."""
    best = None
    for _ in range(REPEATS):
        with _build(variant, n_orders) as net:
            result = drive_to_quiescence(net)
            served = net.places["P_Served"].stats()["absorbed"]
            if best is None or result.wall_secs < best[0]:
                best = (result.wall_secs, result.steps, served)
    return best


def _run_sweep(n_orders: int) -> None:
    rows = {v: _best_run(v, n_orders) for v in ("explicit", "synthesized", "implicit")}

    # Correctness gate: all three must do identical work, or the timing comparison is meaningless.
    steps = {v: r[1] for v, r in rows.items()}
    served = {v: r[2] for v, r in rows.items()}
    assert len(set(steps.values())) == 1, f"step counts diverged across variants: {steps}"
    assert len(set(served.values())) == 1, f"served counts diverged across variants: {served}"

    base = rows["explicit"][0]
    print(f"  orders={n_orders:<5} steps={steps['explicit']:<6} served={served['explicit']:<5}")
    for variant in ("explicit", "synthesized", "implicit"):
        wall, _, _ = rows[variant]
        us_per_order = wall / n_orders * 1e6
        ratio = wall / base
        print(f"    {variant:<12} {wall * 1e3:8.2f} ms  ({us_per_order:8.1f} us/order)  x{ratio:.3f} vs explicit")


def main() -> None:
    print("ADR 0009 — structural resource-return cost (best of %d, logical clock, cafe topology)\n" % REPEATS)
    for n in ORDER_COUNTS:
        _run_sweep(n)
        print()


if __name__ == "__main__":
    main()
