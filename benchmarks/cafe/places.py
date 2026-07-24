"""The ☕ Concurrency Cafe's core places — one documented factory per station.

Every place in the base topology gets its own factory here rather than being
constructed inline in `build_cafe`, so each one is an addressable symbol with a
docstring covering both halves of what it is: the **cafe role** (what a customer
or barista would see) and the **net feature** it exists to demonstrate.

Opt-in stations live in [`cafe.stations`][cafe.stations]; this module is only what you get with a
bare `build_cafe()`.
"""

from cpnx import (
    PacedResourcePlace,
    Place,
    ResourcePlace,
    SinkPlace,
    ThresholdPlace,
)


def p_ticket_line() -> Place:
    """**P_Ticket_Line** — the register queue every order enters through.

    Cafe role:
        The rail of paper tickets above the bar. The register never turns a customer
        away, it just adds another ticket, so this queue has no ceiling. It is also
        where a re-dosed ticket comes *back* to (`T_Rework_Dose` outputs here), which
        is why the line can grow from the middle of the net and not only from
        outside deposits.

    Demonstrates:
        The plain unbounded FIFO [`Place`][cpnx.Place] — no colour set, no bound, no initial
        marking. It is the net's **deep** place: the throughput benchmark stocks it
        with up to 20 000 tickets, which is what makes it the place where marking
        depth actually shows up in engine cost. It is drained by
        `T_Weigh_And_Grind` under [`BindingPolicy.PRIORITY`][cpnx.BindingPolicy], so it is also the one
        place whose depth feeds a full candidate enumeration rather than a head-only
        read.
    """
    return Place("P_Ticket_Line")


def p_digital_scales(capacity: int = 3) -> ResourcePlace:
    """**P_Digital_Scales** — the shared pool of bench scales.

    Cafe role:
        Three digital scales sit on the back bench. A barista grabs one to weigh the
        dose and puts it straight back; nobody holds one for longer than the weighing
        takes.

    Demonstrates:
        [`ResourcePlace`][cpnx.ResourcePlace] as a permit pool — pre-filled with `capacity` `"resource"`
        tokens at construction, and auto-returned by the engine once the consuming
        action completes, so `_weigh_and_grind` never has to hand the permit back
        explicitly. Because a permit arc contributes `C(capacity, count)`
        interchangeable options to the Cartesian product, this place is also one of
        the two multipliers that divide `T_Weigh_And_Grind`'s
        `binding_search_limit` budget (see [`t_weigh_and_grind`][cafe.transitions.t_weigh_and_grind]).

    Args:
        capacity: How many scales are on the bench. Defaults to 3.
    """
    return ResourcePlace("P_Digital_Scales", capacity=capacity)


def p_burr_grinder(grinders: int = 2, pacing_secs: float = 8.0) -> PacedResourcePlace:
    """**P_Burr_Grinder** — the grinders, each needing a breather between doses.

    Cafe role:
        Two burr grinders behind the counter (an espresso grinder and a decaf one).
        After dispensing, a grinder is unavailable for `pacing_secs` while the burrs
        spin down and the chute gets brushed out — a hard rate limit on how fast the
        bar can physically produce grounds.

    Demonstrates:
        [`PacedResourcePlace`][cpnx.PacedResourcePlace], i.e. a permit pool whose returned permits are
        future-dated by `pacing_secs` rather than being immediately re-usable. This
        is the net's source of genuine **back-pressure** and the reason the macro
        benchmarks drive a logical clock: the cooldown is real (the grinder truly is
        unavailable for 8 logical seconds) but waiting it out costs no wall time.

        Note this is a *shallow* timed place — capacity 2-3 — which is exactly what
        makes [`cafe.stations.cold_brew`][cafe.stations.cold_brew]'s deep timed place a distinct shape worth
        benchmarking separately.

    Args:
        grinders: Number of grinders, i.e. the permit capacity. Defaults to 2.
        pacing_secs: Cooldown applied to each returned permit. Defaults to 8.0.
    """
    return PacedResourcePlace("P_Burr_Grinder", capacity=grinders, pacing_secs=pacing_secs)


def p_ground_coffee() -> Place:
    """**P_Ground_Coffee** — dosed grounds waiting for a group head.

    Cafe role:
        A portafilter of ground coffee sitting on the bar, waiting for a free group
        on the espresso machine. Also where a channeled shot's grounds are *rolled
        back to* when `T_Pull_Shot` fails and the engine retries it.

    Demonstrates:
        A colour-restricted [`Place`][cpnx.Place] — `color_set={"ground_coffee"}` makes the place
        reject any token of the wrong colour, which turns a mis-wired output arc into
        an immediate error instead of a silently weird marking. Because it is the
        retry target, it is also the shallow queue that the channeling regime's extra
        `step()`s fire against (which is why retries make µs/*step* look cheaper
        while making the run strictly more expensive).
    """
    return Place("P_Ground_Coffee", color_set={"ground_coffee"})


