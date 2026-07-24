"""Arc and transition *inscriptions* for the core cafe — guards and binding keys.

In CPN terms this module holds the net's predicates and orderings, kept separate
from the [`actions`][cafe.actions] that do the work: a guard decides *whether* a transition may
fire, a binding key decides *which* tokens it fires with, and neither is allowed
to have side effects. The engine enforces that split — everything here is
purity-verified at construction, and evaluated while the engine lock is held.

Every callable here is a closure over immutable values (or over nothing at all),
which is what makes it **certify** for inline evaluation under
`cpnx.certification` instead of paying a thread round-trip per call. The opt-in
stations in [`cafe.stations`][cafe.stations] include deliberately *uncertified* counterparts so
the two dispatch paths can be measured against each other.
"""

from cafe.support import DOSE_TARGET_G
from cpnx import Token


def make_dose_guard(low: float, high: float):
    """Build **T_Weigh_And_Grind**'s guard: a barista won't grind an out-of-spec dose.

    Cafe role:
        Weighing the dose is the whole point of the scale step. A reading outside the
        shop's tolerance band (too little grounds under-extracts, too much
        over-extracts) doesn't get ground — it goes back for a re-dose instead, via
        `T_Rework_Dose`.

    Demonstrates:
        A **transition guard** (`Type[G(t)] = Bool`) evaluated once per *candidate
        binding*. Because the transition it gates runs [`BindingPolicy.PRIORITY`][cpnx.BindingPolicy] over
        a deep place, this is the single most-evaluated callable in the net — the
        profiler attributes the bulk of a guarded run to dispatching it. It is a
        callable closing over the immutable `low`/`high` floats, so it certifies and
        runs inline; the same predicate reading a mutable module global would not.
    """

    def _dose_in_spec(tokens: list[Token]) -> bool:
        order = next(t for t in tokens if not t.is_resource)
        return low <= order.payload.get("weight_g", DOSE_TARGET_G) <= high

    return _dose_in_spec


def make_rework_guard(low: float, high: float):
    """Build **T_Rework_Dose**'s guard: the exact complement of [`make_dose_guard`][cafe.inscriptions.make_dose_guard].

    Cafe role:
        A ticket is reworked precisely when its dose is *out* of the tolerance band —
        the two guards partition the ticket line between the grind station and the
        re-dose station with no overlap and no gap.

    Demonstrates:
        **Complementary guards as a routing mechanism.** Two transitions share one
        input place and are told apart purely by their predicates, so no ticket can
        take both paths and none can stall with neither enabled. Combined with
        [`make_rework_dose`][cafe.actions.make_rework_dose]'s clamping, it is also what makes the rework loop provably
        terminate rather than ping-pong.
    """

    def _dose_out_of_spec(tokens: list[Token]) -> bool:
        order = next(t for t in tokens if not t.is_resource)
        weight = order.payload.get("weight_g", DOSE_TARGET_G)
        return weight < low or weight > high

    return _dose_out_of_spec


def mobile_pickup_first(tokens: list[Token]) -> tuple[int, float]:
    """**T_Weigh_And_Grind**'s `binding_priority_key`: app orders jump the in-store line.

    Cafe role:
        A mobile-pickup ticket is already paid for and its customer is walking over,
        so the bar pulls it ahead of a walk-in. Among tickets of the same kind, the
        oldest goes first.

    Demonstrates:
        [`BindingPolicy.PRIORITY`][cpnx.BindingPolicy] plus a `binding_priority_key` — a **transition-level**
        tie-break that selects the minimum-key binding among the enumerated candidate
        set. Contrast [`cafe.stations.batch_triage`][cafe.stations.batch_triage]'s [`InputArc.key`][cpnx.InputArc],
        which is a different mechanism entirely: that reorders one arc's *token pool*, this
        chooses among whole *bindings* after enumeration.

        Note the key is invoked inline under the engine lock with **no timeout**,
        once per candidate — which is why it does nothing but read two payload fields.
    """
    order = next(t for t in tokens if t.color is None)
    return (0 if order.payload.get("mobile_pickup") else 1, order.created_at)
