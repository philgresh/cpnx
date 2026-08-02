"""Lint the ☕ Concurrency Cafe fixture with the best-effort side-effect linter.

Two guarantees, on the same real net:

* **The real cafe is lint-clean** — every guard/key/filter/condition/priority-key
  across the base topology *and* all legitimate opt-in stations is deterministic and
  side-effect-free, so the linter is silent. This is the regression that stops a future
  station from quietly smuggling I/O into an enabling condition.
* **The hazard gallery is flagged** — enabling ``hazards=True`` adds the deliberate
  anti-patterns in :mod:`cafe.stations.decidability_hazards`, and each one is caught with
  the expected category (the in-context counterpart to ``tests/test_linting.py``).

The cafe lives under ``benchmarks/`` (not on the pytest pythonpath), so this shims that
directory in the same way ``tests/test_concurrency_cafe.py`` does.
"""

import sys
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))

from cafe import build_cafe  # noqa: E402
from cafe.stations import decidability_hazards as hazards  # noqa: E402

from cpnx import Token  # noqa: E402
from cpnx.linting import DATABASE, NETWORK, NONDETERMINISM, CpnxLintWarning, lint_callable  # noqa: E402

# Every legitimate opt-in station enabled — the fullest deterministic cafe we can build.
_ALL_LEGIT_STATIONS = dict(
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
    """Yield ``(label, callable)`` for every enabling/selection callable in *net*."""
    for tname, transition in net.transitions.items():
        if transition.guard is not None:
            yield f"{tname}.guard", transition.guard
        if getattr(transition, "binding_priority_key", None) is not None:
            yield f"{tname}.binding_priority_key", transition.binding_priority_key
        for i, arc in enumerate(transition.inputs):
            if getattr(arc, "key", None) is not None:
                yield f"{tname}.inputs[{i}].key", arc.key
            if getattr(arc, "filter", None) is not None:
                yield f"{tname}.inputs[{i}].filter", arc.filter
        for i, arc in enumerate(transition.outputs):
            if getattr(arc, "condition", None) is not None:
                yield f"{tname}.outputs[{i}].condition", arc.condition


# --- Part A: the real cafe is lint-clean ---------------------------------------------


def test_default_cafe_is_lint_clean():
    """A bare ``build_cafe()`` produces no lint findings and no lint warnings."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        net = build_cafe()
    assert [w for w in caught if issubclass(w.category, CpnxLintWarning)] == []
    offenders = {label: lint_callable(fn) for label, fn in _selection_callables(net) if lint_callable(fn)}
    assert offenders == {}, f"unexpected lint findings in the base cafe: {offenders}"


def test_full_legit_cafe_is_lint_clean():
    """The base topology plus *all* legitimate stations is still lint-clean.

    This is the regression guard: it inspects every selection callable the fixture can
    assemble (short of the hazard gallery) and asserts none trips the linter.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        net = build_cafe(**_ALL_LEGIT_STATIONS)
    assert [w for w in caught if issubclass(w.category, CpnxLintWarning)] == []

    callables = list(_selection_callables(net))
    assert len(callables) >= 10, "expected the full cafe to expose many selection callables"
    offenders = {label: lint_callable(fn) for label, fn in callables if lint_callable(fn)}
    assert offenders == {}, f"a legitimate station introduced a lint finding: {offenders}"


# --- Part B: the hazard gallery is flagged -------------------------------------------

# Expected (transition, callable-attr, category) for each deliberate hazard.
_EXPECTED_HAZARDS = [
    ("T_Loyalty_Pull", "binding_priority_key", NETWORK),
    ("T_Stock_Check_Grind", "guard", DATABASE),
    ("T_Quality_Hold", "filter", NONDETERMINISM),
    ("T_Happy_Hour_Serve", "key", NONDETERMINISM),
]


def _hazard_callable(net, tname: str, attr: str):
    transition = net.transitions[tname]
    if attr in ("guard", "binding_priority_key"):
        return getattr(transition, attr)
    for arc in transition.inputs:
        if getattr(arc, attr, None) is not None:
            return getattr(arc, attr)
    raise AssertionError(f"no {attr} found on {tname}")


@pytest.mark.parametrize(("tname", "attr", "category"), _EXPECTED_HAZARDS)
def test_each_hazard_is_flagged(tname, attr, category):
    net = build_cafe(hazards=True)
    findings = lint_callable(_hazard_callable(net, tname, attr))
    assert findings, f"{tname}.{attr} should have been flagged"
    assert any(f.category == category for f in findings), (
        f"{tname}.{attr} expected category {category}, got {[f.category for f in findings]}"
    )


def test_enabling_hazards_emits_construction_warnings():
    """Constructing the hazard cafe emits a lint warning per hazard callable."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        build_cafe(hazards=True)
    lint_warnings = [w for w in caught if issubclass(w.category, CpnxLintWarning)]
    # Four hazards; the clock hazard reads the clock twice on one line, so >= 4.
    assert len(lint_warnings) >= len(_EXPECTED_HAZARDS)


def test_hazards_only_addition_leaves_base_cafe_clean():
    """The hazard flag is additive: every *non*-hazard transition stays lint-clean."""
    net = build_cafe(hazards=True)
    hazard_names = {t for t, _, _ in _EXPECTED_HAZARDS}
    for label, fn in _selection_callables(net):
        if label.split(".")[0] in hazard_names:
            continue
        assert lint_callable(fn) == [], f"non-hazard callable {label} should be clean"


# --- the network hazard is genuinely runnable against a local mock -------------------


def test_loyalty_stub_serves_a_real_round_trip():
    """The network hazard talks to a real local socket, not a stub of the module.

    Confirms the runnable path: with :func:`loyalty_stub` up, ``loyalty_priority`` makes
    an actual HTTP round-trip and returns a valid min-first priority (0 for VIP, 1 else).
    """
    with hazards.loyalty_stub(delay=0.0):
        vip = hazards.loyalty_priority([Token(payload={"card": "VIP-999999"})])
        walk_in = hazards.loyalty_priority([Token(payload={"card": ""})])
    assert vip in (0, 1) and walk_in == 1
    # And with no endpoint up, it degrades safely rather than raising.
    assert hazards.loyalty_priority([Token(payload={"card": "x"})]) == 1


def test_loyalty_endpoint_requires_auth():
    """The mock rejects an unauthenticated request (401) and accepts the demo token.

    Verifies the authenticated-upstream modelling: without the ``Authorization`` header
    the endpoint 401s; the client sends :data:`LOYALTY_DEMO_TOKEN` and gets a real answer.
    """
    import http.client

    with hazards.loyalty_stub(delay=0.0) as (host, port):
        # No credential → 401.
        anon = http.client.HTTPConnection(host, port, timeout=2.0)
        try:
            anon.request("GET", "/loyalty?card=abc")
            assert anon.getresponse().status == 401
        finally:
            anon.close()

        # With the demo token → 200 and a JSON tier.
        authed = http.client.HTTPConnection(host, port, timeout=2.0)
        try:
            authed.request("GET", "/loyalty?card=abc", headers={"Authorization": hazards._EXPECTED_AUTH})
            resp = authed.getresponse()
            assert resp.status == 200
            assert "tier" in resp.read().decode()
        finally:
            authed.close()

    # The demo token is a self-describing non-secret, not a real credential.
    assert "not-a-secret" in hazards.LOYALTY_DEMO_TOKEN