def p_milk_queue() -> Place:
    """**P_Milk_Queue** — milk tickets waiting for a steam wand.

    Cafe role:
        The other half of an order. `T_Weigh_And_Grind` splits one ticket into two
        parallel work items, and this is the branch that becomes steamed milk while
        the grounds branch becomes a shot.

    Demonstrates:
        A second colour-restricted [`Place`][cpnx.Place] (`{"milk_ticket"}`), and — jointly with
        [`p_ground_coffee`][cafe.places.p_ground_coffee] — the net's **fork**: one transition writing two output
        arcs into two different places, so the two downstream stations become
        independently enabled and can genuinely run concurrently.
    """
    return Place("P_Milk_Queue", color_set={"milk_ticket"})


def p_espresso_machine(capacity: int = 2) -> ResourcePlace:
    """**P_Espresso_Machine** — group heads on the espresso machine.

    Cafe role:
        A two-group machine: two shots can pull at once instead of every pull
        serializing behind a single group.

    Demonstrates:
        [`ResourcePlace`][cpnx.ResourcePlace] used to *buy parallelism* rather than to model scarcity. This
        is the knob that decides how much of `max_workers` the shot station can
        actually use — with `capacity=1` the pool size is irrelevant downstream of
        the grinder, which is the failure mode the concurrency benchmark exists to
        detect.

    Args:
        capacity: Number of group heads. Defaults to 2.
    """
    return ResourcePlace("P_Espresso_Machine", capacity=capacity)


def p_steam_wand(capacity: int = 2) -> ResourcePlace:
    """**P_Steam_Wand** — steam wands for the milk line.

    Cafe role:
        Two wands on the machine, so two milks steam at once.

    Demonstrates:
        The milk line's mirror of [`p_espresso_machine`][cafe.places.p_espresso_machine] — the same
        [`ResourcePlace`][cpnx.ResourcePlace] shape on the parallel branch, so neither branch is structurally privileged
        and the fork's two arms have symmetric capacity.

    Args:
        capacity: Number of wands. Defaults to 2.
    """
    return ResourcePlace("P_Steam_Wand", capacity=capacity)


def p_order_tray(threshold: int = 2, bound: int | None = 6) -> ThresholdPlace:
    """**P_Order_Tray** — the hand-off counter where a drink is assembled.

    Cafe role:
        A drink isn't done until *both* its espresso shot and its steamed milk have
        landed on the tray. The counter also physically fits only so many cups —
        once it's full, the bar has to clear it before pulling more.

    Demonstrates:
        Two orthogonal CPN concepts on one place, which is precisely why they are
        set through two different mechanisms:

        - `ThresholdPlace(threshold=2)` — the **rendezvous**. The place refuses to
          be retrieved from at all until 2 tokens have accumulated, encoding "wait
          for both halves" directly on the place instead of in a guard.
        - `bound` — the **k-bound**, a plain settable attribute inherited from
          [`Place`][cpnx.Place] (the [`ThresholdPlace`][cpnx.ThresholdPlace] constructor deliberately does not expose
          it, since a threshold and a capacity are unrelated ideas). This is what gives
          `T_Pull_Shot`/`T_Steam_Milk` real output-capacity back-pressure.

        It is also the net's only `count=2` input arc and its only `settle_secs`
        arc — see [`t_serve_drink`][cafe.transitions.t_serve_drink].

    Args:
        threshold: Tokens that must accumulate before any retrieval is allowed.
        bound: Optional k-bound (cups the counter fits). `None` removes the bound.
    """
    tray = ThresholdPlace("P_Order_Tray", threshold=threshold)
    # ThresholdPlace's constructor doesn't expose `bound` (threshold and k-bound are
    # orthogonal CPN concepts), but `bound` is a plain, settable attribute inherited
    # from Place.
    tray.bound = bound
    return tray


def p_served() -> SinkPlace:
    """**P_Served** — the hatch where finished drinks leave the system.

    Cafe role:
        Drinks go out to customers and never come back. The shop counts them and
        forgets them.

    Demonstrates:
        [`SinkPlace`][cpnx.SinkPlace] as a terminal absorber — tokens deposited here are counted in
        `stats()["absorbed"]` but not retained (`keep_last=0`), so a 20 000-order run
        does not accumulate 20 000 live tokens in the marking. That is what keeps the
        deep throughput sweeps measuring the *drain*, rather than measuring memory
        growth at the far end of the pipeline.
    """
    return SinkPlace("P_Served")


def p_trash_can(keep_last: int = 10) -> SinkPlace:
    """**P_Trash_Can** — the knock-out bin for shots that couldn't be saved.

    Cafe role:
        A channeled shot gets one more attempt; if it channels again the barista
        bins it. The last few are kept on the bench for a quality check at close.

    Demonstrates:
        Two roles at once. As a [`SinkPlace`][cpnx.SinkPlace] with `keep_last=10` it is a **bounded
        retaining sink** — absorb-and-count like [`p_served`][cafe.places.p_served], but holding a rolling
        window for inspection. It is *also* the net's `error_place`, so the engine
        dead-letters a transition's data tokens here automatically once
        `max_retries` is exhausted, without any arc being drawn to it.

    Args:
        keep_last: Size of the retained rolling window. Defaults to 10.
    """
    return SinkPlace("P_Trash_Can", keep_last=keep_last)
