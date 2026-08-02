"""Run the best-effort side-effect linter across the ☕ Concurrency Cafe.

Three parts:

  A. Lint the full deterministic cafe (every legitimate station on) — expect a clean
     report, demonstrating the linter does not cry wolf on a large hand-written net.
  B. Lint the cafe with the ⚠️ decidability hazards enabled — each deliberate
     anti-pattern is flagged, in context, with its category.
  C. Drive the network hazard against a local mock loyalty endpoint, showing it is
     genuinely runnable (real socket, real latency) while still being flagged in A/B.

Run it::

    python benchmarks/lint_cafe.py
"""

import os
import sys
import time
import warnings
from pathlib import Path

if __name__ == "__main__":  # pragma: no cover - path shim for standalone execution
    _here = Path(__file__).resolve().parent
    sys.path.insert(0, str(_here.parent / "src"))
    sys.path.insert(0, str(_here))

from cafe import build_cafe  # noqa: E402
from cafe.stations import decidability_hazards as hazards  # noqa: E402

from cpnx import Token  # noqa: E402
from cpnx.linting import CpnxLintWarning, lint_callable  # noqa: E402

_ALL_LEGIT = dict(
    cold_brew=True,
    cold_brew_key=True,
    batch_triage=True,
    decaf=True,
    knock_box=True,
    specials_board=True,
    eighty_six=True,
    cupping=True,
    pastry_case=True,
)


def _selection_callables(net):
    for tname, t in net.transitions.items():
        if t.guard is not None:
            yield f"{tname}.guard", t.guard
        if getattr(t, "binding_priority_key", None) is not None:
            yield f"{tname}.binding_priority_key", t.binding_priority_key
        for i, arc in enumerate(t.inputs):
            if getattr(arc, "key", None) is not None:
                yield f"{tname}.inputs[{i}({arc.place})].key", arc.key
            if getattr(arc, "filter", None) is not None:
                yield f"{tname}.inputs[{i}({arc.place})].filter", arc.filter
        for i, arc in enumerate(t.outputs):
            if getattr(arc, "condition", None) is not None:
                yield f"{tname}.outputs[{i}({arc.place})].condition", arc.condition


def _lint_report(title: str, **flags) -> int:
    print("=" * 78)
    print(title)
    print("=" * 78)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        net = build_cafe(**flags)
    warned = len([w for w in caught if issubclass(w.category, CpnxLintWarning)])

    callables = list(_selection_callables(net))
    flagged = 0
    for label, fn in callables:
        findings = lint_callable(fn)
        if findings:
            flagged += 1
            detail = ", ".join(f"{f.category}:{f.symbol}" for f in findings)
            print(f"  [HIT] {label}  <- {detail}")
        else:
            print(f"  [ok ] {label}")
    print(f"\n  transitions={len(net.transitions)}  selection callables={len(callables)}  "
          f"flagged={flagged}  construction warnings={warned}")
    verdict = "CLEAN — no trouble spots." if flagged == 0 else f"{flagged} trouble spot(s) flagged."
    print(f"  => {verdict}\n")
    return flagged


def _loyalty_demo() -> None:
    print("=" * 78)
    print("PART C — the network hazard, run against a local mock loyalty endpoint")
    print("=" * 78)
    with hazards.loyalty_stub(delay=0.02) as addr:
        print(f"  mock loyalty endpoint listening on {addr[0]}:{addr[1]}")
        for card in ("VIP-777777", "walk-in", ""):
            t0 = time.perf_counter()
            pri = hazards.loyalty_priority([Token(payload={"card": card})])
            dt = (time.perf_counter() - t0) * 1000
            tier = "VIP (sorts first)" if pri == 0 else "walk-in"
            print(f"    card={card!r:14} -> priority={pri} ({tier})  [{dt:.1f} ms round-trip]")
    print("\n  A real socket round-trip decided a binding's priority — which is exactly")
    print("  why the linter flags it: enabling now depends on a remote service.\n")


def _randomorg_demo() -> None:
    print("=" * 78)
    print("PART D — true external entropy + real WAN latency from random.org (opt-in)")
    print("=" * 78)
    if os.environ.get("CPNX_DEMO_RANDOM_ORG", "").strip().lower() not in {"1", "true", "yes", "on"}:
        print("  skipped — set CPNX_DEMO_RANDOM_ORG=1 to make ONE well-behaved call to random.org.\n")
        return

    quota = hazards.fetch_randomorg_quota()
    if quota is not None:
        print(f"  quota check: {quota} bits remaining today")
        if quota < 1000:
            print("  quota low — skipping the draw to stay a good citizen.\n")
            return

    t0 = time.perf_counter()
    outcome, note = hazards.fetch_randomorg_outcome()
    dt = (time.perf_counter() - t0) * 1000
    print(f"  one loyalty lookup — one real round-trip  [{dt:.0f} ms]")

    if note == hazards.RATE_LIMITED:
        print(f"  → {note}")
        print("    random.org throttled us — modeled as a valid net scenario (route a")
        print("    'rate-limited' token) rather than an error. That a net's liveness can")
        print("    hinge on someone else's quota is the whole cautionary point.\n")
        return
    if note != "ok":
        print(f"  → {note} (nothing drawn; the external dependency is exactly this fragile)\n")
        return

    print(f"  loyalty outcome, drawn from true atmospheric entropy: {outcome}\n")


if __name__ == "__main__":
    clean = _lint_report("PART A — full deterministic cafe (all legitimate stations on)", **_ALL_LEGIT)
    _lint_report("PART B — cafe with ⚠️ decidability hazards enabled", **_ALL_LEGIT, hazards=True)
    _loyalty_demo()
    _randomorg_demo()
    print("Summary: base cafe is lint-clean" if clean == 0 else "Summary: base cafe HAS findings (!)",
          "· hazards are flagged · network hazard runs against a local mock",
          "(random.org draw is opt-in via CPNX_DEMO_RANDOM_ORG).")
