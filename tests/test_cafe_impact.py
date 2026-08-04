"""Pin the colour-domain blast-radius tracer against the REAL ☕ Concurrency Cafe.

The regression anchor for ``cpnx.analysis`` — the companion to ``tests/test_cafe_lint.py``.
Where that file asserts the linter flags each hazard, this one asserts the *forward
slice* (``net.trace_impact``) reports the exact downstream cone the hand-wired station
topology implies, and that ``net.risk_report`` fuses the two.

Expected sets are computed by reading the station wiring (``benchmarks/cafe/``), not by
snapshotting the whole net, so a genuine re-wiring — not a cosmetic edit — is what would
trip these. Key facts used below (all in ``cafe.stations.decidability_hazards`` and
``cafe.transitions``):

* ``T_Stock_Check_Grind`` outputs to ``P_Ground_Coffee``;
* ``P_Ground_Coffee`` is consumed by ``T_Pull_Shot`` (base) and ``T_Loyalty_Pull`` (hazard);
* ``P_Ground_Coffee`` is the only place on a hazard's radius with a declared ``color_set``
  (``{"ground_coffee"}``), which is what makes it the colour-pruning demo.

The cafe lives under ``benchmarks/`` (not on the pytest pythonpath), shimmed in the same
way ``tests/test_cafe_lint.py`` does.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))

from cafe import build_cafe  # noqa: E402

# The four decidability hazards enabled by ``hazards=True``.
_HAZARDS = ("T_Loyalty_Pull", "T_Stock_Check_Grind", "T_Quality_Hold", "T_Happy_Hour_Serve")


# --- forward blast radius over the real topology -------------------------------------


def test_stock_check_grind_radius_matches_wiring():
    """``T_Stock_Check_Grind`` grinds into ``P_Ground_Coffee``; assert the cone that implies.

    It outputs to ``P_Ground_Coffee``, consumed by ``T_Pull_Shot`` and ``T_Loyalty_Pull``,
    both of which feed ``P_Order_Tray`` — so the serve/QC transitions on the tray are all
    reachable. This is the deepest hazard cone in the net.
    """
    net = build_cafe(hazards=True)
    impact = net.trace_impact("T_Stock_Check_Grind")

    # The direct output, then the two consumers of P_Ground_Coffee.
    assert "P_Ground_Coffee" in impact.places
    assert {"T_Pull_Shot", "T_Loyalty_Pull"} <= impact.transitions
    # Both of those feed the tray, so the tray and everything draining it is reachable.
    assert "P_Order_Tray" in impact.places
    assert {"T_Serve_Drink", "T_Quality_Hold", "T_Happy_Hour_Serve"} <= impact.transitions
    assert {"P_Served", "P_QC_Bench"} <= impact.places

    # A forward slice excludes its own origin even though a cycle can lead back.
    assert "T_Stock_Check_Grind" not in impact.transitions
    # And edges are directed hops that actually name the first grind output.
    assert ("T_Stock_Check_Grind", "P_Ground_Coffee") in impact.edges


def test_loyalty_pull_radius_reaches_the_tray_drains():
    """``T_Loyalty_Pull`` pulls grounds onto the tray, so it reaches every tray consumer."""
    net = build_cafe(hazards=True)
    impact = net.trace_impact("T_Loyalty_Pull")

    assert "P_Order_Tray" in impact.places
    assert {"T_Serve_Drink", "T_Quality_Hold", "T_Happy_Hour_Serve"} <= impact.transitions
    assert {"P_Served", "P_QC_Bench"} <= impact.places
    # It sits upstream of the tray, not upstream of the grinder — no grind cone.
    assert "P_Ground_Coffee" not in impact.places
    assert "T_Pull_Shot" not in impact.transitions
    assert "T_Loyalty_Pull" not in impact.transitions


def test_terminal_hazards_have_shallow_radii():
    """The two tray→sink hazards reach exactly one sink and no further transition."""
    net = build_cafe(hazards=True)

    qc = net.trace_impact("T_Quality_Hold")
    assert qc.places == frozenset({"P_QC_Bench"})
    assert qc.transitions == frozenset()

    happy = net.trace_impact("T_Happy_Hour_Serve")
    assert happy.places == frozenset({"P_Served"})
    assert happy.transitions == frozenset()


def test_unknown_transition_raises_keyerror():
    """Tracing a name the net does not know is a ``KeyError``, per the frozen API."""
    net = build_cafe(hazards=True)
    try:
        net.trace_impact("T_Not_A_Real_Transition")
    except KeyError:
        pass
    else:  # pragma: no cover - the assert above should always fire
        raise AssertionError("expected KeyError for an unknown transition name")


# --- colour pruning is a strict cone-of-influence reduction --------------------------


def test_colour_pruning_is_a_strict_subset():
    """Colour scoping prunes the cone in two distinct ways over this topology.

    Two places on ``T_Stock_Check_Grind``'s radius carry a ``color_set``:
    ``P_Ground_Coffee`` (``{"ground_coffee"}``) at the first hop, and
    ``P_Espresso_Machine`` (``{"resource"}``) reached through ``T_Pull_Shot``'s permit
    **self-loop**. So:

    * ``colors={"ground_coffee"}`` keeps the data cone but prunes the resource pool it
      cannot carry — a strict subset of the universal trace in *places*, same
      *transitions* (``T_Pull_Shot`` still consumes a matching ``ground_coffee`` token);
    * ``colors={"nonexistent"}`` is disjoint even from the first hop, collapsing the
      whole trace to empty.
    """
    net = build_cafe(hazards=True)
    universal = net.trace_impact("T_Stock_Check_Grind")
    matching = net.trace_impact("T_Stock_Check_Grind", colors={"ground_coffee"})
    pruned = net.trace_impact("T_Stock_Check_Grind", colors={"nonexistent"})

    # The resource pool is on the universal radius (via the T_Pull_Shot self-loop) ...
    assert "P_Espresso_Machine" in universal.places
    # ... and the ground-coffee scope prunes it while keeping the data cone.
    assert matching.transitions == universal.transitions
    assert matching.places < universal.places
    assert "P_Espresso_Machine" not in matching.places

    # A colour disjoint from even the first hop collapses the whole cone.
    assert pruned.places < matching.places
    assert pruned.places == frozenset() and pruned.transitions == frozenset()
    # The override is reflected on the returned map.
    assert pruned.colors == frozenset({"nonexistent"})


# --- risk_report fuses the linter with the tracer ------------------------------------


def test_risk_report_lists_every_hazard_with_a_blast_radius():
    """The hazards trip the linter, so each appears in ``impact_maps`` with a category."""
    net = build_cafe(hazards=True)
    report = net.risk_report()

    # Every hazard is linted-dirty, so every hazard carries an impact map.
    assert set(_HAZARDS) <= set(report["impact_maps"])
    # No base (non-hazard) transition is dirty or colour-annotated, so none leak in.
    assert set(report["impact_maps"]) == set(_HAZARDS)

    flagged = {f["transition"] for f in report["findings"]}
    assert set(_HAZARDS) <= flagged
    # At least one finding carries a lint category (they all do).
    categories = {finding["category"] for f in report["findings"] for finding in f["findings"]}
    assert categories, "expected at least one lint category across the hazard findings"

    # The fused map for the deepest hazard names its downstream cone, JSON-serialisably.
    grind_map = report["impact_maps"]["T_Stock_Check_Grind"]
    assert "T_Loyalty_Pull" in grind_map["transitions"]
    assert grind_map["origin"] == "T_Stock_Check_Grind"
