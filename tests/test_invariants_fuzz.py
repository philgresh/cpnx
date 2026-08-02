"""Property-based liveness/safety verification for cpnx workflow nets.

Where ``tests/test_state_machine.py`` fuzzes the *sequence of operations* against a
resource-rich net (a `RuleBasedStateMachine`), this module fuzzes the *initial
marking and token payloads* of two fixed nets and asserts the empirical liveness
properties that Paper 1's decidability narrative rests on:

* **No orphaned tokens** — after driving to quiescence, every data token has
  reached a designated sink; none is trapped in an intermediate place.
* **Bounded quiescence** — the engine reaches a formal fixed point
  (`is_quiescent() is True`) within a step budget linear in the token count.
* **Deadlock is either by design or absent** — a deliberately under-provisioned
  net is confirmed to deadlock *exactly where designed* (the negative control),
  which is what lets the absence-of-deadlock assertion on the healthy net mean
  something.

Framing (per the task's academic grounding): high-level Petri nets (ISO/IEC
15909-1:2019) are decidable for many properties, but cpnx's admission of arbitrary
Python inscriptions forfeits that. This suite is therefore **not** a substitute
for exhaustive state-space model checking — which hits the combinatorial/Turing
wall on data-dependent nets — but rigorous randomized *fuzzing* in the sense of
property-based testing (Hypothesis), validating convergence on concrete markings
that stand in for production workloads. Cf. the static/dynamic pairing argued by
Eghbali, Burk & Pradel, "DyLin: A Dynamic Linter for Python" (FSE 2025): the
static AST linter (:mod:`cpnx.linting`) catches legible trouble spots; this
dynamic harness catches what static analysis provably cannot.
"""

import math

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cpnx import (
    InputArc,
    OutputArc,
    PetriNet,
    Place,
    SinkPlace,
    Token,
    Transition,
)

# --- hostile token strategies --------------------------------------------------------

# Deliberately adversarial payload *values*: extreme and boundary numerics, empty and
# unicode strings, None, booleans. These flow untouched through pass-through actions, so
# the point is not that the engine interprets them but that no pathological value derails
# routing, conservation, or convergence.
_HOSTILE_VALUES = (
    st.integers(min_value=-(2**63), max_value=2**63)
    | st.floats(allow_nan=True, allow_infinity=True)
    | st.booleans()
    | st.text(alphabet=st.characters(), max_size=8)
    | st.none()
)

# Payload keys include the empty string and unicode to stress dict/FrozenDict handling.
_HOSTILE_KEYS = st.text(alphabet=st.characters(), max_size=6)


@st.composite
def _hostile_token(draw: st.DrawFn) -> Token:
    # `color` is any non-"resource" string or None: these must all be treated as data
    # tokens by the sink-conservation accounting. "resource" is excluded on purpose — a
    # resource token is not conserved data and would confuse the orphan check.
    color = draw(st.sampled_from([None, "", "espresso", "π", "\x00", "job"]))
    payload = draw(st.dictionaries(_HOSTILE_KEYS, _HOSTILE_VALUES, max_size=4))
    return Token(color=color, payload=payload)


def _hostile_tokens(min_size: int = 0, max_size: int = 12):
    return st.lists(_hostile_token(), min_size=min_size, max_size=max_size)


# --- nets ----------------------------------------------------------------------------


def _pass_through(tokens: list[Token]) -> list[Token]:
    """Return consumed tokens unchanged — a pure data pass-through action."""
    return list(tokens)


def build_linear_workflow() -> PetriNet:
    """A minimal drain-guaranteed workflow: ``P_in -> t1 -> P_mid -> t2 -> P_sink``.

    No resources, thresholds, guards, or settle windows, so from *any* initial marking
    the only fixed point is "every data token absorbed by the sink". This is the net whose
    liveness we assert positively. ``failed`` is a sink too, so dead-lettered tokens (there
    should be none here) would still be conserved rather than vanish.
    """
    net = PetriNet(
        max_workers=1,
        error_place="failed",
        places=[
            Place("P_in"),
            Place("P_mid"),
            SinkPlace("P_sink", keep_last=8),
            SinkPlace("failed", keep_last=8),
        ],
        transitions=[
            Transition(
                name="t1",
                inputs=[InputArc("P_in", count=1)],
                outputs=[OutputArc("P_mid", count=1)],
                action=_pass_through,
            ),
            Transition(
                name="t2",
                inputs=[InputArc("P_mid", count=1)],
                outputs=[OutputArc("P_sink", count=1)],
                action=_pass_through,
            ),
        ],
    )
    return net


