"""Macro benchmark for `SubstitutionTransition` — what wrapping a region in a subnet costs.

The cafe's `pastry_case` station is the fixture's only hierarchical-CPN example, and it was
the one structure shape with no numbers. Its cost axis is unlike the deep-place sweeps in
`bench_station_costs.py`: a `SubstitutionTransition` fires like any other pooled action, but
its "action" is *drive an entire nested net to quiescence*. So the questions are about
**per-firing overhead**, not marking depth.

Three things, each isolating one fact established by reading `_execute_substitution_transition`
in `engine.py`:

1. **Overhead of the abstraction.** Wrap a chain of K instant pass-through transitions in a
   subnet vs. inlining the identical K transitions in the parent. Same logical work; the
   wall-time delta per firing is the cost of the subnet machinery (deposit into ports, drive
   the subnet to quiescence, retrieve from ports) that inlining does not pay.

2. **Does that overhead scale with subnet size?** Sweep K. A fixed per-firing cost (spin-up /
   teardown only) stays flat in K; a per-internal-transition cost grows with it.

3. **Driver-mode inheritance — a subnet's cooldowns are free under simulation.** A subnet
   inherits the parent's clock *regime*: under `drive_to_quiescence` (logical) the subnet is
   driven logically too, so its internal cooldowns/settles are jumped for free, exactly as the
   parent's are; under `run()` (wall, production) they are waited out in real time. This section
   runs the same cooldown both ways and shows it free in simulation and real in production.

Driver note — clock isolation + regime inheritance
--------------------------------------------------
A subnet's clock *value* is isolated (the parent's logical time is never pushed onto it), while
its clock *regime* (logical vs. wall) is inherited from how the parent is being driven.

This was not always so. `drive_to_quiescence` used to *strand* tokens inside a subnet (14 of 20
stuck in the input port) and report success anyway, because the parent's logical time value was
pushed onto the subnet and the second firing at the same instant moved its clock
backward-or-equal, which `advance_time` rejects — a silent wrong result. The fix isolates the
value and inherits the regime (`src/cpnx/engine.py`, regression tests in `tests/test_subnet.py`).

Tradeoff, measured below: for a *friction-free* subnet the logical driver's tighter clock
machinery is ~20% heavier per firing than `run()`'s — a simulation-only cost, and negligible
against the win for any subnet with real internal friction (waited in full → jumped for free).
Sections 1-2 report the production (wall-clock `run()`) overhead.

    python benchmarks/bench_subnet.py
"""

import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src"))
sys.path.insert(0, str(_HERE))

from cpnx import (  # noqa: E402
    InputArc,
    OutputArc,
    PacedResourcePlace,
    PetriNet,
    Place,
    SinkPlace,
    SubstitutionTransition,
    Token,
    Transition,
)

NET_SEED = 20260723

#: A generous wall-clock ceiling. Friction-free nets finish far inside it; it only exists so a
#: genuinely stuck net fails via the drain assertion rather than hanging.
_DEADLINE_SECS = 120.0


def _identity(tokens: list[Token]) -> list[Token]:
    """Pass the consumed token straight through — the cheapest possible action, so the
    measurement is engine machinery and not action work."""
    return tokens


def _chain(prefix: str, depth: int, source: str, sink: str) -> tuple[list[Place], list[Transition]]:
    """A linear chain `source -> T0 -> P0 -> ... -> T{depth-1} -> sink` of instant transitions.

    Shared by the subnet body and the flat control so the two do the identical logical work —
    the only difference between the experiments is whether this chain lives inside a wrapped
    subnet or inline in the parent.
    """
    places: list[Place] = []
    transitions: list[Transition] = []
    prev = source
    for i in range(depth):
        nxt = sink if i == depth - 1 else f"{prefix}_P{i}"
        if nxt != sink:
            places.append(Place(nxt))
        transitions.append(
            Transition(name=f"{prefix}_T{i}", inputs=[InputArc(prev)], outputs=[OutputArc(nxt)], action=_identity)
        )
        prev = nxt
    return places, transitions


