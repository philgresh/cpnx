"""Regression tests for `SubstitutionTransition` under both drivers.

The headline case (`test_logical_driver_fully_drains_a_subnet`) reproduces a silent
correctness bug: driving a wrapped net on the **logical** clock
(`PetriNet.drive_to_quiescence`) used to strand tokens inside the subnet and report success
anyway (`is_quiescent()` True, a fast wrong result). The cause was clock coupling —
`_sync_subnet_time` pushed the parent's logical time onto the subnet via `advance_time`, but a
subnet fires once per binding, so the second firing at the same parent instant moved the subnet
clock backward-or-equal and `advance_time` raised; that ValueError surfaced as a firing failure,
rolled the transition back, and left the already-deposited copy stuck in the subnet's port.

The fix isolates the subnet's clock entirely: it always runs on its own wall clock, and the
parent's clock never crosses the port boundary. These tests pin that.
"""

import sys
import time
from pathlib import Path

import pytest

from cpnx import (
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

# Reuse the benchmark's synthetic chain builders — they are the canonical wrapped/flat pair.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))
import bench_subnet  # noqa: E402


def _identity(tokens: list[Token]) -> list[Token]:
    return tokens


@pytest.mark.parametrize("depth", [1, 3, 10])
def test_logical_driver_fully_drains_a_subnet(depth):
    """The exact repro: N tokens through a wrapped subnet, driven on the logical clock, must all
    reach the sink — not strand inside the subnet while `is_quiescent()` claims completion."""
    net = bench_subnet._wrapped_net(depth)
    n = 20
    with net:
        for i in range(n):
            net.deposit("P_Source", Token(payload={"i": i}))
        result = net.drive_to_quiescence()
        assert net.places["P_Sink"].stats()["absorbed"] == n, "tokens stranded in the subnet"
        assert net.is_quiescent()
        assert result.steps == n, "one firing per token — no rollback/re-fire churn"
        # Nothing left behind in the subnet's internal places.
        subnet = net.transitions["T_Sub"].subnet
        assert all(len(p) == 0 for name, p in subnet.places.items())


def test_logical_and_wall_drivers_agree_on_the_marking():
    """The logical driver must produce the same fixed-point marking as the wall-clock driver — it
    is the deterministic driver, and a subnet must not break that."""
    n = 20
    logical = bench_subnet._wrapped_net(3)
    with logical:
        for i in range(n):
            logical.deposit("P_Source", Token(payload={"i": i}))
        logical.drive_to_quiescence()
        logical_served = logical.places["P_Sink"].stats()["absorbed"]

    wall = bench_subnet._wrapped_net(3)
    with wall:
        for i in range(n):
            wall.deposit("P_Source", Token(payload={"i": i}))
        wall.run(deadline=time.monotonic() + 30.0)
        wall_served = wall.places["P_Sink"].stats()["absorbed"]

    assert logical_served == wall_served == n


def _paced_subnet_net(pacing: float) -> PetriNet:
    """A parent wrapping a subnet whose single step needs a paced (cooling) permit."""
    subnet = PetriNet(
        places=[Place("P_In"), PacedResourcePlace("P_Slot", capacity=1, pacing_secs=pacing), Place("P_Out")],
        transitions=[Transition("T_Step", [InputArc("P_In"), InputArc("P_Slot")], [OutputArc("P_Out")], _identity)],
    )
    return PetriNet(
        places=[Place("P_Source"), SinkPlace("P_Sink")],
        transitions=[
            SubstitutionTransition(
                name="T_Sub",
                inputs=[InputArc("P_Source")],
                outputs=[OutputArc("P_Sink")],
                action=_identity,
                subnet=subnet,
                port_socket_map={"P_In": "P_Source", "P_Out": "P_Sink"},
                subnet_deadline_secs=30.0,
            )
        ],
    )


def test_subnet_cooldown_is_free_under_logical_driver():
    """A subnet with real internal friction drains under the logical driver, AND its cooldown is
    jumped for free — the parent's logical-clock discipline is inherited by the subnet (on the
    subnet's own clock). A large pacing would take real seconds if it were waited out; here the
    whole drive must finish far inside that, proving the cooldown was skipped, not slept."""
    pacing = 5.0  # would be 5 s * n if waited on the wall clock — must NOT be
    n = 4
    net = _paced_subnet_net(pacing=pacing)
    with net:
        for i in range(n):
            net.deposit("P_Source", Token(payload={"i": i}))
        start = time.perf_counter()
        net.drive_to_quiescence()
        elapsed = time.perf_counter() - start
        assert net.places["P_Sink"].stats()["absorbed"] == n
        assert elapsed < 1.0, f"subnet cooldown was waited in real time ({elapsed:.1f}s), not jumped"


def test_subnet_cooldown_is_waited_on_the_wall_clock_in_production():
    """The counterpart: under the wall-clock driver (`run`, production), a subnet's cooldown IS
    waited out in real time — correct production semantics."""
    pacing = 0.05
    n = 3
    net = _paced_subnet_net(pacing=pacing)
    with net:
        for i in range(n):
            net.deposit("P_Source", Token(payload={"i": i}))
        start = time.perf_counter()
        net.run(deadline=time.monotonic() + 30.0)
        elapsed = time.perf_counter() - start
        assert net.places["P_Sink"].stats()["absorbed"] == n
        assert elapsed >= (n - 1) * pacing, "a real cooldown was skipped under the wall-clock driver"


def test_subnet_clock_value_stays_decoupled_across_many_firings():
    """The parent's logical *time value* must never leak into the subnet, however many firings —
    even though the subnet keeps its own (independent) logical clock under the logical driver."""
    net = bench_subnet._wrapped_net(2)
    net.advance_time(5000.0)  # parent far out on the logical clock
    with net:
        for i in range(10):
            net.deposit("P_Source", Token(payload={"i": i}))
        net.drive_to_quiescence()
        subnet = net.transitions["T_Sub"].subnet
        assert subnet._model_time != 5000.0, "parent's logical time value leaked into the subnet"
        assert net.places["P_Sink"].stats()["absorbed"] == 10