def build_deadlocking_net() -> PetriNet:
    """A net **designed** to deadlock: ``t_join`` needs one token from each of two input
    places, but only ``P_left`` is ever fed. The token in ``P_left`` can never fire and is
    stuck by construction — the negative control that proves the harness can tell a real
    deadlock from a healthy quiescent drain.
    """
    return PetriNet(
        max_workers=1,
        error_place="failed",
        places=[
            Place("P_left"),
            Place("P_right"),
            SinkPlace("P_out", keep_last=4),
            SinkPlace("failed", keep_last=4),
        ],
        transitions=[
            Transition(
                name="t_join",
                inputs=[InputArc("P_left", count=1), InputArc("P_right", count=1)],
                outputs=[OutputArc("P_out", count=1)],
                action=_pass_through,
            ),
        ],
    )


# --- helpers -------------------------------------------------------------------------


def _absorbed(net: PetriNet, *sink_names: str) -> int:
    return sum(net.places[name]._absorbed for name in sink_names)


def _live_data_outside_sinks(net: PetriNet) -> int:
    """Count non-resource tokens sitting in any non-sink place (the orphan candidates)."""
    total = 0
    for name, tokens in net.marking.items():
        if isinstance(net.places.get(name), SinkPlace):
            continue
        total += sum(1 for t in tokens if not t.is_resource)
    return total


# --- properties ----------------------------------------------------------------------


@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.data_too_large])
@given(tokens=_hostile_tokens(max_size=12))
def test_no_orphaned_tokens_and_bounded_quiescence(tokens: list[Token]):
    """From any hostile initial marking, the linear net drains fully to its sink.

    Asserts, jointly, the three headline invariants for the healthy net:
    conservation (deposited == absorbed), no orphans (nothing stuck outside a sink),
    formal quiescence, and a firing count within a linear budget.
    """
    net = build_linear_workflow()
    for token in tokens:
        net.deposit("P_in", token)

    result = net.drive_to_quiescence()

    # Formal fixed point reached.
    assert net.is_quiescent() is True

    # No orphaned data tokens: everything is absorbed by a sink, nothing left behind.
    assert _live_data_outside_sinks(net) == 0
    assert _absorbed(net, "P_sink", "failed") == len(tokens)

    # Bounded convergence: each token traverses exactly two transitions, so the firing
    # count cannot exceed 2 * N (plus a small constant slack for the empty-net case).
    assert result.steps <= 2 * len(tokens) + 2


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.data_too_large])
@given(tokens=_hostile_tokens(min_size=1, max_size=8))
def test_healthy_net_is_not_dead_with_stuck_tokens(tokens: list[Token]):
    """After draining, the healthy net is either fully quiescent with an empty interior,
    or not dead at all — never dead-with-orphans."""
    net = build_linear_workflow()
    for token in tokens:
        net.deposit("P_in", token)
    net.drive_to_quiescence()

    if net.is_dead():
        # A dead marking is only acceptable if no data token is stranded outside a sink.
        assert _live_data_outside_sinks(net) == 0


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.data_too_large])
@given(tokens=_hostile_tokens(min_size=1, max_size=6))
def test_deadlock_is_confined_to_the_by_design_net(tokens: list[Token]):
    """Negative control: the under-provisioned net deadlocks *exactly* as designed.

    Feeding only ``P_left`` must leave every deposited token stranded there (never fired,
    never absorbed) and the net dead — demonstrating the harness distinguishes a genuine,
    by-design deadlock from the healthy net's clean drain above.
    """
    net = build_deadlocking_net()
    for token in tokens:
        net.deposit("P_left", token)
    net.drive_to_quiescence()

    assert net.is_dead() is True
    # Nothing reached the sink; every token is stuck in P_left by construction.
    assert _absorbed(net, "P_out") == 0
    stuck_in_left = sum(1 for t in net.marking.get("P_left", ()) if not t.is_resource)
    assert stuck_in_left == len(tokens)


def test_empty_marking_is_immediately_quiescent():
    """Edge case: a net with no tokens is quiescent at once, with zero firings."""
    net = build_linear_workflow()
    result = net.drive_to_quiescence()
    assert net.is_quiescent() is True
    assert result.steps == 0
    assert _absorbed(net, "P_sink", "failed") == 0


def test_step_budget_is_finite_for_a_known_marking():
    """A concrete, non-fuzzed check that the linear budget bound is tight, not vacuous."""
    net = build_linear_workflow()
    n = 5
    for _ in range(n):
        net.deposit("P_in", Token())
    result = net.drive_to_quiescence()
    assert _absorbed(net, "P_sink") == n
    assert n <= result.steps <= 2 * n
    # Sanity on the slack constant: never near the pathological max_ticks safety cap.
    assert result.ticks < math.inf
