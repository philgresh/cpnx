"""`build_cafe` — assemble the ☕ Concurrency Cafe topology from its documented parts.

This module is deliberately thin. Every place, transition, guard, key, and action
is defined and documented in [`cafe.places`][cafe.places], [`cafe.transitions`][cafe.transitions],
[`cafe.inscriptions`][cafe.inscriptions], [`cafe.actions`][cafe.actions], or a module under
[`cafe.stations`][cafe.stations]; all this function does is choose which of them to include and hand the result to
[`PetriNet`][cpnx.PetriNet]. If you are looking for *what a station is and why it exists*, read
its factory's docstring, not this file.
"""

from cafe import places as core_places
from cafe import transitions as core_transitions

# Aliased with a leading underscore because `build_cafe`'s station flags are named after
# the stations themselves — a bare `cold_brew` import would be shadowed by the `cold_brew`
# parameter inside the function body, and the failure would stay hidden for as long as the
# flag defaulted to off.
from cafe.stations import batch_triage as _batch_triage
from cafe.stations import cold_brew as _cold_brew
from cafe.stations import cupping as _cupping
from cafe.stations import decaf as _decaf
from cafe.stations import eighty_six as _eighty_six
from cafe.stations import knock_box as _knock_box
from cafe.stations import pastry_case as _pastry_case
from cafe.stations import specials_board as _specials_board
from cafe.support import dose_band
from cpnx import PetriNet


