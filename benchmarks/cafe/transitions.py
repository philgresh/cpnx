"""The ☕ Concurrency Cafe's core transitions — one documented factory per station.

Each factory returns a fully-wired [`Transition`][cpnx.Transition] and documents both halves of what
it is: the **cafe role** (what the barista is doing) and the **net features** it
exercises — binding policy, guard, arc shape, timeouts, retries.

Opt-in stations live in [`cafe.stations`][cafe.stations]; this module is only what you get with a
bare `build_cafe()`.
"""

from cafe import actions, inscriptions
from cafe.support import with_work
from cpnx import BindingPolicy, InputArc, OutputArc, Transition


def t_weigh_and_grind(
    *,
    work_secs: float = 0.0,
    dose_low: float | None = None,
    dose_high: float | None = None,
    resource_arcs_first: bool = False,
) -> Transition:
    """**T_Weigh_And_Grind** — weigh the dose, grind it, split the order in two.

    Cafe role:
        The first real station. A barista takes the next ticket off the rail, grabs a
        free scale and a free grinder, weighs the dose, and — if it's in spec —
        grinds it. Mobile-pickup tickets get pulled ahead of walk-ins. An out-of-spec
        dose never reaches the grinder; `T_Rework_Dose` takes it instead.

    Demonstrates:
        The net's densest transition, and the one every guarded benchmark number
        comes from:

        - **[`BindingPolicy.PRIORITY`][cpnx.BindingPolicy] over a deep place.** It enumerates the whole
          (bounded) candidate set rather than short-circuiting, so it is where
          per-candidate guard dispatch actually costs something.
        - **A guard on the enumerated path** — [`make_dose_guard`][cafe.inscriptions.make_dose_guard], evaluated once
        per candidate binding. Passing `dose_low=None` omits it entirely, which is the
          fixture's guard-free A/B arm.
        - **`binding_priority_key`** ([`mobile_pickup_first`][cafe.inscriptions.mobile_pickup_first]) — a
        transition-level tie-break, distinct from an arc-level `key`.
        - **Two permit arcs plus a data arc**, i.e. a three-dimensional Cartesian
          product — see `resource_arcs_first`.
        - **`action_timeout_secs`** — weighing is a quick bounded action, so it gets
          a short deadline.

    Warning:
        `binding_search_limit` (default 1000) is spent against raw Cartesian
        *product* tuples, so the permit arcs divide the usable ticket depth:
        `effective_depth ≈ limit / (scales × grinders)`. On the default topology
        mobile-pickup preference holds to depth ~166 and is silently gone by ~170
        (1000 / (3 × 2)); with `grinders=1` it held to ~333. Past that the scan still
        runs and still costs — it just stops finding the token it is looking for and
        falls back to insertion order, with no error and no warning. That is
        [#18](https://github.com/philgresh/cpnx/issues/18): the bug is budget
        *accounting*, not the limit itself. Raising `grinders` to buy parallelism
        makes it bite twice as early.

    Args:
        work_secs: Physical seconds the station occupies a worker.
        dose_low: Lower bound of the acceptable dose band. `None` omits the guard.
        dose_high: Upper bound of the acceptable dose band. `None` omits the guard.
        resource_arcs_first: Which order the input arcs are listed in — the fixture's
            handle on the arc-ordering tuning lever documented on [`BindingPolicy`][cpnx.BindingPolicy].

            `itertools.product` varies the **last** arc fastest, so listing the deep
            data arc last makes the ticket dimension the one that changes first, and
            the search's first `limit` candidates then sweep `limit` distinct tickets
            instead of `limit / (scales × grinders)` of them. `False` (the default)
            keeps the historical data-arc-first order so existing numbers stay
            comparable; `True` is the documented-recommended order and should raise
            effective ticket depth by the permit-arc product without changing
            semantics.
    """
    data_arc = InputArc("P_Ticket_Line")
    permit_arcs = [InputArc("P_Digital_Scales"), InputArc("P_Burr_Grinder")]
    inputs = [*permit_arcs, data_arc] if resource_arcs_first else [data_arc, *permit_arcs]

    guard = None
    if dose_low is not None and dose_high is not None:
        guard = inscriptions.make_dose_guard(dose_low, dose_high)

    return Transition(
        name="T_Weigh_And_Grind",
        inputs=inputs,
        outputs=[OutputArc("P_Ground_Coffee"), OutputArc("P_Milk_Queue")],
        action=with_work(work_secs, actions.weigh_and_grind),
        action_timeout_secs=1.0,
        guard=guard,
        binding_policy=BindingPolicy.PRIORITY,
        binding_priority_key=inscriptions.mobile_pickup_first,
    )


