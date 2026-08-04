"""Unit tests for :func:`cpnx.risk_report` — fusing lint findings with blast radius.

`risk_report` answers, in one JSON-serialisable dict, both "which transitions
reach outside the decidable core?" (the side-effect linter) and "how far can each
effect spread?" (the impact tracer). These tests pin:

* a side-effecting guard (``time.time()``) shows up in ``findings`` with the right
  category AND its name keys ``impact_maps``;
* a colour-annotated but lint-clean transition appears in ``impact_maps`` but NOT
  in ``findings``;
* the whole report (and each ``ImpactMap.to_dict()``) round-trips through JSON.

Constructing a guard that trips the linter emits a ``CpnxLintWarning``; we suppress
it at construction the way ``tests/test_linting.py`` does.
"""

import json
import time
import warnings

from cpnx import (
    InputArc,
    OutputArc,
    PetriNet,
    Place,
    Transition,
)
from cpnx.linting import NONDETERMINISM


def _passthrough(tokens):
    return list(tokens)


# Module-level guard so the linter has a recoverable source file (a lambda/closure
# defined inside a test would not be resolvable). `time.time()` is reliably flagged
# as NONDETERMINISM (see tests/test_linting.py::guard_clock_time).
def guard_clock_time(tokens):
    return time.time() > 0


def _build_net():
    """A net with one side-effecting transition and one clean colour-annotated one."""
    net = PetriNet()
    for p in ("src", "mid", "dst", "src2", "mid2"):
        net.add_place(Place(p))

    # `dirty`: a guard the linter flags. Construction would warn — suppress it.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        net.add_transition(
            Transition(
                "dirty",
                [InputArc("src")],
                [OutputArc("mid")],
                action=_passthrough,
                guard=guard_clock_time,
            )
        )
    net.add_transition(Transition("downstream", [InputArc("mid")], [OutputArc("dst")], action=_passthrough))

    # `annotated`: lint-clean but carries an explicit impacts_colors declaration.
    net.add_transition(
        Transition(
            "annotated",
            [InputArc("src2")],
            [OutputArc("mid2")],
            action=_passthrough,
            impacts_colors={"order"},
        )
    )
    return net


def test_side_effecting_transition_appears_in_findings_with_category():
    net = _build_net()
    report = net.risk_report()
    dirty = [f for f in report["findings"] if f["transition"] == "dirty"]
    assert len(dirty) == 1
    findings = dirty[0]["findings"]
    assert any(f["role"] == "guard" and f["category"] == NONDETERMINISM for f in findings)
    assert any("time.time" in f["symbol"] for f in findings)


def test_side_effecting_transition_also_keys_impact_maps():
    net = _build_net()
    report = net.risk_report()
    assert "dirty" in report["impact_maps"]
    # Its blast radius is its actual forward cone.
    dirty_map = report["impact_maps"]["dirty"]
    assert dirty_map["origin"] == "dirty"
    assert "downstream" in dirty_map["transitions"]
    assert set(dirty_map["places"]) == {"mid", "dst"}


def test_clean_annotated_transition_in_impact_maps_but_not_findings():
    net = _build_net()
    report = net.risk_report()
    assert "annotated" in report["impact_maps"]
    assert all(f["transition"] != "annotated" for f in report["findings"])


def test_clean_unannotated_transition_absent_from_both():
    net = _build_net()
    report = net.risk_report()
    # `downstream` is lint-clean and has no impacts_colors -> excluded from both sections.
    assert "downstream" not in report["impact_maps"]
    assert all(f["transition"] != "downstream" for f in report["findings"])


def test_report_round_trips_through_json():
    net = _build_net()
    report = net.risk_report()
    restored = json.loads(json.dumps(report))
    assert restored == report


def test_impact_map_to_dict_round_trips_through_json():
    net = _build_net()
    impact = net.trace_impact("dirty")
    as_dict = impact.to_dict()
    restored = json.loads(json.dumps(as_dict))
    assert restored == as_dict


def test_report_shape_is_well_formed():
    net = _build_net()
    report = net.risk_report()
    assert set(report.keys()) == {"findings", "impact_maps"}
    assert isinstance(report["findings"], list)
    assert isinstance(report["impact_maps"], dict)
    for entry in report["findings"]:
        assert set(entry.keys()) == {"transition", "findings"}
        for f in entry["findings"]:
            assert set(f.keys()) == {"role", "category", "symbol"}


def test_module_level_risk_report_matches_method():
    from cpnx import risk_report

    net = _build_net()
    assert risk_report(net) == net.risk_report()


def test_empty_net_report_is_empty():
    net = PetriNet()
    report = net.risk_report()
    assert report["findings"] == []
    assert report["impact_maps"] == {}


def test_risk_report_skips_transition_removed_after_snapshot(monkeypatch):
    """A transition vanishing between the lock-snapshot and its per-transition trace
    is skipped, not fatal. `risk_report` snapshots the transition list under the lock,
    releases it, then re-enters `trace_impact` (which re-acquires the lock and raises
    `KeyError` on an unknown name) per transition — a TOCTOU window on a live net. The
    guard catches that `KeyError` and continues rather than aborting the whole report.
    """
    import cpnx.analysis as analysis

    net = _build_net()
    real_trace = analysis.trace_impact

    def flaky_trace(n, name, **kwargs):
        if name == "dirty":
            raise KeyError(name)  # simulate a concurrent removal after the snapshot
        return real_trace(n, name, **kwargs)

    monkeypatch.setattr(analysis, "trace_impact", flaky_trace)
    report = net.risk_report()

    # The report still completes: the stale transition is absent from impact_maps,
    # its lint findings (gathered before the trace) survive, and other transitions
    # are traced normally.
    assert "dirty" not in report["impact_maps"]
    assert any(f["transition"] == "dirty" for f in report["findings"])
    assert "annotated" in report["impact_maps"]
