"""Trace the colour-domain **blast radius** of the ☕ Concurrency Cafe's hazards.

The companion to ``benchmarks/lint_cafe.py``. Where the linter answers *which*
transitions reach outside the decidable core (network / database / clock /
randomness), this driver answers the second safety question — *how far downstream can
each one's effect spread?* — by walking the static net topology forward from a
transition and returning the exact set of places and transitions it can reach.

That forward walk is a **cone-of-influence (COI) reduction** over the net's
place/transition graph: it keeps only the elements a "real observer" can transitively
reach, and prunes a colour-constrained place whose ``color_set`` cannot carry a traced
colour (Telbisz, Bajczi, Szekeres & Vörös, "On-the-fly Cone-of-Influence Reduction for
Model Checking Concurrent Software," SPIN'25; Watanabe, Nishizawa & Takaki, "A
Coalgebraic Representation of Reduction by Cone of Influence," *ENTCS* 164 (2006)
177–194). The trace is a deliberate over-approximation — it may name a place a more
precise analysis would drop, but never omits one that is genuinely reachable.

Three parts, on the hazards-enabled cafe:

  A. The default (universal) blast radius of each of the four decidability hazards —
     the impacted place/transition counts and their sorted names. The cafe's hazards
     predate ``impacts_colors``, so an undeclared trace is universal (no pruning).
  B. The colour-pruning contrast on ``T_Stock_Check_Grind`` — the one hazard whose
     immediate output place (``P_Ground_Coffee``) declares a ``color_set``. Universal
     vs ``colors={"ground_coffee"}`` (the matching colour keeps the whole cone) vs
     ``colors={"nonexistent"}`` (a colour the place cannot carry collapses the cone to
     nothing at the first hop). The cone-of-influence reduction made concrete.
  C. ``net.risk_report()`` as pretty JSON — each flagged hazard's lint category AND its
     blast radius fused into one report, the two safety prongs side by side.

Deterministic and offline: tracing reads only the net's structure, never fires a
transition, and touches no socket — the mock loyalty endpoint and the random.org path
of ``lint_cafe.py`` are not exercised here.

Run it::

    python benchmarks/impact_cafe.py
"""

import json
import sys
import warnings
from pathlib import Path

if __name__ == "__main__":  # pragma: no cover - path shim for standalone execution
    _here = Path(__file__).resolve().parent
    sys.path.insert(0, str(_here.parent / "src"))
    sys.path.insert(0, str(_here))

from cafe import build_cafe  # noqa: E402

# The four decidability hazards, in the order build_cafe wires them, with the one-word
# reason each is a hazard (the linter category) for the PART A header line.
_HAZARDS = [
    ("T_Loyalty_Pull", "network"),
    ("T_Stock_Check_Grind", "database"),
    ("T_Quality_Hold", "randomness"),
    ("T_Happy_Hour_Serve", "clock"),
]


def _build_hazards_cafe():
    """Build the hazards-enabled cafe, swallowing the expected lint warnings.

    Constructing the hazard gallery emits one :class:`CpnxLintWarning` per hazard
    callable — that is ``lint_cafe.py``'s subject, not this driver's, so silence it to
    keep the impact report clean and deterministic.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return build_cafe(hazards=True)


def _part_a(net) -> None:
    print("=" * 78)
    print("PART A — default (universal) blast radius of each ⚠️ decidability hazard")
    print("=" * 78)
    print("  Undeclared origins (no impacts_colors) trace every colour — no pruning.\n")
    for name, why in _HAZARDS:
        impact = net.trace_impact(name)
        print(f"  {name}  ({why})")
        print(f"    impacted places      ({len(impact.places):>2}): {', '.join(sorted(impact.places)) or '—'}")
        print(f"    impacted transitions ({len(impact.transitions):>2}): "
              f"{', '.join(sorted(impact.transitions)) or '—'}")
        print(f"    origin excluded from its own radius: {name not in impact.transitions}\n")


def _part_b(net) -> None:
    print("=" * 78)
    print("PART B — colour pruning: the cone-of-influence reduction made concrete")
    print("=" * 78)
    origin = "T_Stock_Check_Grind"
    print(f"  {origin} reaches two colour-constrained places: P_Ground_Coffee")
    print("  ({'ground_coffee'}) at the first hop, and P_Espresso_Machine ({'resource'})")
    print("  via T_Pull_Shot's permit self-loop. Scoping the trace prunes what a colour")
    print("  cannot carry.\n")
    scopes = [
        (None, "universal (no pruning)"),
        ({"ground_coffee"}, "data cone kept; the {'resource'} pool is pruned"),
        ({"nonexistent"}, "disjoint even from the first hop — collapses to empty"),
    ]
    for colors, label in scopes:
        impact = net.trace_impact(origin, colors=colors)
        shown = "None" if colors is None else "{" + ", ".join(sorted(colors)) + "}"
        print(f"  colors={shown:<16} -> {len(impact.places)} place(s), "
              f"{len(impact.transitions)} transition(s)   [{label}]")
    universal = net.trace_impact(origin)
    pruned = net.trace_impact(origin, colors={"nonexistent"})
    print(f"\n  radius shrank from {len(universal.transitions)} transitions to "
          f"{len(pruned.transitions)}: the disjoint colour is a strict subset "
          f"({pruned.transitions < universal.transitions}).\n")


def _part_c(net) -> None:
    print("=" * 78)
    print("PART C — risk_report(): lint category + blast radius, fused per hazard")
    print("=" * 78)
    report = net.risk_report()
    flagged = [f["transition"] for f in report["findings"]]
    print(f"  flagged transitions: {', '.join(flagged)}")
    print(f"  impact_maps cover:   {', '.join(sorted(report['impact_maps']))}\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    print()


if __name__ == "__main__":
    cafe = _build_hazards_cafe()
    _part_a(cafe)
    _part_b(cafe)
    _part_c(cafe)
    print("Summary: each hazard's forward blast radius is bounded by the net's static "
          "topology · colour")
    print("declarations prune it (a cone-of-influence reduction) · risk_report fuses "
          "the linter's")
    print("'which transitions reach outside the decidable core' with the tracer's "
          "'how far can each spread'.")