def t_pull_shot(
    *,
    work_secs: float = 0.0,
    channel_failure_rate: float = 0.15,
    channel_seed: int | None = None,
) -> Transition:
    """**T_Pull_Shot** — lock in the portafilter and pull an espresso.

    Cafe role:
        Grounds plus a free group head become a shot on the tray. Sometimes the puck
        channels and the shot is ruined; the barista re-doses and tries once more
        before binning it.

    Demonstrates:
        The **failure path**, which is the only place in the net where a transition
        raises:

        - **Atomic rollback** — a raising action returns the grounds token to
          `P_Ground_Coffee` *and* the group-head permit to its pool, together.
        - **`max_retries=1`** — one retry, then the engine dead-letters the data
          token to the net's `error_place`. No arc is drawn to `P_Trash_Can`; the
          engine routes it.
        - **`retry_delay` on the model clock** — a rolled-back token comes back
          future-dated, which is what makes the retry regime measurable on a logical
          clock at all.

    Args:
        work_secs: Physical seconds the station occupies a worker.
        channel_failure_rate: Probability a pull channels. `0.0` removes all RNG.
        channel_seed: Seed for a private RNG; only effective at `max_workers=1`.
    """
    return Transition(
        name="T_Pull_Shot",
        inputs=[InputArc("P_Ground_Coffee"), InputArc("P_Espresso_Machine")],
        outputs=[OutputArc("P_Order_Tray")],
        action=with_work(work_secs, actions.make_pull_shot(channel_failure_rate, channel_seed)),
        action_timeout_secs=0.5,
        max_retries=1,
    )


def t_steam_milk(*, work_secs: float = 0.0) -> Transition:
    """**T_Steam_Milk** — steam the milk and send it to the tray.

    Cafe role:
        The parallel arm of the order. A free wand plus a milk ticket becomes steamed
        oat or dairy milk, which joins the shot on the tray.

    Demonstrates:
        The net's only **[`OutputArc.condition`][cpnx.OutputArc]** usage, via the
        `OutputArc.on_color(...)` constructor. Two arcs point at the *same* place and
        are distinguished purely by their activation predicates, so the oat/dairy
        branch stays legible in the event log. `on_color` closes over an immutable
        string, so both conditions certify and run inline.

    Args:
        work_secs: Physical seconds the station occupies a worker.
    """
    return Transition(
        name="T_Steam_Milk",
        inputs=[InputArc("P_Milk_Queue"), InputArc("P_Steam_Wand")],
        outputs=[
            OutputArc.on_color("oat_milk", "P_Order_Tray"),
            OutputArc.on_color("dairy_milk", "P_Order_Tray"),
        ],
        action=with_work(work_secs, actions.steam_milk),
        action_timeout_secs=0.5,
    )


def t_serve_drink(*, work_secs: float = 0.0, tray_settle_secs: float = 0.05) -> Transition:
    """**T_Serve_Drink** — bus the tray and call the drink.

    Cafe role:
        Once a shot and a milk are both on the counter, the barista waits a beat to
        see whether the rest of the order lands, then assembles and serves.

    Demonstrates:
        The **rendezvous join**, and the net's only use of two arc features:

        - **`count=2`** — one firing drains exactly one drink's worth. Combined with
          `P_Order_Tray`'s `threshold=2`, the transition simply cannot become enabled
          on a half-order.
        - **`settle_secs`** — a quiet-period requirement on the *place*, not a delay
          on the token: the arc refuses to fire until no new token has arrived for
          this long. It is the only arc exercising that branch of the engine's
          availability check (and of `benchmarks/_driver.py`'s clock advance).

    Args:
        work_secs: Physical seconds the station occupies a worker.
        tray_settle_secs: Quiet period required on the tray before serving.
    """
    return Transition(
        name="T_Serve_Drink",
        inputs=[InputArc("P_Order_Tray", count=2, settle_secs=tray_settle_secs)],
        outputs=[OutputArc("P_Served")],
        action=with_work(work_secs, actions.serve_drink),
        action_timeout_secs=0.5,
    )


def t_rework_dose(*, work_secs: float = 0.0, dose_low: float, dose_high: float) -> Transition:
    """**T_Rework_Dose** — re-dose a ticket whose weight missed spec.

    Cafe role:
        The scale read out of band. Rather than grinding a bad dose, the barista
        adjusts and puts the ticket back on the rail.

    Demonstrates:
        A **self-loop** — the transition's input and output are the same place, so a
        reworked ticket re-enters the line and is re-evaluated by both guards. Two
        things keep that from being a livelock: the action clamps into the band, and
        this guard is the exact complement of the grind guard.

        Also the fixture's one deliberate use of the default
        **[`BindingPolicy.LEGACY`][cpnx.BindingPolicy]** on a deep place: it only ever inspects the head of
        `P_Ticket_Line`. A ticket not yet at the head is simply skipped over by
        `T_Weigh_And_Grind`'s PRIORITY search, so it reaches the head eventually
        without this transition needing to enumerate for it — a worked example of
        choosing the cheap policy where completeness isn't needed.

    Args:
        work_secs: Physical seconds the station occupies a worker.
        dose_low: Lower bound of the acceptable dose band.
        dose_high: Upper bound of the acceptable dose band.
    """
    return Transition(
        name="T_Rework_Dose",
        inputs=[InputArc("P_Ticket_Line")],
        outputs=[OutputArc("P_Ticket_Line")],
        action=with_work(work_secs, actions.make_rework_dose(dose_low, dose_high)),
        action_timeout_secs=0.5,
        guard=inscriptions.make_rework_guard(dose_low, dose_high),
    )
