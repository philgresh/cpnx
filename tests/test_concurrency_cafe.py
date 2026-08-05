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
                # Orders enter through the single front door; T_Take_Order writes tickets.
                net.deposit("P_New_Order", Token(payload=payload))

            net.run(deadline=time.monotonic() + 3.0)

            # Every order must clear the front door (T_Take_Order), and at least one must
            # then be ground — a net that can't fire at all is a real regression. (We don't
            # assert an exact served count: the grinder's pacing cooldown and the ~15%
            # channeling failure make the precise number nondeterministic within a short
            # deadline.)
            assert len(net.marking["P_New_Order"]) == 0, "orders never left the front door"
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


def _return_arc(net, transition: str, pool: str):
    arcs = [a for a in net.transitions[transition].outputs if a.place == pool]
    return arcs[0] if arcs else None


def _strip_resource_returns(net) -> None:
    for t in net.transitions.values():
        t.outputs[:] = [a for a in t.outputs if not isinstance(net.places.get(a.place), ResourcePlace)]


class TestConcurrencyCafeResourceReturnModes:
    """ADR 0009: the cafe showcases all three resource-return modes in one net —
    explicit (scales), synthesized (grinder, group head), and implicit/opt-out (wand)."""

    def test_all_three_return_modes_coexist(self):
        net = build_cafe()
        net.validate()  # triggers synthesis

        # explicit — the scale permit's return arc is author-declared.
        scales = _return_arc(net, "T_Weigh_And_Grind", "P_Digital_Scales")
        assert scales is not None and scales.synthesized is False

        # synthesized (default) — grinder and group-head returns are added by the engine.
        grinder = _return_arc(net, "T_Weigh_And_Grind", "P_Burr_Grinder")
        assert grinder is not None and grinder.synthesized is True
        espresso = _return_arc(net, "T_Pull_Shot", "P_Espresso_Machine")
        assert espresso is not None and espresso.synthesized is True

        # implicit (opt-out) — the wand borrow is left off-arc.
        assert net.transitions["T_Steam_Milk"].auto_return_resources is False
        assert _return_arc(net, "T_Steam_Milk", "P_Steam_Wand") is None

    def test_only_the_wand_return_is_drawn_implicit(self):
        dot = build_cafe().to_dot()
        # Exactly one dashed implicit-return edge — the opted-out wand.
        assert dot.count("return (implicit)") == 1
        assert '"T_Steam_Milk" -> "P_Steam_Wand" [label="return (implicit)", style=dashed];' in dot
        # The synthesized group-head return is a solid, ordinary arc.
        assert '"T_Pull_Shot" -> "P_Espresso_Machine"' in dot

    def test_terminal_places_are_sunk_to_the_far_right(self):
        dot = build_cafe().to_dot()
        rank = next((line for line in dot.splitlines() if "rank=sink" in line), "")
        # The served-drinks sink and the dead-letter bin both end the flow, so both are sunk.
        assert '"P_Served";' in rank
        assert '"P_Trash_Can";' in rank
        # A resource pool (consumed by a borrow) is never terminal.
        assert '"P_Espresso_Machine";' not in rank

    def test_synthesis_preserves_work_vs_all_explicit(self):
        """A cafe relying on synthesis does the same work as one where every borrow is explicit."""
        orders = [
            {"ratio": "1:2", "weight_g": 18, "dairy_free": (i % 2 == 0), "mobile_pickup": (i % 3 == 0)}
            for i in range(60)
        ]

        def work(strip_and_optin: bool) -> tuple[int, int]:
            net = build_cafe(channel_failure_rate=0.0, seed=4242, max_workers=1)
            if strip_and_optin:
                # Force every borrow onto the synthesis path: drop the explicit scale arc and
                # opt the wand back in, so all four returns are engine-synthesized.
                _strip_resource_returns(net)
                for t in net.transitions.values():
                    t.auto_return_resources = True
            for payload in orders:
                net.deposit("P_New_Order", Token(payload=payload))
            steps = net.drive_to_quiescence().steps
            return steps, net.places["P_Served"].stats()["absorbed"]

        assert work(strip_and_optin=True) == work(strip_and_optin=False)