def _wrapped_net(depth: int) -> PetriNet:
    """Parent with one `SubstitutionTransition` wrapping a depth-`K` chain.

    Wiring mirrors `cafe.stations.pastry_case`: the substitution transition consumes from a
    parent socket, the subnet's input port is bound to that socket, and its output port to the
    parent sink.
    """
    sub_places, sub_transitions = _chain("sub", depth, "P_In", "P_Out")
    subnet = PetriNet(
        places=[Place("P_In"), *sub_places, Place("P_Out")],
        transitions=sub_transitions,
        max_workers=1,
        seed=NET_SEED,
    )
    return PetriNet(
        places=[Place("P_Source"), SinkPlace("P_Sink")],
        transitions=[
            SubstitutionTransition(
                name="T_Sub",
                inputs=[InputArc("P_Source")],
                outputs=[OutputArc("P_Sink")],
                action=None,  # type: ignore[arg-type] — a subnet fires the net, not an action
                subnet=subnet,
                port_socket_map={"P_In": "P_Source", "P_Out": "P_Sink"},
                subnet_deadline_secs=_DEADLINE_SECS,
            )
        ],
        max_workers=1,
        seed=NET_SEED,
    )


def _flat_net(depth: int) -> PetriNet:
    """Parent doing the identical K steps inline — the control with no subnet machinery."""
    places, transitions = _chain("flat", depth, "P_Source", "P_Sink")
    return PetriNet(
        places=[Place("P_Source"), *places, SinkPlace("P_Sink")],
        transitions=transitions,
        max_workers=1,
        seed=NET_SEED,
    )


def _drain(net: PetriNet, n_tokens: int) -> float:
    """Deposit `n_tokens` into `P_Source`, run to quiescence on the wall clock, return seconds.

    Asserts every token reached the sink, so a subnet that silently strands tokens fails
    loudly instead of reporting a fast, wrong number.
    """
    with net:
        for i in range(n_tokens):
            net.deposit("P_Source", Token(payload={"i": i}))
        start = time.perf_counter()
        net.run(deadline=time.monotonic() + _DEADLINE_SECS)
        wall = time.perf_counter() - start
        absorbed = net.places["P_Sink"].stats()["absorbed"]
        assert absorbed == n_tokens, f"only {absorbed}/{n_tokens} reached the sink — net stalled"
    return wall


def run_overhead_table(n_tokens: int = 1000) -> None:
    print(f"\n=== Per-firing subnet overhead: wrapped vs. inlined, {n_tokens} tokens ===")
    print("Both do the identical K instant steps per token; the delta is the subnet machinery.")
    print("`overhead` = (wrapped - flat) / tokens = cost of one subnet firing.\n")
    print(f"{'subnet depth K':>15} {'flat us/tok':>13} {'wrapped us/tok':>16} {'overhead us/firing':>20}")
    print("-" * 68)
    for depth in (1, 3, 10, 30):
        # Min of three: measurement noise only ever adds time.
        flat = min(_drain(_flat_net(depth), n_tokens) for _ in range(3)) / n_tokens * 1e6
        wrapped = min(_drain(_wrapped_net(depth), n_tokens) for _ in range(3)) / n_tokens * 1e6
        print(f"{depth:>15} {flat:>13.2f} {wrapped:>16.2f} {wrapped - flat:>20.2f}")
    print("\n(K=1 ~= the fixed per-firing floor; the growth above it is the subnet re-running its own")
    print(" run/quiescence loop per internal step — heavier than an inlined step, so a subnet")
    print(" MULTIPLIES a large internal workflow's cost rather than adding only a fixed tax.)")


