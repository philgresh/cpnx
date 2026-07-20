"""Smoke tests for the ☕ Concurrency Cafe benchmark fixture (``benchmarks/concurrency_cafe.py``).

These assert the reference topology *builds*, *validates*, and *makes forward progress* — they
deliberately do NOT assert conservation, because the cafe transitions transform tokens (order ->
grounds -> espresso -> drink) rather than merely relocating fixed colours. Conservation is the
job of ``tests/test_state_machine.py``; here we only guard against the example rotting.
"""

import sys
import time
from pathlib import Path

from cpnx import (
    PacedResourcePlace,
    PetriNet,
    ResourcePlace,
    SinkPlace,
    ThresholdPlace,
    Token,
)

# The cafe lives under benchmarks/ (not a package, not on the pytest pythonpath), so add that
# directory to sys.path the same way the fixture itself shims in ``src`` for standalone runs.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))

from concurrency_cafe import build_cafe  # noqa: E402

ORDERS = [
    {"ratio": "1:2", "weight_g": 18, "dairy_free": True, "mobile_pickup": False},
    {"ratio": "1:2", "weight_g": 18, "dairy_free": False, "mobile_pickup": True},
    {"ratio": "1:2.5", "weight_g": 20, "dairy_free": False, "mobile_pickup": False},
]


class TestConcurrencyCafeBuilds:
    def test_build_returns_validated_net(self):
        net = build_cafe()
        assert isinstance(net, PetriNet)
        net.validate()  # raises on any structural problem

    def test_stations_have_expected_cpnx_types(self):
        net = build_cafe()
        assert isinstance(net.places["P_Digital_Scales"], ResourcePlace)
        assert net.places["P_Digital_Scales"].capacity == 3
        assert isinstance(net.places["P_Burr_Grinder"], PacedResourcePlace)
        assert net.places["P_Burr_Grinder"].capacity == 2  # default `grinders=2`
        assert isinstance(net.places["P_Espresso_Machine"], ResourcePlace)
        assert net.places["P_Espresso_Machine"].capacity == 2
        assert isinstance(net.places["P_Steam_Wand"], ResourcePlace)
        assert net.places["P_Steam_Wand"].capacity == 2
        assert isinstance(net.places["P_Order_Tray"], ThresholdPlace)
        assert net.places["P_Order_Tray"].threshold == 2
        assert net.places["P_Order_Tray"].bound == 6  # default `tray_bound=6`
        assert isinstance(net.places["P_Served"], SinkPlace)
        assert isinstance(net.places["P_Trash_Can"], SinkPlace)

    def test_grinders_kwarg_sizes_the_burr_grinder_pool(self):
        net = build_cafe(grinders=1)
        assert net.places["P_Burr_Grinder"].capacity == 1

    def test_tray_bound_kwarg_sets_the_order_tray_bound(self):
        net = build_cafe(tray_bound=None)
        assert net.places["P_Order_Tray"].bound is None

    def test_serve_drink_arc_carries_the_configured_settle_secs(self):
        net = build_cafe(tray_settle_secs=1.5)
        arc = next(a for a in net.transitions["T_Serve_Drink"].inputs if a.place == "P_Order_Tray")
        assert arc.settle_secs == 1.5


class TestConcurrencyCafeRuns:
    def test_orders_make_forward_progress(self):
        with build_cafe() as net:
            for payload in ORDERS:
                net.deposit("P_Ticket_Line", Token(payload=payload))

            net.run(deadline=time.monotonic() + 3.0)

            # The grinder is available at t=0, so at least one ticket must leave the line —
            # a net that can't fire at all is a real regression. (We don't assert an exact
            # served count: the grinder's pacing cooldown and the ~15% channeling failure
            # make the precise number nondeterministic within a short deadline.)
            remaining = len(net.marking["P_Ticket_Line"])
            assert remaining < len(ORDERS), "no order left the ticket line — cafe never fired"

            # A real conservation-derived bound (not a tautology): each grind firing produces
            # exactly one milk ticket (always steamed successfully, never trashed) and exactly
            # one grounds token (which becomes either one espresso tray arrival or one trashed
            # shot). So total tray arrivals == 2*grind_firings - trashed, and since a served
            # drink drains exactly 2 tray tokens, 2*served <= 2*grind_firings - trashed. Grind
            # firings can never exceed the number of orders (rework mutates a ticket in place
            # rather than creating new ones, and a ticket is ground exactly once), so
            # grind_firings <= len(ORDERS), giving 2*served + trashed <= 2*len(ORDERS).
            served = net.places["P_Served"].stats()["absorbed"]
            trashed = net.places["P_Trash_Can"].stats()["absorbed"]
            assert 2 * served + trashed <= 2 * len(ORDERS), (
                f"served={served}, trashed={trashed} exceed what {len(ORDERS)} orders could "
                "possibly have produced — a token was double-counted or conjured from nowhere"
            )