def build_cafe(
    *,
    pacing_secs: float = 8.0,
    channel_failure_rate: float = 0.15,
    channel_seed: int | None = None,
    max_workers: int = 4,
    dose_tolerance_g: float | None = 1.0,
    grinders: int = 2,
    work_secs: float = 0.0,
    tray_settle_secs: float = 0.05,
    tray_bound: int | None = 6,
    seed: int | None = None,
    binding_search_limit: int = 1000,
    resource_arcs_first: bool = False,
    cold_brew: bool = False,
    cold_brew_key: bool = False,
    batch_triage: bool = False,
    decaf: bool = False,
    knock_box: bool = False,
    knock_box_min_pucks: int = 25,
    specials_board: bool = False,
    eighty_six: bool = False,
    cupping: bool = False,
    cupping_count: int = 4,
    pastry_case: bool = False,
) -> PetriNet:
    """Wire up the Concurrency Cafe topology and return the (unstarted) [`PetriNet`][cpnx.PetriNet].

    Flow: `P_Ticket_Line` → (weigh & grind, gated by the dose guard, using a scale and
    a grinder) → `P_Ground_Coffee` / `P_Milk_Queue` in parallel → (pull shot, using a
    group head / steam milk, using a wand) → `P_Order_Tray` (waits for both a shot and
    a milk, and for the counter to settle) → (serve) → `P_Served`. A ticket whose
    declared dose misses the tolerance band is reworked (`T_Rework_Dose`) and returned
    to the back of `P_Ticket_Line` rather than ever reaching the grinder. Botched shots
    are dead-lettered to `P_Trash_Can`.

    This net is illustrative and **not conservation-checked**: transitions transform
    token colours and payloads rather than merely relocating fixed tokens, so
    per-colour counts are not expected to balance across a run. See the package
    docstring for the full caveat.

    Args:
        pacing_secs: Grinder cooldown window. The default 8.0 models a real spin-down;
            the throughput benchmark keeps it non-zero (real back-pressure) but drives
            the net on a logical clock so the wait costs no wall-clock time.
        channel_failure_rate: Probability that `T_Pull_Shot` channels and eventually
            dead-letters a shot. The default 0.15 exercises the retry path; `0.0` makes
            the run draw no RNG at all, so it reproduces at any worker count.
        channel_seed: Seed for a private channeling RNG. Only effective at
            `max_workers=1` — see [`cafe.actions.make_pull_shot`][cafe.actions.make_pull_shot].
        max_workers: Size of the engine's action thread pool.
        dose_tolerance_g: Half-width, in grams, of the acceptable dose band around the
            18 g target (default 1.0 → `[17, 19]`). This is the knob that drives
            per-candidate guard evaluation cost: a tighter band rejects more tickets, a
            wider one accepts nearly everything, and `None` removes the guard entirely
            (`T_Weigh_And_Grind.guard` unset and `T_Rework_Dose` omitted), reproducing
            the cheap guard-free binding-search path for A/B comparison.
        grinders: Number of burr grinders (default 2: espresso plus decaf). Raising this
            lifts the pipeline's dominant serializer — but see
            [`cafe.transitions.t_weigh_and_grind`][cafe.transitions.t_weigh_and_grind]'s warning, since it also halves
            the search budget available to the ticket dimension.
        work_secs: Wall-clock seconds each station's action sleeps before returning,
            modelling the physical time a barista spends there. Default `0.0` keeps
            actions instant; a nonzero value is what makes parallel speedup observable,
            since `time.sleep` releases the GIL.
        tray_settle_secs: Quiet period required on `P_Order_Tray` before serving.
        tray_bound: Optional k-bound on `P_Order_Tray` — how many cups the counter fits.
            Default 6 gives genuine but non-crippling back-pressure; `None` removes it.
        seed: Seeds the engine's transition-choice RNG. Every transition here shares the
            default `priority`, so each `step()` breaks the tie with `_rng.choice`;
            unseeded, the *step count* wanders run to run (~2%), which silently makes
            every µs/step figure a comparison between runs that did different amounts of
            work. **Benchmarks must pass a fixed seed**; `None` is for the demo, where a
            bit of variety is the point.
        binding_search_limit: Maximum input-token combinations tried per binding
            resolution, passed straight through to [`PetriNet`][cpnx.PetriNet]. Exposed here because it
            is the fixture's main untested tuning knob: it trades scan cost against how
            deep into `P_Ticket_Line` the PRIORITY search can still see a mobile-pickup
            ticket. See [`cafe.transitions.t_weigh_and_grind`][cafe.transitions.t_weigh_and_grind].
        resource_arcs_first: List `T_Weigh_And_Grind`'s permit arcs before its data arc,
            the ordering [`BindingPolicy`][cpnx.BindingPolicy]'s documentation recommends. Default `False`
            keeps the historical order so existing numbers stay comparable.
        cold_brew: Add the 🧊 cold-brew tower — a deep **timed** place. See
            [`cafe.stations.cold_brew`][cafe.stations.cold_brew].
        cold_brew_key: Also attach a certified [`InputArc.key`][cpnx.InputArc] to the tower's arc,
            reproducing the timed×key residual ([#25]). Requires `cold_brew=True`.
        batch_triage: Add the 📋 rush-hour triage queue — a deep place drained through a
            certified [`InputArc.key`][cpnx.InputArc]. See [`cafe.stations.batch_triage`][cafe.stations.batch_triage].
        decaf: Add the ☕ decaf-only barista — an [`InputArc.filter`][cpnx.InputArc] with no `key`, which
            never gets a key index however well it certifies. See [`cafe.stations.decaf`][cafe.stations.decaf].
        knock_box: Add the 🥁 knock box — a `consume_all` arc behind a mostly-false guard,
            re-scanned in full on every `step()`. See [`cafe.stations.knock_box`][cafe.stations.knock_box].
        knock_box_min_pucks: How full the bin must be before the barista empties it — the
            lull-frequency knob. Only meaningful with `knock_box=True`.
        specials_board: Add the 🧾 specials board — an **uncertified** [`InputArc.key`][cpnx.InputArc]
            computing the same ordering as `batch_triage`, so the two are an A/B pair for
            what certification is worth. See [`cafe.stations.specials_board`][cafe.stations.specials_board].
        eighty_six: Add the 🚫 86 board — a certified `key` behind an *uncertified*
            `filter`, which disqualifies the whole arc from indexing. See
            [`cafe.stations.eighty_six`][cafe.stations.eighty_six].
        cupping: Add the 🥄 cupping table — a `count > 1` keyed arc under a guard, which
            stresses the candidate space rather than the token pool. See
            [`cafe.stations.cupping`][cafe.stations.cupping].
        cupping_count: Cups per flight. Only meaningful with `cupping=True`.
        pastry_case: Add the 🥐 pastry case — the fixture's only
            [`SubstitutionTransition`][cpnx.SubstitutionTransition], driving a nested
            kitchen subnet to quiescence per firing. See
            [`cafe.stations.pastry_case`][cafe.stations.pastry_case].

    Raises:
        ValueError: If `cold_brew_key` is set without `cold_brew`.
    """
    if cold_brew_key and not cold_brew:
        raise ValueError("cold_brew_key=True requires cold_brew=True — there is no tower to key.")

    dose_low, dose_high = dose_band(dose_tolerance_g)

    net_places = [
        core_places.p_ticket_line(),
        core_places.p_digital_scales(),
        core_places.p_burr_grinder(grinders=grinders, pacing_secs=pacing_secs),
        core_places.p_ground_coffee(),
        core_places.p_milk_queue(),
        core_places.p_espresso_machine(),
        core_places.p_steam_wand(),
        core_places.p_order_tray(bound=tray_bound),
        core_places.p_served(),
        core_places.p_trash_can(),
    ]

    net_transitions = [
        core_transitions.t_weigh_and_grind(
            work_secs=work_secs,
            dose_low=dose_low,
            dose_high=dose_high,
            resource_arcs_first=resource_arcs_first,
        ),
        core_transitions.t_pull_shot(
            work_secs=work_secs,
            channel_failure_rate=channel_failure_rate,
            channel_seed=channel_seed,
        ),
        core_transitions.t_steam_milk(work_secs=work_secs),
        core_transitions.t_serve_drink(work_secs=work_secs, tray_settle_secs=tray_settle_secs),
    ]

    if dose_tolerance_g is not None:
        net_transitions.append(
            core_transitions.t_rework_dose(work_secs=work_secs, dose_low=dose_low, dose_high=dose_high)
        )

    if cold_brew:
        net_places += _cold_brew.places()
        net_transitions += _cold_brew.transitions(work_secs=work_secs, key=cold_brew_key)

    if batch_triage:
        net_places += _batch_triage.places()
        net_transitions += _batch_triage.transitions(work_secs=work_secs)

    if decaf:
        net_places += _decaf.places()
        net_transitions += _decaf.transitions(work_secs=work_secs)

    if knock_box:
        net_places += _knock_box.places()
        net_transitions += _knock_box.transitions(work_secs=work_secs, min_pucks=knock_box_min_pucks)

    if specials_board:
        net_places += _specials_board.places()
        net_transitions += _specials_board.transitions(work_secs=work_secs)

    if eighty_six:
        net_places += _eighty_six.places()
        net_transitions += _eighty_six.transitions(work_secs=work_secs)

    if cupping:
        net_places += _cupping.places()
        net_transitions += _cupping.transitions(work_secs=work_secs, count=cupping_count)

    if pastry_case:
        net_places += _pastry_case.places()
        net_transitions += _pastry_case.transitions(work_secs=work_secs)

    return PetriNet(
        max_workers=max_workers,
        error_place="P_Trash_Can",
        places=net_places,
        transitions=net_transitions,
        # Fast rollback so a channeled shot's grounds are eligible for a retry quickly
        # instead of the 1s default — keeps this demo snappy.
        retry_delay=0.2,
        binding_search_limit=binding_search_limit,
        seed=seed,
    )
