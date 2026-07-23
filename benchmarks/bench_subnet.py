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
   wall-time delta per firing is the cost of the subnet machinery (deposit into ports, spin
   up `subnet.run()`, retrieve from ports, sync the clock) that inlining does not pay.

2. **Does that overhead scale with subnet size?** Sweep K. A fixed per-firing cost (spin-up /
   teardown only) stays flat in K; a per-internal-transition cost grows with it.

3. **The wall-clock leak.** A subnet's internal `run()` waits out *its own* cooldowns on the
   **real** clock. The parent's logical-clock driver (`drive_to_quiescence`) — the trick that
   makes the cafe's grinder cooldown cost nothing — cannot reach across the boundary. This
   section shows the same cooldown costing nothing in the parent and its full real time in a
   subnet.

Driver note — a finding in its own right
----------------------------------------
The logical-clock driver **does not compose with a `SubstitutionTransition`**: driving a
wrapped net with `PetriNet.drive_to_quiescence` strands tokens inside the subnet (measured:
14 of 20 stuck in the subnet's input port). The subnet runs on the wall clock while the
parent's logical clock is frozen and jumped, and the two time models do not reconcile. This
had never surfaced because `pastry_case` is not in the throughput sweep, so no logical-clock
benchmark had ever met a subnet. Consequently this script drives on the **wall clock**
(`PetriNet.run`), which is correct for a subnet; for the friction-free nets in sections 1-2
that still equals engine CPU, since there is nothing to wait out.

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


def _paced_subnet_wall(n_tokens: int, pacing: float) -> float:
    """The identical cooldown INSIDE a subnet. A subnet cannot be logical-clock-driven (it
    strands tokens — see the module docstring), so it is driven on the wall clock, where
    `subnet.run()` waits the cooldown out in full."""
    subnet = PetriNet(
        places=[Place("P_In"), PacedResourcePlace("P_Slot", capacity=1, pacing_secs=pacing), Place("P_Out")],
        transitions=[Transition("T_Step", [InputArc("P_In"), InputArc("P_Slot")], [OutputArc("P_Out")], _identity)],
        max_workers=1,
        seed=NET_SEED,
    )
    net = PetriNet(
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
    return _drain(net, n_tokens)


def run_wallclock_leak(n_tokens: int = 8, pacing: float = 0.05) -> None:
    print(f"\n=== The wall-clock leak: {n_tokens} tokens through a {pacing}s cooldown ===")
    print("The same cooldown, once in the parent and once inside a subnet. The parent's logical")
    print("driver jumps over its own friction (free); it cannot reach into a subnet, whose")
    print("run() waits the identical cooldown out in real time.\n")
    parent = _paced_parent_logical(n_tokens, pacing)
    subnet = _paced_subnet_wall(n_tokens, pacing)
    floor = n_tokens * pacing
    print(f"  cooldown in parent  (logical driver skips it): {parent * 1e3:8.1f} ms")
    print(f"  cooldown in subnet  (wall run waits it):       {subnet * 1e3:8.1f} ms")
    print(f"  real-time floor  (n x pacing):                 {floor * 1e3:8.1f} ms")
    print(f"\n  the subnet paid ~{subnet / max(parent, 1e-9):.0f}x the parent, landing on the real-time floor:")
    print("  the logical-clock benchmark trick does NOT extend across a subnet boundary.")


def main() -> None:
    print(f"☕ SubstitutionTransition benchmark — seed={NET_SEED}, max_workers=1, wall clock")
    run_overhead_table()
    run_wallclock_leak()


if __name__ == "__main__":
    main()