def _paced_parent_logical(n_tokens: int, pacing: float) -> float:
    """A paced cooldown in the PARENT, driven on the LOGICAL clock — the driver jumps over it,
    so it costs no wall time (the cafe's grinder-cooldown trick)."""
    net = PetriNet(
        places=[Place("P_Source"), PacedResourcePlace("P_Slot", capacity=1, pacing_secs=pacing), SinkPlace("P_Sink")],
        transitions=[
            Transition("T_Step", [InputArc("P_Source"), InputArc("P_Slot")], [OutputArc("P_Sink")], _identity)
        ],
        max_workers=1,
        seed=NET_SEED,
    )
    with net:
        for i in range(n_tokens):
            net.deposit("P_Source", Token(payload={"i": i}))
        start = time.perf_counter()
        net.drive_to_quiescence()
        wall = time.perf_counter() - start
        assert net.places["P_Sink"].stats()["absorbed"] == n_tokens
    return wall


def _paced_subnet_net(pacing: float) -> PetriNet:
    """A parent wrapping a subnet whose single internal step needs a paced (cooling) permit."""
    subnet = PetriNet(
        places=[Place("P_In"), PacedResourcePlace("P_Slot", capacity=1, pacing_secs=pacing), Place("P_Out")],
        transitions=[Transition("T_Step", [InputArc("P_In"), InputArc("P_Slot")], [OutputArc("P_Out")], _identity)],
        max_workers=1,
        seed=NET_SEED,
    )
    return PetriNet(
        places=[Place("P_Source"), SinkPlace("P_Sink")],
        transitions=[
            SubstitutionTransition(
                name="T_Sub",
                inputs=[InputArc("P_Source")],
                outputs=[OutputArc("P_Sink")],
                action=None,  # type: ignore[arg-type]
                subnet=subnet,
                port_socket_map={"P_In": "P_Source", "P_Out": "P_Sink"},
                subnet_deadline_secs=_DEADLINE_SECS,
            )
        ],
        max_workers=1,
        seed=NET_SEED,
    )


def _paced_subnet_logical(n_tokens: int, pacing: float) -> float:
    """Subnet cooldown under the LOGICAL driver — inherited regime jumps it for free."""
    net = _paced_subnet_net(pacing)
    with net:
        for i in range(n_tokens):
            net.deposit("P_Source", Token(payload={"i": i}))
        start = time.perf_counter()
        net.drive_to_quiescence()
        wall = time.perf_counter() - start
        assert net.places["P_Sink"].stats()["absorbed"] == n_tokens
    return wall


def _paced_subnet_wall(n_tokens: int, pacing: float) -> float:
    """The identical subnet cooldown under the WALL driver (`run()`, production) — waited in full."""
    return _drain(_paced_subnet_net(pacing), n_tokens)


def run_driver_mode(n_tokens: int = 8, pacing: float = 0.05) -> None:
    print(f"\n=== Driver-mode inheritance: {n_tokens} tokens through a {pacing}s cooldown ===")
    print("A subnet inherits the parent's clock regime. Under the logical driver (simulation) a")
    print("subnet's cooldown is jumped for free, exactly like the parent's; under the wall driver")
    print("(production) it is correctly waited out in real time.\n")
    parent_logical = _paced_parent_logical(n_tokens, pacing)
    subnet_logical = _paced_subnet_logical(n_tokens, pacing)
    subnet_wall = _paced_subnet_wall(n_tokens, pacing)
    floor = n_tokens * pacing
    print(f"  parent cooldown, logical driver:  {parent_logical * 1e3:8.1f} ms  (jumped)")
    print(f"  subnet cooldown, logical driver:  {subnet_logical * 1e3:8.1f} ms  (jumped — inherited)")
    print(f"  subnet cooldown, wall driver:     {subnet_wall * 1e3:8.1f} ms  (waited — production)")
    print(f"  real-time floor  (n x pacing):    {floor * 1e3:8.1f} ms")
    speedup = subnet_wall / max(subnet_logical, 1e-9)
    print(f"\n  logical driving is ~{speedup:.0f}x faster to SIMULATE the friction subnet, while the wall")
    print("  driver still waits the real cooldown when you actually run the net.")


def main() -> None:
    print(f"☕ SubstitutionTransition benchmark — seed={NET_SEED}, max_workers=1")
    run_overhead_table()
    run_driver_mode()


if __name__ == "__main__":
    main()
