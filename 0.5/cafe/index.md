# ☕ The Concurrency Cafe

A single-bar specialty coffee shop during the morning rush, modelled as a Coloured Petri Net. Tickets pile up at the register, baristas share a small pool of scales, the grinders need a breather after every dose, a barista won't grind an out-of-spec dose, and a drink isn't done until both the shot and the milk land on the same tray.

It is a demo you can read end to end — but it is also `cpnx`'s **benchmark fixture**, and that gives every station a second job. Each one is the only place in the corpus where some particular engine cost path gets exercised, so each factory below documents two things: the **cafe role** (what a barista would see) and the **net feature** it demonstrates.

Not a conservation-checked net

The cafe's transitions *transform* tokens — an order becomes grounds, then espresso, then part of a drink — rather than merely moving fixed colours between places. That is deliberate and idiomatic for `cpnx`, but it means token counts are not invariant across a run the way a strict place/transition conservation model's would be. Read the output as "a cafe served some drinks and binned some botched shots", not as an audited ledger.

Run it:

```
python benchmarks/concurrency_cafe.py
```

## The tour

## cafe

☕ The Concurrency Cafe — a whimsical, illustrative `cpnx` reference topology.

Picture a single-bar specialty coffee shop during the morning rush. Tickets pile up at the register, baristas share a small pool of digital scales, the grinders need a breather after every dose, a barista won't grind a ticket whose declared dose misses spec (it goes back for a re-dose instead), and a finished drink is only "done" once *both* the espresso shot and the steamed milk have landed on the same tray. That whole scene maps almost one-to-one onto `cpnx`'s vocabulary of places, resources, thresholds, guards, and sinks — which is why it makes a good end-to-end tour of the library.

It is also the fixture every benchmark in `benchmarks/` runs against, so each station carries a second job: to be the *only* place in the corpus where some particular engine cost path is exercised. Each factory's docstring names both — the cafe role and the net feature.

#### Layout

| Module            | Holds                                                       |
| ----------------- | ----------------------------------------------------------- |
| cafe.places       | The core places, one documented factory each                |
| cafe.transitions  | The core transitions, one documented factory each           |
| cafe.inscriptions | Guards and binding keys — pure, run under the engine lock   |
| cafe.actions      | Transition actions — side effects allowed, run off the lock |
| cafe.stations     | Opt-in stations, one self-contained module per station      |
| cafe.net          | `build_cafe`, which just chooses among the above            |
| cafe.support      | Shared constants and the `work_secs` wrapper                |

Warning

This is an **illustrative benchmark/demo, not a conservation-checked CPN**. Its transitions *transform* tokens (an order token is consumed and becomes a ground-coffee token, then an espresso token, then part of a drink token) rather than merely moving fixed colours between places. That is deliberate and idiomatic for `cpnx`, but it means you should not expect the total token count, or any single colour's count, to be invariant across a run the way it would be in a strict place/transition conservation model. Treat what this prints as "a cafe served some drinks and binned some botched shots", not as an audited ledger.

#### Token colours in play

- `None` (order tickets) — an uncoloured data token carrying the customer's order as its `payload`: `ratio`, `weight_g`, `dairy_free`, `mobile_pickup`.
- `"resource"` — permit tokens pre-filled into ResourcePlace and PacedResourcePlace instances (scales, grinders, group heads, wands). The engine returns these automatically once consumed; action code never hands them back.
- `"ground_coffee"` / `"milk_ticket"` — intermediate work-in-progress tokens produced by the grind step, one feeding the espresso line and one the milk line.
- `"espresso"` / `"oat_milk"` / `"dairy_milk"` — finished component tokens that accumulate on the order tray.
- `"cold_brew"` — a batch steeping in the opt-in cold-brew tower.
- `"drink"` — the final assembled beverage, deposited into the `P_Served` sink.

#### Base topology (always present)

| Place                | cpnx type          | Cafe role                                      |
| -------------------- | ------------------ | ---------------------------------------------- |
| `P_Ticket_Line`      | Place              | Unbounded FIFO of incoming order tickets       |
| `P_Digital_Scales`   | ResourcePlace      | Shared pool of 3 scales                        |
| `P_Burr_Grinder`     | PacedResourcePlace | Grinders, each with a cooldown                 |
| `P_Ground_Coffee`    | Place              | Grounds awaiting a shot                        |
| `P_Milk_Queue`       | Place              | Milk tickets awaiting steaming                 |
| `P_Espresso_Machine` | ResourcePlace      | Two group heads                                |
| `P_Steam_Wand`       | ResourcePlace      | Two steam wands                                |
| `P_Order_Tray`       | ThresholdPlace     | Shot + milk rendezvous; counter fits 6 cups    |
| `P_Served`           | SinkPlace          | Terminal place for completed drinks            |
| `P_Trash_Can`        | SinkPlace          | Dead-letter bin (also the net's `error_place`) |

#### Opt-in stations

All default to off, and all are structure-preserving when off — `build_cafe()` with no flags is exactly the table above. See cafe.stations for the module contract.

| Flag            | Station             | Exercises                         |
| --------------- | ------------------- | --------------------------------- |
| `cold_brew`     | 🧊 Cold-brew tower  | A deep **timed** place            |
| `cold_brew_key` | ↳ with a keyed arc  | The timed×key residual (#25)      |
| `batch_triage`  | 📋 Rush-hour triage | A certified InputArc.key at depth |

Run it directly:

```
python benchmarks/concurrency_cafe.py
```

### build_cafe

```
build_cafe(
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
) -> PetriNet
```

Wire up the Concurrency Cafe topology and return the (unstarted) PetriNet.

Flow: `P_Ticket_Line` → (weigh & grind, gated by the dose guard, using a scale and a grinder) → `P_Ground_Coffee` / `P_Milk_Queue` in parallel → (pull shot, using a group head / steam milk, using a wand) → `P_Order_Tray` (waits for both a shot and a milk, and for the counter to settle) → (serve) → `P_Served`. A ticket whose declared dose misses the tolerance band is reworked (`T_Rework_Dose`) and returned to the back of `P_Ticket_Line` rather than ever reaching the grinder. Botched shots are dead-lettered to `P_Trash_Can`.

This net is illustrative and **not conservation-checked**: transitions transform token colours and payloads rather than merely relocating fixed tokens, so per-colour counts are not expected to balance across a run. See the package docstring for the full caveat.

Parameters:

| Name                   | Type    | Description                                                                                                                                                                                                                                                                                                                       | Default                                                                                                                                                                                                                                                                                                                                                                                                                  |
| ---------------------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `pacing_secs`          | `float` | Grinder cooldown window. The default 8.0 models a real spin-down; the throughput benchmark keeps it non-zero (real back-pressure) but drives the net on a logical clock so the wait costs no wall-clock time.                                                                                                                     | `8.0`                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `channel_failure_rate` | `float` | Probability that T_Pull_Shot channels and eventually dead-letters a shot. The default 0.15 exercises the retry path; 0.0 makes the run draw no RNG at all, so it reproduces at any worker count.                                                                                                                                  | `0.15`                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `channel_seed`         | \`int   | None\`                                                                                                                                                                                                                                                                                                                            | Seed for a private channeling RNG. Only effective at max_workers=1 — see cafe.actions.make_pull_shot.                                                                                                                                                                                                                                                                                                                    |
| `max_workers`          | `int`   | Size of the engine's action thread pool.                                                                                                                                                                                                                                                                                          | `4`                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `dose_tolerance_g`     | \`float | None\`                                                                                                                                                                                                                                                                                                                            | Half-width, in grams, of the acceptable dose band around the 18 g target (default 1.0 → [17, 19]). This is the knob that drives per-candidate guard evaluation cost: a tighter band rejects more tickets, a wider one accepts nearly everything, and None removes the guard entirely (T_Weigh_And_Grind.guard unset and T_Rework_Dose omitted), reproducing the cheap guard-free binding-search path for A/B comparison. |
| `grinders`             | `int`   | Number of burr grinders (default 2: espresso plus decaf). Raising this lifts the pipeline's dominant serializer — but see cafe.transitions.t_weigh_and_grind's warning, since it also halves the search budget available to the ticket dimension.                                                                                 | `2`                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `work_secs`            | `float` | Wall-clock seconds each station's action sleeps before returning, modelling the physical time a barista spends there. Default 0.0 keeps actions instant; a nonzero value is what makes parallel speedup observable, since time.sleep releases the GIL.                                                                            | `0.0`                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `tray_settle_secs`     | `float` | Quiet period required on P_Order_Tray before serving.                                                                                                                                                                                                                                                                             | `0.05`                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `tray_bound`           | \`int   | None\`                                                                                                                                                                                                                                                                                                                            | Optional k-bound on P_Order_Tray — how many cups the counter fits. Default 6 gives genuine but non-crippling back-pressure; None removes it.                                                                                                                                                                                                                                                                             |
| `seed`                 | \`int   | None\`                                                                                                                                                                                                                                                                                                                            | Seeds the engine's transition-choice RNG. Every transition here shares the default priority, so each step() breaks the tie with \_rng.choice; unseeded, the step count wanders run to run (~2%), which silently makes every µs/step figure a comparison between runs that did different amounts of work. Benchmarks must pass a fixed seed; None is for the demo, where a bit of variety is the point.                   |
| `binding_search_limit` | `int`   | Maximum input-token combinations tried per binding resolution, passed straight through to PetriNet. Exposed here because it is the fixture's main untested tuning knob: it trades scan cost against how deep into P_Ticket_Line the PRIORITY search can still see a mobile-pickup ticket. See cafe.transitions.t_weigh_and_grind. | `1000`                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `resource_arcs_first`  | `bool`  | List T_Weigh_And_Grind's permit arcs before its data arc, the ordering BindingPolicy's documentation recommends. Default False keeps the historical order so existing numbers stay comparable.                                                                                                                                    | `False`                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `cold_brew`            | `bool`  | Add the 🧊 cold-brew tower — a deep timed place. See cafe.stations.cold_brew.                                                                                                                                                                                                                                                     | `False`                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `cold_brew_key`        | `bool`  | Also attach a certified InputArc.key to the tower's arc, reproducing the timed×key residual ([#25]). Requires cold_brew=True.                                                                                                                                                                                                     | `False`                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `batch_triage`         | `bool`  | Add the 📋 rush-hour triage queue — a deep place drained through a certified InputArc.key. See cafe.stations.batch_triage.                                                                                                                                                                                                        | `False`                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `decaf`                | `bool`  | Add the ☕ decaf-only barista — an InputArc.filter with no key, which never gets a key index however well it certifies. See cafe.stations.decaf.                                                                                                                                                                                  | `False`                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `knock_box`            | `bool`  | Add the 🥁 knock box — a consume_all arc behind a mostly-false guard, re-scanned in full on every step(). See cafe.stations.knock_box.                                                                                                                                                                                            | `False`                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `knock_box_min_pucks`  | `int`   | How full the bin must be before the barista empties it — the lull-frequency knob. Only meaningful with knock_box=True.                                                                                                                                                                                                            | `25`                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `specials_board`       | `bool`  | Add the 🧾 specials board — an uncertified InputArc.key computing the same ordering as batch_triage, so the two are an A/B pair for what certification is worth. See cafe.stations.specials_board.                                                                                                                                | `False`                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `eighty_six`           | `bool`  | Add the 🚫 86 board — a certified key behind an uncertified filter, which disqualifies the whole arc from indexing. See cafe.stations.eighty_six.                                                                                                                                                                                 | `False`                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `cupping`              | `bool`  | Add the 🥄 cupping table — a count > 1 keyed arc under a guard, which stresses the candidate space rather than the token pool. See cafe.stations.cupping.                                                                                                                                                                         | `False`                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `cupping_count`        | `int`   | Cups per flight. Only meaningful with cupping=True.                                                                                                                                                                                                                                                                               | `4`                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `pastry_case`          | `bool`  | Add the 🥐 pastry case — the fixture's only SubstitutionTransition, driving a nested kitchen subnet to quiescence per firing. See cafe.stations.pastry_case.                                                                                                                                                                      | `False`                                                                                                                                                                                                                                                                                                                                                                                                                  |

Raises:

| Type         | Description                                |
| ------------ | ------------------------------------------ |
| `ValueError` | If cold_brew_key is set without cold_brew. |

Source code in `benchmarks/cafe/net.py`

```
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
```

## Building a net

## cafe.net

`build_cafe` — assemble the ☕ Concurrency Cafe topology from its documented parts.

This module is deliberately thin. Every place, transition, guard, key, and action is defined and documented in cafe.places, cafe.transitions, cafe.inscriptions, cafe.actions, or a module under cafe.stations; all this function does is choose which of them to include and hand the result to PetriNet. If you are looking for *what a station is and why it exists*, read its factory's docstring, not this file.

### build_cafe

```
build_cafe(
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
) -> PetriNet
```

Wire up the Concurrency Cafe topology and return the (unstarted) PetriNet.

Flow: `P_Ticket_Line` → (weigh & grind, gated by the dose guard, using a scale and a grinder) → `P_Ground_Coffee` / `P_Milk_Queue` in parallel → (pull shot, using a group head / steam milk, using a wand) → `P_Order_Tray` (waits for both a shot and a milk, and for the counter to settle) → (serve) → `P_Served`. A ticket whose declared dose misses the tolerance band is reworked (`T_Rework_Dose`) and returned to the back of `P_Ticket_Line` rather than ever reaching the grinder. Botched shots are dead-lettered to `P_Trash_Can`.

This net is illustrative and **not conservation-checked**: transitions transform token colours and payloads rather than merely relocating fixed tokens, so per-colour counts are not expected to balance across a run. See the package docstring for the full caveat.

Parameters:

| Name                   | Type    | Description                                                                                                                                                                                                                                                                                                                       | Default                                                                                                                                                                                                                                                                                                                                                                                                                  |
| ---------------------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `pacing_secs`          | `float` | Grinder cooldown window. The default 8.0 models a real spin-down; the throughput benchmark keeps it non-zero (real back-pressure) but drives the net on a logical clock so the wait costs no wall-clock time.                                                                                                                     | `8.0`                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `channel_failure_rate` | `float` | Probability that T_Pull_Shot channels and eventually dead-letters a shot. The default 0.15 exercises the retry path; 0.0 makes the run draw no RNG at all, so it reproduces at any worker count.                                                                                                                                  | `0.15`                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `channel_seed`         | \`int   | None\`                                                                                                                                                                                                                                                                                                                            | Seed for a private channeling RNG. Only effective at max_workers=1 — see cafe.actions.make_pull_shot.                                                                                                                                                                                                                                                                                                                    |
| `max_workers`          | `int`   | Size of the engine's action thread pool.                                                                                                                                                                                                                                                                                          | `4`                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `dose_tolerance_g`     | \`float | None\`                                                                                                                                                                                                                                                                                                                            | Half-width, in grams, of the acceptable dose band around the 18 g target (default 1.0 → [17, 19]). This is the knob that drives per-candidate guard evaluation cost: a tighter band rejects more tickets, a wider one accepts nearly everything, and None removes the guard entirely (T_Weigh_And_Grind.guard unset and T_Rework_Dose omitted), reproducing the cheap guard-free binding-search path for A/B comparison. |
| `grinders`             | `int`   | Number of burr grinders (default 2: espresso plus decaf). Raising this lifts the pipeline's dominant serializer — but see cafe.transitions.t_weigh_and_grind's warning, since it also halves the search budget available to the ticket dimension.                                                                                 | `2`                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `work_secs`            | `float` | Wall-clock seconds each station's action sleeps before returning, modelling the physical time a barista spends there. Default 0.0 keeps actions instant; a nonzero value is what makes parallel speedup observable, since time.sleep releases the GIL.                                                                            | `0.0`                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `tray_settle_secs`     | `float` | Quiet period required on P_Order_Tray before serving.                                                                                                                                                                                                                                                                             | `0.05`                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `tray_bound`           | \`int   | None\`                                                                                                                                                                                                                                                                                                                            | Optional k-bound on P_Order_Tray — how many cups the counter fits. Default 6 gives genuine but non-crippling back-pressure; None removes it.                                                                                                                                                                                                                                                                             |
| `seed`                 | \`int   | None\`                                                                                                                                                                                                                                                                                                                            | Seeds the engine's transition-choice RNG. Every transition here shares the default priority, so each step() breaks the tie with \_rng.choice; unseeded, the step count wanders run to run (~2%), which silently makes every µs/step figure a comparison between runs that did different amounts of work. Benchmarks must pass a fixed seed; None is for the demo, where a bit of variety is the point.                   |
| `binding_search_limit` | `int`   | Maximum input-token combinations tried per binding resolution, passed straight through to PetriNet. Exposed here because it is the fixture's main untested tuning knob: it trades scan cost against how deep into P_Ticket_Line the PRIORITY search can still see a mobile-pickup ticket. See cafe.transitions.t_weigh_and_grind. | `1000`                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `resource_arcs_first`  | `bool`  | List T_Weigh_And_Grind's permit arcs before its data arc, the ordering BindingPolicy's documentation recommends. Default False keeps the historical order so existing numbers stay comparable.                                                                                                                                    | `False`                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `cold_brew`            | `bool`  | Add the 🧊 cold-brew tower — a deep timed place. See cafe.stations.cold_brew.                                                                                                                                                                                                                                                     | `False`                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `cold_brew_key`        | `bool`  | Also attach a certified InputArc.key to the tower's arc, reproducing the timed×key residual ([#25]). Requires cold_brew=True.                                                                                                                                                                                                     | `False`                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `batch_triage`         | `bool`  | Add the 📋 rush-hour triage queue — a deep place drained through a certified InputArc.key. See cafe.stations.batch_triage.                                                                                                                                                                                                        | `False`                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `decaf`                | `bool`  | Add the ☕ decaf-only barista — an InputArc.filter with no key, which never gets a key index however well it certifies. See cafe.stations.decaf.                                                                                                                                                                                  | `False`                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `knock_box`            | `bool`  | Add the 🥁 knock box — a consume_all arc behind a mostly-false guard, re-scanned in full on every step(). See cafe.stations.knock_box.                                                                                                                                                                                            | `False`                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `knock_box_min_pucks`  | `int`   | How full the bin must be before the barista empties it — the lull-frequency knob. Only meaningful with knock_box=True.                                                                                                                                                                                                            | `25`                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `specials_board`       | `bool`  | Add the 🧾 specials board — an uncertified InputArc.key computing the same ordering as batch_triage, so the two are an A/B pair for what certification is worth. See cafe.stations.specials_board.                                                                                                                                | `False`                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `eighty_six`           | `bool`  | Add the 🚫 86 board — a certified key behind an uncertified filter, which disqualifies the whole arc from indexing. See cafe.stations.eighty_six.                                                                                                                                                                                 | `False`                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `cupping`              | `bool`  | Add the 🥄 cupping table — a count > 1 keyed arc under a guard, which stresses the candidate space rather than the token pool. See cafe.stations.cupping.                                                                                                                                                                         | `False`                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `cupping_count`        | `int`   | Cups per flight. Only meaningful with cupping=True.                                                                                                                                                                                                                                                                               | `4`                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `pastry_case`          | `bool`  | Add the 🥐 pastry case — the fixture's only SubstitutionTransition, driving a nested kitchen subnet to quiescence per firing. See cafe.stations.pastry_case.                                                                                                                                                                      | `False`                                                                                                                                                                                                                                                                                                                                                                                                                  |

Raises:

| Type         | Description                                |
| ------------ | ------------------------------------------ |
| `ValueError` | If cold_brew_key is set without cold_brew. |

Source code in `benchmarks/cafe/net.py`

```
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
```

## Core places

The base topology — everything you get from a bare `build_cafe()`.

## cafe.places

The ☕ Concurrency Cafe's core places — one documented factory per station.

Every place in the base topology gets its own factory here rather than being constructed inline in `build_cafe`, so each one is an addressable symbol with a docstring covering both halves of what it is: the **cafe role** (what a customer or barista would see) and the **net feature** it exists to demonstrate.

Opt-in stations live in cafe.stations; this module is only what you get with a bare `build_cafe()`.

### p_ticket_line

```
p_ticket_line() -> Place
```

**P_Ticket_Line** — the register queue every order enters through.

Cafe role

The rail of paper tickets above the bar. The register never turns a customer away, it just adds another ticket, so this queue has no ceiling. It is also where a re-dosed ticket comes *back* to (`T_Rework_Dose` outputs here), which is why the line can grow from the middle of the net and not only from outside deposits.

Demonstrates

The plain unbounded FIFO Place — `schema=is_order` requires each ticket to carry its dose `weight_g` (a real reject, unlike `schema=dict`, which every mapping payload trivially satisfies), but it has no colour set, no bound, and no initial marking. It is the net's **deep** place: the throughput benchmark stocks it with up to 20 000 tickets, which is what makes it the place where marking depth actually shows up in engine cost. It is drained by `T_Weigh_And_Grind` under BindingPolicy.PRIORITY, so it is also the one place whose depth feeds a full candidate enumeration rather than a head-only read.

Source code in `benchmarks/cafe/places.py`

```
def p_ticket_line() -> Place:
    """**P_Ticket_Line** — the register queue every order enters through.

    Cafe role:
        The rail of paper tickets above the bar. The register never turns a customer
        away, it just adds another ticket, so this queue has no ceiling. It is also
        where a re-dosed ticket comes *back* to (`T_Rework_Dose` outputs here), which
        is why the line can grow from the middle of the net and not only from
        outside deposits.

    Demonstrates:
        The plain unbounded FIFO [`Place`][cpnx.Place] — `schema=is_order` requires each ticket
        to carry its dose `weight_g` (a real reject, unlike `schema=dict`, which every mapping
        payload trivially satisfies), but it has no colour set, no bound, and no initial marking.
        It is the net's **deep** place: the throughput benchmark stocks it with up to 20 000
        tickets, which is what makes it the place where marking depth actually shows
        up in engine cost. It is drained by `T_Weigh_And_Grind` under
        [`BindingPolicy.PRIORITY`][cpnx.BindingPolicy], so it is also the one place whose depth feeds a
        full candidate enumeration rather than a head-only read.
    """
    return Place("P_Ticket_Line", schema=is_order)
```

### p_digital_scales

```
p_digital_scales(capacity: int = 3) -> ResourcePlace
```

**P_Digital_Scales** — the shared pool of bench scales.

Cafe role

Three digital scales sit on the back bench. A barista grabs one to weigh the dose and puts it straight back; nobody holds one for longer than the weighing takes.

Demonstrates

ResourcePlace as a permit pool — pre-filled with `capacity` `"resource"` tokens at construction, and auto-returned by the engine once the consuming action completes, so `_weigh_and_grind` never has to hand the permit back explicitly. Because a permit arc contributes `C(capacity, count)` interchangeable options to the Cartesian product, this place is also one of the two multipliers that divide `T_Weigh_And_Grind`'s `binding_search_limit` budget (see t_weigh_and_grind).

Parameters:

| Name       | Type  | Description                                      | Default |
| ---------- | ----- | ------------------------------------------------ | ------- |
| `capacity` | `int` | How many scales are on the bench. Defaults to 3. | `3`     |

Source code in `benchmarks/cafe/places.py`

```
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
```

### p_burr_grinder

```
p_burr_grinder(
    grinders: int = 2, pacing_secs: float = 8.0
) -> PacedResourcePlace
```

**P_Burr_Grinder** — the grinders, each needing a breather between doses.

Cafe role

Two burr grinders behind the counter (an espresso grinder and a decaf one). After dispensing, a grinder is unavailable for `pacing_secs` while the burrs spin down and the chute gets brushed out — a hard rate limit on how fast the bar can physically produce grounds.

Demonstrates

PacedResourcePlace, i.e. a permit pool whose returned permits are future-dated by `pacing_secs` rather than being immediately re-usable. This is the net's source of genuine **back-pressure** and the reason the macro benchmarks drive a logical clock: the cooldown is real (the grinder truly is unavailable for 8 logical seconds) but waiting it out costs no wall time.

Note this is a *shallow* timed place — capacity 2-3 — which is exactly what makes cafe.stations.cold_brew's deep timed place a distinct shape worth benchmarking separately.

Parameters:

| Name          | Type    | Description                                                  | Default |
| ------------- | ------- | ------------------------------------------------------------ | ------- |
| `grinders`    | `int`   | Number of grinders, i.e. the permit capacity. Defaults to 2. | `2`     |
| `pacing_secs` | `float` | Cooldown applied to each returned permit. Defaults to 8.0.   | `8.0`   |

Source code in `benchmarks/cafe/places.py`

```
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
```

### p_ground_coffee

```
p_ground_coffee() -> Place
```

**P_Ground_Coffee** — dosed grounds waiting for a group head.

Cafe role

A portafilter of ground coffee sitting on the bar, waiting for a free group on the espresso machine. Also where a channeled shot's grounds are *rolled back to* when `T_Pull_Shot` fails and the engine retries it.

Demonstrates

A colour-restricted and schema-validated Place — `color_set={"ground_coffee"}` and `schema=is_order` make the place reject any token of the wrong colour or any payload missing its dose `weight_g` (grounds are derived from the order via `Token.evolve`, so they carry it), which turns a mis-wired output arc into an immediate error instead of a silently weird marking. Because it is the retry target, it is also the shallow queue that the channeling regime's extra `step()`s fire against (which is why retries make µs/*step* look cheaper while making the run strictly more expensive).

Source code in `benchmarks/cafe/places.py`

```
def p_ground_coffee() -> Place:
    """**P_Ground_Coffee** — dosed grounds waiting for a group head.

    Cafe role:
        A portafilter of ground coffee sitting on the bar, waiting for a free group
        on the espresso machine. Also where a channeled shot's grounds are *rolled
        back to* when `T_Pull_Shot` fails and the engine retries it.

    Demonstrates:
        A colour-restricted and schema-validated [`Place`][cpnx.Place] — `color_set={"ground_coffee"}`
        and `schema=is_order` make the place reject any token of the wrong colour or any
        payload missing its dose `weight_g` (grounds are derived from the order via
        `Token.evolve`, so they carry it), which turns a mis-wired output arc into an
        immediate error instead of a silently weird marking. Because it is the retry target,
        it is also the shallow queue that the channeling regime's extra `step()`s fire
        against (which is why retries make µs/*step* look cheaper while making the
        run strictly more expensive).
    """
    return Place("P_Ground_Coffee", color_set={"ground_coffee"}, schema=is_order)
```

### p_milk_queue

```
p_milk_queue() -> Place
```

**P_Milk_Queue** — milk tickets waiting for a steam wand.

Cafe role

The other half of an order. `T_Weigh_And_Grind` splits one ticket into two parallel work items, and this is the branch that becomes steamed milk while the grounds branch becomes a shot.

Demonstrates

A second colour-restricted and schema-validated Place (`{"milk_ticket"}`, `schema=is_order` — the milk ticket is `evolve`d from the order and keeps its `weight_g`), and — jointly with p_ground_coffee — the net's **fork**: one transition writing two output arcs into two different places, so the two downstream stations become independently enabled and can genuinely run concurrently.

Source code in `benchmarks/cafe/places.py`

```
def p_milk_queue() -> Place:
    """**P_Milk_Queue** — milk tickets waiting for a steam wand.

    Cafe role:
        The other half of an order. `T_Weigh_And_Grind` splits one ticket into two
        parallel work items, and this is the branch that becomes steamed milk while
        the grounds branch becomes a shot.

    Demonstrates:
        A second colour-restricted and schema-validated [`Place`][cpnx.Place] (`{"milk_ticket"}`,
        `schema=is_order` — the milk ticket is `evolve`d from the order and keeps its
        `weight_g`), and — jointly with [`p_ground_coffee`][cafe.places.p_ground_coffee] — the net's
        **fork**: one transition writing two output arcs into two different places,
        so the two downstream stations become independently enabled and can genuinely
        run concurrently.
    """
    return Place("P_Milk_Queue", color_set={"milk_ticket"}, schema=is_order)
```

### p_espresso_machine

```
p_espresso_machine(capacity: int = 2) -> ResourcePlace
```

**P_Espresso_Machine** — group heads on the espresso machine.

Cafe role

A two-group machine: two shots can pull at once instead of every pull serializing behind a single group.

Demonstrates

ResourcePlace used to *buy parallelism* rather than to model scarcity. This is the knob that decides how much of `max_workers` the shot station can actually use — with `capacity=1` the pool size is irrelevant downstream of the grinder, which is the failure mode the concurrency benchmark exists to detect.

Parameters:

| Name       | Type  | Description                           | Default |
| ---------- | ----- | ------------------------------------- | ------- |
| `capacity` | `int` | Number of group heads. Defaults to 2. | `2`     |

Source code in `benchmarks/cafe/places.py`

```
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
```

### p_steam_wand

```
p_steam_wand(capacity: int = 2) -> ResourcePlace
```

**P_Steam_Wand** — steam wands for the milk line.

Cafe role

Two wands on the machine, so two milks steam at once.

Demonstrates

The milk line's mirror of p_espresso_machine — the same ResourcePlace shape on the parallel branch, so neither branch is structurally privileged and the fork's two arms have symmetric capacity.

Parameters:

| Name       | Type  | Description                     | Default |
| ---------- | ----- | ------------------------------- | ------- |
| `capacity` | `int` | Number of wands. Defaults to 2. | `2`     |

Source code in `benchmarks/cafe/places.py`

```
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
```

### p_order_tray

```
p_order_tray(
    threshold: int = 2, bound: int | None = 6
) -> ThresholdPlace
```

**P_Order_Tray** — the hand-off counter where a drink is assembled.

Cafe role

A drink isn't done until *both* its espresso shot and its steamed milk have landed on the tray. The counter also physically fits only so many cups — once it's full, the bar has to clear it before pulling more.

Demonstrates

Two orthogonal CPN concepts on one place, which is precisely why they are set through two different mechanisms:

- `ThresholdPlace(threshold=2)` — the **rendezvous**. The place refuses to be retrieved from at all until 2 tokens have accumulated, encoding "wait for both halves" directly on the place instead of in a guard.
- `bound` — the **k-bound**, a plain settable attribute inherited from Place (the ThresholdPlace constructor deliberately does not expose it, since a threshold and a capacity are unrelated ideas). This is what gives `T_Pull_Shot`/`T_Steam_Milk` real output-capacity back-pressure.

It is also the net's only `count=2` input arc and its only `settle_secs` arc — see t_serve_drink.

Parameters:

| Name        | Type  | Description                                                  | Default                                                           |
| ----------- | ----- | ------------------------------------------------------------ | ----------------------------------------------------------------- |
| `threshold` | `int` | Tokens that must accumulate before any retrieval is allowed. | `2`                                                               |
| `bound`     | \`int | None\`                                                       | Optional k-bound (cups the counter fits). None removes the bound. |

Source code in `benchmarks/cafe/places.py`

```
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
    # `schema=has_payload`: the tray holds espresso and steamed-milk tokens (heterogeneous
    # colours), so it can't require one key — but a payload-less token here is a wiring bug.
    tray = ThresholdPlace("P_Order_Tray", threshold=threshold, schema=has_payload)
    # ThresholdPlace's constructor doesn't expose `bound` (threshold and k-bound are
    # orthogonal CPN concepts), but `bound` is a plain, settable attribute inherited
    # from Place.
    tray.bound = bound
    return tray
```

### p_served

```
p_served() -> SinkPlace
```

**P_Served** — the hatch where finished drinks leave the system.

Cafe role

Drinks go out to customers and never come back. The shop counts them and forgets them.

Demonstrates

SinkPlace as a terminal absorber — tokens deposited here are counted in `stats()["absorbed"]` but not retained (`keep_last=0`), so a 20 000-order run does not accumulate 20 000 live tokens in the marking. That is what keeps the deep throughput sweeps measuring the *drain*, rather than measuring memory growth at the far end of the pipeline.

`schema=has_payload`: served drinks are heterogeneous — a freshly-assembled `{"components": ...}` token from `T_Serve_Drink`, or a drive-through station's `evolve`d ticket — so a single required key would be wrong, but a payload-less token still signals a wiring bug.

Source code in `benchmarks/cafe/places.py`

```
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

        `schema=has_payload`: served drinks are heterogeneous — a freshly-assembled
        `{"components": ...}` token from `T_Serve_Drink`, or a drive-through station's
        `evolve`d ticket — so a single required key would be wrong, but a payload-less
        token still signals a wiring bug.
    """
    return SinkPlace("P_Served", schema=has_payload)
```

### p_trash_can

```
p_trash_can(keep_last: int = 10) -> SinkPlace
```

**P_Trash_Can** — the knock-out bin for shots that couldn't be saved.

Cafe role

A channeled shot gets one more attempt; if it channels again the barista bins it. The last few are kept on the bench for a quality check at close.

Demonstrates

Two roles at once. As a SinkPlace with `keep_last=10` it is a **bounded retaining sink** — absorb-and-count like p_served, but holding a rolling window for inspection. It is *also* the net's `error_place`, so the engine dead-letters a transition's data tokens here automatically once `max_retries` is exhausted, without any arc being drawn to it.

Deliberately carries **no** `schema`: as the `error_place` it must accept whatever gets dead-lettered — including the error-coloured tokens the engine mints for *schema* violations elsewhere — so constraining it would risk rejecting a dead-letter inside the locked commit and stranding it. See SinkPlace's error-place warning.

Parameters:

| Name        | Type  | Description                                          | Default |
| ----------- | ----- | ---------------------------------------------------- | ------- |
| `keep_last` | `int` | Size of the retained rolling window. Defaults to 10. | `10`    |

Source code in `benchmarks/cafe/places.py`

```
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

        Deliberately carries **no** `schema`: as the `error_place` it must accept whatever
        gets dead-lettered — including the error-coloured tokens the engine mints for *schema*
        violations elsewhere — so constraining it would risk rejecting a dead-letter inside the
        locked commit and stranding it. See [`SinkPlace`][cpnx.SinkPlace]'s error-place warning.

    Args:
        keep_last: Size of the retained rolling window. Defaults to 10.
    """
    return SinkPlace("P_Trash_Can", keep_last=keep_last)
```

## Core transitions

## cafe.transitions

The ☕ Concurrency Cafe's core transitions — one documented factory per station.

Each factory returns a fully-wired Transition and documents both halves of what it is: the **cafe role** (what the barista is doing) and the **net features** it exercises — binding policy, guard, arc shape, timeouts, retries.

Opt-in stations live in cafe.stations; this module is only what you get with a bare `build_cafe()`.

### t_weigh_and_grind

```
t_weigh_and_grind(
    *,
    work_secs: float = 0.0,
    dose_low: float | None = None,
    dose_high: float | None = None,
    resource_arcs_first: bool = False,
) -> Transition
```

**T_Weigh_And_Grind** — weigh the dose, grind it, split the order in two.

Cafe role

The first real station. A barista takes the next ticket off the rail, grabs a free scale and a free grinder, weighs the dose, and — if it's in spec — grinds it. Mobile-pickup tickets get pulled ahead of walk-ins. An out-of-spec dose never reaches the grinder; `T_Rework_Dose` takes it instead.

Demonstrates

The net's densest transition, and the one every guarded benchmark number comes from:

- **BindingPolicy.PRIORITY over a deep place.** It enumerates the whole (bounded) candidate set rather than short-circuiting, so it is where per-candidate guard dispatch actually costs something.
- **A guard on the enumerated path** — make_dose_guard, evaluated once per candidate binding. Passing `dose_low=None` omits it entirely, which is the fixture's guard-free A/B arm.
- **`binding_priority_key`** (mobile_pickup_first) — a transition-level tie-break, distinct from an arc-level `key`.
- **Two permit arcs plus a data arc**, i.e. a three-dimensional Cartesian product — see `resource_arcs_first`.
- **`action_timeout_secs`** — weighing is a quick bounded action, so it gets a short deadline.

Warning

`binding_search_limit` (default 1000) is spent against raw Cartesian *product* tuples, so the permit arcs divide the usable ticket depth: `effective_depth ≈ limit / (scales × grinders)`. On the default topology mobile-pickup preference holds to depth ~166 and is silently gone by ~170 (1000 / (3 × 2)); with `grinders=1` it held to ~333. Past that the scan still runs and still costs — it just stops finding the token it is looking for and falls back to insertion order, with no error and no warning. That is [#18](https://github.com/philgresh/cpnx/issues/18): the bug is budget *accounting*, not the limit itself. Raising `grinders` to buy parallelism makes it bite twice as early.

Parameters:

| Name                  | Type    | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | Default                                                        |
| --------------------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| `work_secs`           | `float` | Physical seconds the station occupies a worker.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | `0.0`                                                          |
| `dose_low`            | \`float | None\`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | Lower bound of the acceptable dose band. None omits the guard. |
| `dose_high`           | \`float | None\`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | Upper bound of the acceptable dose band. None omits the guard. |
| `resource_arcs_first` | `bool`  | Which order the input arcs are listed in — the fixture's handle on the arc-ordering tuning lever documented on BindingPolicy. itertools.product varies the last arc fastest, so listing the deep data arc last makes the ticket dimension the one that changes first, and the search's first limit candidates then sweep limit distinct tickets instead of limit / (scales × grinders) of them. False (the default) keeps the historical data-arc-first order so existing numbers stay comparable; True is the documented-recommended order and should raise effective ticket depth by the permit-arc product without changing semantics. | `False`                                                        |

Source code in `benchmarks/cafe/transitions.py`

```
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
```

### t_pull_shot

```
t_pull_shot(
    *,
    work_secs: float = 0.0,
    channel_failure_rate: float = 0.15,
    channel_seed: int | None = None,
) -> Transition
```

**T_Pull_Shot** — lock in the portafilter and pull an espresso.

Cafe role

Grounds plus a free group head become a shot on the tray. Sometimes the puck channels and the shot is ruined; the barista re-doses and tries once more before binning it.

Demonstrates

The **failure path**, which is the only place in the net where a transition raises:

- **Atomic rollback** — a raising action returns the grounds token to `P_Ground_Coffee` *and* the group-head permit to its pool, together.
- **`max_retries=1`** — one retry, then the engine dead-letters the data token to the net's `error_place`. No arc is drawn to `P_Trash_Can`; the engine routes it.
- **`retry_delay` on the model clock** — a rolled-back token comes back future-dated, which is what makes the retry regime measurable on a logical clock at all.

Parameters:

| Name                   | Type    | Description                                       | Default                                                  |
| ---------------------- | ------- | ------------------------------------------------- | -------------------------------------------------------- |
| `work_secs`            | `float` | Physical seconds the station occupies a worker.   | `0.0`                                                    |
| `channel_failure_rate` | `float` | Probability a pull channels. 0.0 removes all RNG. | `0.15`                                                   |
| `channel_seed`         | \`int   | None\`                                            | Seed for a private RNG; only effective at max_workers=1. |

Source code in `benchmarks/cafe/transitions.py`

```
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
```

### t_steam_milk

```
t_steam_milk(*, work_secs: float = 0.0) -> Transition
```

**T_Steam_Milk** — steam the milk and send it to the tray.

Cafe role

The parallel arm of the order. A free wand plus a milk ticket becomes steamed oat or dairy milk, which joins the shot on the tray.

Demonstrates

The net's only **OutputArc.condition** usage, via the `OutputArc.on_color(...)` constructor. Two arcs point at the *same* place and are distinguished purely by their activation predicates, so the oat/dairy branch stays legible in the event log. `on_color` closes over an immutable string, so both conditions certify and run inline.

Parameters:

| Name        | Type    | Description                                     | Default |
| ----------- | ------- | ----------------------------------------------- | ------- |
| `work_secs` | `float` | Physical seconds the station occupies a worker. | `0.0`   |

Source code in `benchmarks/cafe/transitions.py`

```
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
```

### t_serve_drink

```
t_serve_drink(
    *,
    work_secs: float = 0.0,
    tray_settle_secs: float = 0.05,
) -> Transition
```

**T_Serve_Drink** — bus the tray and call the drink.

Cafe role

Once a shot and a milk are both on the counter, the barista waits a beat to see whether the rest of the order lands, then assembles and serves.

Demonstrates

The **rendezvous join**, and the net's only use of two arc features:

- **`count=2`** — one firing drains exactly one drink's worth. Combined with `P_Order_Tray`'s `threshold=2`, the transition simply cannot become enabled on a half-order.
- **`settle_secs`** — a quiet-period requirement on the *place*, not a delay on the token: the arc refuses to fire until no new token has arrived for this long. It is the only arc exercising that branch of the engine's availability check (and of `benchmarks/_driver.py`'s clock advance).

Parameters:

| Name               | Type    | Description                                       | Default |
| ------------------ | ------- | ------------------------------------------------- | ------- |
| `work_secs`        | `float` | Physical seconds the station occupies a worker.   | `0.0`   |
| `tray_settle_secs` | `float` | Quiet period required on the tray before serving. | `0.05`  |

Source code in `benchmarks/cafe/transitions.py`

```
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
```

### t_rework_dose

```
t_rework_dose(
    *,
    work_secs: float = 0.0,
    dose_low: float,
    dose_high: float,
) -> Transition
```

**T_Rework_Dose** — re-dose a ticket whose weight missed spec.

Cafe role

The scale read out of band. Rather than grinding a bad dose, the barista adjusts and puts the ticket back on the rail.

Demonstrates

A **self-loop** — the transition's input and output are the same place, so a reworked ticket re-enters the line and is re-evaluated by both guards. Two things keep that from being a livelock: the action clamps into the band, and this guard is the exact complement of the grind guard.

Also the fixture's one deliberate use of the default **BindingPolicy.LEGACY** on a deep place: it only ever inspects the head of `P_Ticket_Line`. A ticket not yet at the head is simply skipped over by `T_Weigh_And_Grind`'s PRIORITY search, so it reaches the head eventually without this transition needing to enumerate for it — a worked example of choosing the cheap policy where completeness isn't needed.

Parameters:

| Name        | Type    | Description                                     | Default    |
| ----------- | ------- | ----------------------------------------------- | ---------- |
| `work_secs` | `float` | Physical seconds the station occupies a worker. | `0.0`      |
| `dose_low`  | `float` | Lower bound of the acceptable dose band.        | *required* |
| `dose_high` | `float` | Upper bound of the acceptable dose band.        | *required* |

Source code in `benchmarks/cafe/transitions.py`

```
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
```

## Inscriptions — guards and binding keys

The net's predicates and orderings. These run **under the engine lock** and are purity-verified, which is why they are kept apart from the actions.

## cafe.inscriptions

Arc and transition *inscriptions* for the core cafe — guards and binding keys.

In CPN terms this module holds the net's predicates and orderings, kept separate from the actions that do the work: a guard decides *whether* a transition may fire, a binding key decides *which* tokens it fires with, and neither is allowed to have side effects. The engine enforces that split — everything here is purity-verified at construction, and evaluated while the engine lock is held.

Every callable here is a closure over immutable values (or over nothing at all), which is what makes it **certify** for inline evaluation under `cpnx.certification` instead of paying a thread round-trip per call. The opt-in stations in cafe.stations include deliberately *uncertified* counterparts so the two dispatch paths can be measured against each other.

### make_dose_guard

```
make_dose_guard(low: float, high: float)
```

Build **T_Weigh_And_Grind**'s guard: a barista won't grind an out-of-spec dose.

Cafe role

Weighing the dose is the whole point of the scale step. A reading outside the shop's tolerance band (too little grounds under-extracts, too much over-extracts) doesn't get ground — it goes back for a re-dose instead, via `T_Rework_Dose`.

Demonstrates

A **transition guard** (`Type[G(t)] = Bool`) evaluated once per *candidate binding*. Because the transition it gates runs BindingPolicy.PRIORITY over a deep place, this is the single most-evaluated callable in the net — the profiler attributes the bulk of a guarded run to dispatching it. It is a callable closing over the immutable `low`/`high` floats, so it certifies and runs inline; the same predicate reading a mutable module global would not.

Source code in `benchmarks/cafe/inscriptions.py`

```
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
```

### make_rework_guard

```
make_rework_guard(low: float, high: float)
```

Build **T_Rework_Dose**'s guard: the exact complement of make_dose_guard.

Cafe role

A ticket is reworked precisely when its dose is *out* of the tolerance band — the two guards partition the ticket line between the grind station and the re-dose station with no overlap and no gap.

Demonstrates

**Complementary guards as a routing mechanism.** Two transitions share one input place and are told apart purely by their predicates, so no ticket can take both paths and none can stall with neither enabled. Combined with make_rework_dose's clamping, it is also what makes the rework loop provably terminate rather than ping-pong.

Source code in `benchmarks/cafe/inscriptions.py`

```
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
```

### mobile_pickup_first

```
mobile_pickup_first(
    tokens: list[Token],
) -> tuple[int, float]
```

**T_Weigh_And_Grind**'s `binding_priority_key`: app orders jump the in-store line.

Cafe role

A mobile-pickup ticket is already paid for and its customer is walking over, so the bar pulls it ahead of a walk-in. Among tickets of the same kind, the oldest goes first.

Demonstrates

BindingPolicy.PRIORITY plus a `binding_priority_key` — a **transition-level** tie-break that selects the minimum-key binding among the enumerated candidate set. Contrast cafe.stations.batch_triage's InputArc.key, which is a different mechanism entirely: that reorders one arc's *token pool*, this chooses among whole *bindings* after enumeration.

Note the key is invoked inline under the engine lock with **no timeout**, once per candidate — which is why it does nothing but read two payload fields.

Source code in `benchmarks/cafe/inscriptions.py`

```
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
```

## Actions

The work a barista actually does. Actions run on the thread pool, **outside** the lock, and are the one part of a net explicitly allowed side effects.

## cafe.actions

The core cafe's transition *actions* — the work a barista actually does.

Actions are the one part of a cpnx net that is explicitly allowed side effects: they run on the engine's thread pool, **outside** the engine lock, and are not purity-verified. That is the whole reason guards and binding keys live in cafe.inscriptions instead — those run *under* the lock and must stay pure and trivially cheap.

A recurring idiom below: an action that consumes a permit filters its input with `is_resource` rather than indexing `tokens[0]`, because arc order does not guarantee token position in the binding.

### weigh_and_grind

```
weigh_and_grind(tokens: list[Token]) -> list[Token]
```

**T_Weigh_And_Grind**'s action: split one ticket into grounds and a milk ticket.

Cafe role

The barista weighs the dose on a scale, grinds it, and the order becomes two parallel jobs — a portafilter to pull and a milk to steam.

Demonstrates

The net's **fork**: one action returning two differently-coloured tokens that the transition's two output arcs route to two different places, making the espresso and milk lines independently enabled from here on.

Also the resource-return contract — the scale and grinder permits consumed alongside the order are *not* returned here. The engine automatically deposits any consumed-but-unreturned resource token back into its source place once the action completes, so an action only ever has to produce the *data* tokens that carry the order forward.

Source code in `benchmarks/cafe/actions.py`

```
def weigh_and_grind(tokens: list[Token]) -> list[Token]:
    """**T_Weigh_And_Grind**'s action: split one ticket into grounds and a milk ticket.

    Cafe role:
        The barista weighs the dose on a scale, grinds it, and the order becomes two
        parallel jobs — a portafilter to pull and a milk to steam.

    Demonstrates:
        The net's **fork**: one action returning two differently-coloured tokens that
        the transition's two output arcs route to two different places, making the
        espresso and milk lines independently enabled from here on.

        Also the resource-return contract — the scale and grinder permits consumed
        alongside the order are *not* returned here. The engine automatically
        deposits any consumed-but-unreturned resource token back into its source
        place once the action completes, so an action only ever has to produce the
        *data* tokens that carry the order forward.
    """
    order = next(t for t in tokens if not t.is_resource)
    grounds = order.evolve(payload_updates={"stage": "grounds"}, color="ground_coffee")
    milk_ticket = order.evolve(payload_updates={"stage": "milk_ticket"}, color="milk_ticket")
    return [grounds, milk_ticket]
```

### make_rework_dose

```
make_rework_dose(low: float, high: float)
```

Build **T_Rework_Dose**'s action: adjust the grinder and re-weigh the ticket.

Cafe role

The dose came off the scale out of spec, so the barista nudges the grind setting and re-doses rather than pulling a bad shot.

Demonstrates

**Loop termination by construction.** Clamps to the *nearest* bound rather than snapping to the band's center, so a single rework always lands the weight back inside `[low, high]` and therefore satisfies make_dose_guard on the next pass. Snapping to the center would work too, but clamping makes the invariant local and obvious: the output of this action is, by definition, in the band the complementary guard tests.

Source code in `benchmarks/cafe/actions.py`

```
def make_rework_dose(low: float, high: float):
    """Build **T_Rework_Dose**'s action: adjust the grinder and re-weigh the ticket.

    Cafe role:
        The dose came off the scale out of spec, so the barista nudges the grind
        setting and re-doses rather than pulling a bad shot.

    Demonstrates:
        **Loop termination by construction.** Clamps to the *nearest* bound rather
        than snapping to the band's center, so a single rework always lands the
        weight back inside `[low, high]` and therefore satisfies
        [`make_dose_guard`][cafe.inscriptions.make_dose_guard] on the next pass. Snapping to the center would work too,
        but clamping makes the invariant local and obvious: the output of this
        action is, by definition, in the band the complementary guard tests.
    """

    def _rework_dose(tokens: list[Token]) -> list[Token]:
        ticket = tokens[0]
        weight = ticket.payload.get("weight_g", DOSE_TARGET_G)
        return [ticket.evolve(payload_updates={"weight_g": min(max(weight, low), high)})]

    return _rework_dose
```

### make_pull_shot

```
make_pull_shot(failure_rate: float, seed: int | None)
```

Build **T_Pull_Shot**'s action, with a configurable channeling failure rate.

Cafe role

Water finds a crack in the puck and runs straight through — a channeled, uneven extraction. The shot is ruined and the grounds are wasted.

Demonstrates

The **retry and dead-letter path**. Raising from an action makes the engine roll the binding back atomically: the grounds token returns to `P_Ground_Coffee` and the espresso permit returns to its pool. Combined with the transition's `max_retries=1`, a channeled shot gets exactly one more attempt before the engine routes it to the net's `error_place` (`P_Trash_Can`), so a ruined dose can't loop forever. At a 15% channel rate that yields a dead-letter rate near 0.15² — a shot must channel *twice* to be binned.

Parameters:

| Name           | Type    | Description                                                                                                                                      | Default                                                                                                                                                                                                                                                                                                                                                                                                                |
| -------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `failure_rate` | `float` | Probability a given pull channels. ~0.15 is realistic; 0.0 makes the run draw no RNG at all, so it reproduces step-for-step at any worker count. | *required*                                                                                                                                                                                                                                                                                                                                                                                                             |
| `seed`         | \`int   | None\`                                                                                                                                           | Swaps the global random module for a private random.Random(seed), making a channeling run reproducible — but only at max_workers=1. Above one worker the order in which concurrent firings draw from the shared generator is scheduler-dependent, so a fixed seed no longer pins which shots channel (and random.Random is not documented as thread-safe). The channeling benchmark regime is therefore single-worker. |

Source code in `benchmarks/cafe/actions.py`

```
def make_pull_shot(failure_rate: float, seed: int | None):
    """Build **T_Pull_Shot**'s action, with a configurable channeling failure rate.

    Cafe role:
        Water finds a crack in the puck and runs straight through — a channeled,
        uneven extraction. The shot is ruined and the grounds are wasted.

    Demonstrates:
        The **retry and dead-letter path**. Raising from an action makes the engine
        roll the binding back atomically: the grounds token returns to
        `P_Ground_Coffee` and the espresso permit returns to its pool. Combined with
        the transition's `max_retries=1`, a channeled shot gets exactly one more
        attempt before the engine routes it to the net's `error_place`
        (`P_Trash_Can`), so a ruined dose can't loop forever. At a 15% channel rate
        that yields a dead-letter rate near 0.15² — a shot must channel *twice* to be
        binned.

    Args:
        failure_rate: Probability a given pull channels. ~0.15 is realistic; `0.0`
            makes the run draw no RNG at all, so it reproduces step-for-step at any
            worker count.
        seed: Swaps the global `random` module for a private `random.Random(seed)`,
            making a channeling run reproducible — but **only at `max_workers=1`**.
            Above one worker the order in which concurrent firings draw from the
            shared generator is scheduler-dependent, so a fixed seed no longer pins
            *which* shots channel (and `random.Random` is not documented as
            thread-safe). The channeling benchmark regime is therefore
            single-worker.
    """
    rng = random.Random(seed) if seed is not None else random

    def _pull_shot(tokens: list[Token]) -> list[Token]:
        grounds = next(t for t in tokens if not t.is_resource)
        if failure_rate and rng.random() < failure_rate:
            raise RuntimeError("channeling detected — shot pulled unevenly, discarding grounds")
        return [grounds.evolve(payload_updates={"stage": "espresso"}, color="espresso")]

    return _pull_shot
```

### steam_milk

```
steam_milk(tokens: list[Token]) -> list[Token]
```

**T_Steam_Milk**'s action: steam oat or dairy depending on the original order.

Cafe role

The barista picks up the right jug — oat for a dairy-free ticket, dairy otherwise — and steams it.

Demonstrates

**Colour as a routing signal.** The action does not decide where the token goes; it only sets the colour, and the transition's two `OutputArc.on_color(...)` arcs decide. That keeps the branch visible in the event log even though both colours land on the same tray, and it is the net's only use of an output-arc `condition`.

Source code in `benchmarks/cafe/actions.py`

```
def steam_milk(tokens: list[Token]) -> list[Token]:
    """**T_Steam_Milk**'s action: steam oat or dairy depending on the original order.

    Cafe role:
        The barista picks up the right jug — oat for a dairy-free ticket, dairy
        otherwise — and steams it.

    Demonstrates:
        **Colour as a routing signal.** The action does not decide where the token
        goes; it only sets the colour, and the transition's two
        `OutputArc.on_color(...)` arcs decide. That keeps the branch visible in the
        event log even though both colours land on the same tray, and it is the
        net's only use of an output-arc `condition`.
    """
    ticket = next(t for t in tokens if not t.is_resource)
    color = "oat_milk" if ticket.payload.get("dairy_free") else "dairy_milk"
    return [ticket.evolve(payload_updates={"stage": color}, color=color)]
```

### serve_drink

```
serve_drink(tokens: list[Token]) -> list[Token]
```

**T_Serve_Drink**'s action: assemble a tray pair into one finished drink.

Cafe role

Shot and milk are both on the tray; the barista combines them and calls the drink.

Demonstrates

A **join** — two tokens in, one out, which is what makes the net non-conservative by design (see the package docstring's caveat).

Illustrative simplification worth knowing about: the tray is a plain FIFO ThresholdPlace, so the two tokens retrieved are whichever espresso/milk tokens happen to be at the head — *not* guaranteed to be the same customer's shot and milk. Fine for a fixture built to exercise concurrency and station wiring, but exactly the kind of thing conservation-checking (out of scope here) would catch.

Source code in `benchmarks/cafe/actions.py`

```
def serve_drink(tokens: list[Token]) -> list[Token]:
    """**T_Serve_Drink**'s action: assemble a tray pair into one finished drink.

    Cafe role:
        Shot and milk are both on the tray; the barista combines them and calls the
        drink.

    Demonstrates:
        A **join** — two tokens in, one out, which is what makes the net
        non-conservative by design (see the package docstring's caveat).

        Illustrative simplification worth knowing about: the tray is a plain FIFO
        [`ThresholdPlace`][cpnx.ThresholdPlace], so the two tokens retrieved are whichever espresso/milk
        tokens happen to be at the head — *not* guaranteed to be the same customer's
        shot and milk. Fine for a fixture built to exercise concurrency and station
        wiring, but exactly the kind of thing conservation-checking (out of scope
        here) would catch.
    """
    components = sorted(t.color for t in tokens)
    return [Token(color="drink", payload={"components": components})]
```

## Opt-in stations

Every station below is default-off and structure-preserving when off, so `build_cafe()` with no flags is exactly the base topology and long-standing benchmark numbers stay comparable. Each exists because there is some engine cost path the base net never touches.

## cafe.stations

Opt-in cafe stations — one module per station, every one default-off.

Each module in this package is a **self-contained station**: its own places, its own transitions, and whatever guards, keys, filters, and actions those need. A station exists because there is some engine cost path the base topology never touches, and each module's docstring names that path explicitly.

#### Station module contract

Every module here exposes exactly two entry points, so cafe.net.build_cafe can wire any subset of them without knowing anything about a particular station:

```
def places() -> list[Place]: ...
def transitions(*, work_secs: float = 0.0) -> list[Transition]: ...
```

A station may add further **keyword-only** parameters of its own — `cold_brew` takes `key`, `knock_box` takes `min_pucks`, `cupping` takes `count` — and `build_cafe` forwards them from correspondingly-named flags. Both functions must be callable with no arguments at all, and neither may mutate anything outside its own return value. Nothing in this package deposits tokens — a station only declares structure, and the benchmark that uses it stocks the queue itself. That is what keeps a station's depth a property of the *experiment* rather than of the fixture.

#### Why default-off

Every station here is structure-preserving when disabled: `build_cafe()` with no flags returns exactly the base topology, so the long-standing benchmark numbers stay comparable. Turning a station on adds places and transitions but changes nothing about the ones already there.

### 🧊 Cold-brew tower — a deep timed place

## cafe.stations.cold_brew

🧊 The cold-brew tower — a genuinely **deep timed** place.

Cafe role

A rack of cold-brew batches steeping overnight. Each batch is put up at a different time and is undrinkable until its own steep has elapsed; when one matures a barista pours it straight over ice. No grinder, no group head, no steam wand — cold brew bypasses the whole espresso pipeline.

Demonstrates

The **deep timed marking**. Every other timed thing in the cafe is a PacedResourcePlace with capacity 2-3, so its cooling set never holds more than a handful of entries. A cold-brew tower holds dozens-to-hundreds of concurrently-steeping tokens, each with its own future Token.available_at, which is the only shape that puts real pressure on the token store's cooling min-heap and on the engine's `_earliest_cooldown_boundary` clock advance.

Note the *place* is a plain Place — nothing about the class makes it timed. What makes it timed is that the tokens deposited into it carry a future `available_at`, which is why nothing here deposits: the benchmark stocks the tower itself.

With `key=True` this station also reproduces the **timed×key residual** ([#25](https://github.com/philgresh/cpnx/issues/25)) — see `cold_brew_key`.

### cold_brew_key

```
cold_brew_key(token: Token) -> tuple[int, float]
```

InputArc.key for the tower: biggest cup first, then oldest batch.

Cafe role

Faced with a rack of matured batches, a barista pulls the one that fills the largest pending cup — a 20oz order empties a batch usefully, a 12oz leaves an awkward remainder. Ties go to whichever has been steeping longest.

Demonstrates

**The timed×key residual, deliberately.** This key is perfectly ordinary and fully *certified* — a pure per-token closure over the token's own payload, no closed-over mutable state — so on any untimed place it would be served from the place's persistent `(key, seq)` min-heap in O(cap log cap).

It is not, because the place it sits on holds cooling tokens. Place.peek_by_key refuses to answer whenever the store has *any* cooling entry: the key index covers the ready set only, and a cooling token is served straight off the cooling heap without ever migrating into the ready set, so the index cannot claim to represent the whole available pool. Rather than return a silently incomplete ordering, it declines and the engine falls back to the per-firing filter-then-sort over the full marking.

The result is the one retrieval shape in the corpus that is still ≈O(N² log N) despite doing everything the documentation asks. `build_cafe(cold_brew=True, cold_brew_key=True)` is its reproducer; the plain `cold_brew=True` arm is the control, identical in every respect except the arc's `key`.

Source code in `benchmarks/cafe/stations/cold_brew.py`

```
def cold_brew_key(token: Token) -> tuple[int, float]:
    """[`InputArc.key`][cpnx.InputArc] for the tower: biggest cup first, then oldest batch.

    Cafe role:
        Faced with a rack of matured batches, a barista pulls the one that fills the
        largest pending cup — a 20oz order empties a batch usefully, a 12oz leaves an
        awkward remainder. Ties go to whichever has been steeping longest.

    Demonstrates:
        **The timed×key residual, deliberately.** This key is perfectly ordinary and
        fully *certified* — a pure per-token closure over the token's own payload, no
        closed-over mutable state — so on any untimed place it would be served from
        the place's persistent `(key, seq)` min-heap in O(cap log cap).

        It is not, because the place it sits on holds cooling tokens.
        [`Place.peek_by_key`][cpnx.Place.peek_by_key] refuses to answer whenever the store has *any* cooling
        entry: the key index covers the ready set only, and a cooling token is served
        straight off the cooling heap without ever migrating into the ready set, so
        the index cannot claim to represent the whole available pool. Rather than
        return a silently incomplete ordering, it declines and the engine falls back
        to the per-firing filter-then-sort over the full marking.

        The result is the one retrieval shape in the corpus that is still ≈O(N² log N)
        despite doing everything the documentation asks. `build_cafe(cold_brew=True,
        cold_brew_key=True)` is its reproducer; the plain `cold_brew=True` arm is the
        control, identical in every respect except the arc's `key`.
    """
    return (-int(token.payload.get("cup_oz", 12)), token.created_at)
```

### pull_cold_brew

```
pull_cold_brew(tokens: list[Token]) -> list[Token]
```

**T_Pull_Cold_Brew**'s action: pour a matured batch straight into a served drink.

Cafe role

Cold brew is pre-brewed and poured over ice, so a matured batch goes directly to the hatch rather than through the shot/milk rendezvous.

Demonstrates

That **maturity needs no check in user code**. The engine refuses to hand this action a token whose `available_at` is still in the future (see Place.retrieve), so arrival at this action *is* the "matured" signal — there is deliberately no timestamp comparison in the body.

Source code in `benchmarks/cafe/stations/cold_brew.py`

```
def pull_cold_brew(tokens: list[Token]) -> list[Token]:
    """**T_Pull_Cold_Brew**'s action: pour a matured batch straight into a served drink.

    Cafe role:
        Cold brew is pre-brewed and poured over ice, so a matured batch goes directly
        to the hatch rather than through the shot/milk rendezvous.

    Demonstrates:
        That **maturity needs no check in user code**. The engine refuses to hand this
        action a token whose `available_at` is still in the future (see
        [`Place.retrieve`][cpnx.Place.retrieve]), so arrival at this action *is* the "matured" signal — there
        is deliberately no timestamp comparison in the body.
    """
    steeped = tokens[0]
    return [steeped.evolve(payload_updates={"stage": "cold_brew"}, color="drink")]
```

### places

```
places() -> list[Place]
```

The tower itself — one colour-restricted and schema-validated Place holding steeping batches.

Source code in `benchmarks/cafe/stations/cold_brew.py`

```
def places() -> list[Place]:
    """The tower itself — one colour-restricted and schema-validated [`Place`][cpnx.Place] holding steeping batches."""
    return [Place("P_Cold_Brew_Steeping", color_set={"cold_brew"}, schema=has_payload)]
```

### transitions

```
transitions(
    *, work_secs: float = 0.0, key: bool = False
) -> list[Transition]
```

**T_Pull_Cold_Brew** — pour whatever has finished steeping.

Demonstrates

With `key=False` (the default) this is deliberately the plainest transition in the whole fixture: no guard, no `key`, no `filter`, default `LEGACY` policy, one input arc, one output arc. Place.retrieve has already filtered to matured tokens before the arc sees them, so plain FIFO over "whatever's ready" is all it needs — and that isolates the *timed store* as the only thing being measured.

With `key=True` the arc carries `cold_brew_key` and the station becomes the timed×key reproducer instead. Everything else is held constant, so an A/B between the two arms attributes the whole difference to the index declining.

Parameters:

| Name        | Type    | Description                                                                                   | Default |
| ----------- | ------- | --------------------------------------------------------------------------------------------- | ------- |
| `work_secs` | `float` | Physical seconds the station occupies a worker.                                               | `0.0`   |
| `key`       | `bool`  | Attach cold_brew_key to the input arc, reproducing the timed×key residual. Defaults to False. | `False` |

Source code in `benchmarks/cafe/stations/cold_brew.py`

```
def transitions(*, work_secs: float = 0.0, key: bool = False) -> list[Transition]:
    """**T_Pull_Cold_Brew** — pour whatever has finished steeping.

    Demonstrates:
        With `key=False` (the default) this is deliberately the plainest transition in
        the whole fixture: no guard, no `key`, no `filter`, default `LEGACY` policy,
        one input arc, one output arc. [`Place.retrieve`][cpnx.Place.retrieve] has already filtered to matured
        tokens before the arc sees them, so plain FIFO over "whatever's ready" is all
        it needs — and that isolates the *timed store* as the only thing being
        measured.

        With `key=True` the arc carries `cold_brew_key` and the station becomes the
        timed×key reproducer instead. Everything else is held constant, so an A/B
        between the two arms attributes the whole difference to the index declining.

    Args:
        work_secs: Physical seconds the station occupies a worker.
        key: Attach `cold_brew_key` to the input arc, reproducing the timed×key
            residual. Defaults to `False`.
    """
    arc = InputArc("P_Cold_Brew_Steeping", key=cold_brew_key) if key else InputArc("P_Cold_Brew_Steeping")
    return [
        Transition(
            name="T_Pull_Cold_Brew",
            inputs=[arc],
            outputs=[OutputArc("P_Served")],
            action=with_work(work_secs, pull_cold_brew),
            action_timeout_secs=0.5,
        )
    ]
```

### 📋 Rush-hour triage — a certified `InputArc.key` at depth

## cafe.stations.batch_triage

📋 The rush-hour triage queue — a deep place drained through an InputArc.key.

Cafe role

Mid-rush the rail is twenty tickets deep and the barista stops working in strict arrival order. Oat-milk tickets get clustered together (switching milks means re-purging the wand every time), and within a milk group the tickets least likely to bounce through rework go first.

Demonstrates

The **certified InputArc.key fast path** — the shape the persistent `(key, seq)` min-heap on the place exists to serve, and the fixture's headline win: draining this queue went from ≈O(N² log N) to ≈O(N log N).

It is deliberately a *different mechanism* from `T_Weigh_And_Grind`'s `binding_priority_key`. That one reorders whole enumerated **bindings** at the transition level; this one reorders one arc's **token pool** before any binding is formed. Having both in one net is what makes the distinction legible.

Because the ordering value is per-token and pure, the engine can compute it once at deposit and keep it in a heap, rather than re-deriving it for the whole marking on every firing — which is exactly what an opaque `list[Token] -> list[Token]` arc expression could never allow. See [ADR 0004](https://github.com/philgresh/cpnx/blob/main/docs/adr/0004-arc-selection-key-filter.md).

### batch_triage_key

```
batch_triage_key(token: Token) -> tuple[int, int, float]
```

InputArc.key for the triage queue: how a barista triages a deep rush.

Cafe role

Not a random shuffle — a real batching heuristic, in two groupings:

1. **Oat before dairy.** Switching milks mid-rush means re-purging the steam wand every single time (carryover flavour), so a barista clusters every oat-milk ticket together before touching a dairy one rather than alternating.
1. **On-spec before out-of-spec.** Within a milk group, a ticket whose dose is already on target is pulled ahead of one likely to bounce through `T_Rework_Dose` — a rush doesn't want to get stuck behind a slow ticket.

Demonstrates

A **certified** per-token key. It reads only the token's own `payload` and `created_at` and closes over nothing mutable, so `cpnx.certification` proves it closed-world and the engine both (a) evaluates it inline rather than round-tripping it through the timeout-bounded expression pool, and (b) is willing to index it — an uncertified key cannot be indexed at all, because keying happens on the `deposit()` path, which cannot wait on an executor.

Ties fall to `created_at`, and the engine breaks any remaining tie by insertion order, so the drain stays deterministic. Note this reorders the *groups*, not the tickets within them: every ticket is still consumed eventually, just not in strict arrival order.

cafe.stations.specials_board holds the deliberately-uncertified twin of this function, for measuring what certification is worth.

Source code in `benchmarks/cafe/stations/batch_triage.py`

```
def batch_triage_key(token: Token) -> tuple[int, int, float]:
    """[`InputArc.key`][cpnx.InputArc] for the triage queue: how a barista triages a deep rush.

    Cafe role:
        Not a random shuffle — a real batching heuristic, in two groupings:

        1. **Oat before dairy.** Switching milks mid-rush means re-purging the steam
           wand every single time (carryover flavour), so a barista clusters every
           oat-milk ticket together before touching a dairy one rather than
           alternating.
        2. **On-spec before out-of-spec.** Within a milk group, a ticket whose dose is
           already on target is pulled ahead of one likely to bounce through
           `T_Rework_Dose` — a rush doesn't want to get stuck behind a slow ticket.

    Demonstrates:
        A **certified** per-token key. It reads only the token's own `payload` and
        `created_at` and closes over nothing mutable, so `cpnx.certification` proves
        it closed-world and the engine both (a) evaluates it inline rather than
        round-tripping it through the timeout-bounded expression pool, and (b) is
        willing to index it — an uncertified key cannot be indexed at all, because
        keying happens on the `deposit()` path, which cannot wait on an executor.

        Ties fall to `created_at`, and the engine breaks any remaining tie by
        insertion order, so the drain stays deterministic. Note this reorders the
        *groups*, not the tickets within them: every ticket is still consumed
        eventually, just not in strict arrival order.

        [`cafe.stations.specials_board`][cafe.stations.specials_board] holds the deliberately-uncertified twin of
        this function, for measuring what certification is worth.
    """
    return (
        0 if token.payload.get("dairy_free") else 1,
        0 if token.payload.get("weight_g", DOSE_TARGET_G) == DOSE_TARGET_G else 1,
        token.created_at,
    )
```

### serve_batch_triage

```
serve_batch_triage(tokens: list[Token]) -> list[Token]
```

**T_Batch_Triage_Serve**'s action: hand a triaged ticket straight out as a drink.

Demonstrates

Deliberate **minimalism as experimental hygiene**. It skips the grind/pull/steam machinery entirely, because this queue exists to exercise InputArc.key over a deep pool and nothing else — re-modelling the full pipeline a second time would put unrelated engine work in the measurement.

Source code in `benchmarks/cafe/stations/batch_triage.py`

```
def serve_batch_triage(tokens: list[Token]) -> list[Token]:
    """**T_Batch_Triage_Serve**'s action: hand a triaged ticket straight out as a drink.

    Demonstrates:
        Deliberate **minimalism as experimental hygiene**. It skips the
        grind/pull/steam machinery entirely, because this queue exists to exercise
        [`InputArc.key`][cpnx.InputArc] over a deep pool and nothing else — re-modelling the full
        pipeline a second time would put unrelated engine work in the measurement.
    """
    ticket = tokens[0]
    return [ticket.evolve(payload_updates={"stage": "drink"}, color="drink")]
```

### places

```
places() -> list[Place]
```

The backlog — an unbounded FIFO Place with `schema=is_order`, same shape as `P_Ticket_Line`.

Source code in `benchmarks/cafe/stations/batch_triage.py`

```
def places() -> list[Place]:
    """The backlog — an unbounded FIFO [`Place`][cpnx.Place] with `schema=is_order`, same shape as `P_Ticket_Line`."""
    return [Place("P_Batch_Triage_Queue", schema=is_order)]
```

### transitions

```
transitions(*, work_secs: float = 0.0) -> list[Transition]
```

**T_Batch_Triage_Serve** — pull the next ticket in triage order.

Demonstrates

A single keyed input arc and nothing else: no guard, no filter, default `LEGACY` policy, `count=1`. Under `LEGACY` the arc is read head-only, so the key index is asked for just `count` tokens — the cheapest possible read of a deep keyed place, and the one the throughput benchmark's key-index rows measure.

Parameters:

| Name        | Type    | Description                                     | Default |
| ----------- | ------- | ----------------------------------------------- | ------- |
| `work_secs` | `float` | Physical seconds the station occupies a worker. | `0.0`   |

Source code in `benchmarks/cafe/stations/batch_triage.py`

```
def transitions(*, work_secs: float = 0.0) -> list[Transition]:
    """**T_Batch_Triage_Serve** — pull the next ticket in triage order.

    Demonstrates:
        A single keyed input arc and nothing else: no guard, no filter, default
        `LEGACY` policy, `count=1`. Under `LEGACY` the arc is read head-only, so the
        key index is asked for just `count` tokens — the cheapest possible read of a
        deep keyed place, and the one the throughput benchmark's key-index rows
        measure.

    Args:
        work_secs: Physical seconds the station occupies a worker.
    """
    return [
        Transition(
            name="T_Batch_Triage_Serve",
            inputs=[InputArc("P_Batch_Triage_Queue", key=batch_triage_key)],
            outputs=[OutputArc("P_Served")],
            action=with_work(work_secs, serve_batch_triage),
            action_timeout_secs=0.5,
        )
    ]
```

### ☕ The decaf-only barista — `filter` without `key`

## cafe.stations.decaf

🫘 The decaf-only barista — a deep place drained through an InputArc.filter alone.

Cafe role

One barista works a side station that serves only decaf tickets, pulled off the same kind of deep backlog as the rush-hour rail. There is no batching heuristic here, no milk-clustering, no dose-spec preference — just eligibility. A ticket either says decaf or it doesn't, and the barista takes the first eligible one in arrival order.

Demonstrates

The **filter-only performance cliff**: an InputArc with a `filter` but no `key` never gets a key index, even when the filter is fully certified. In `engine._materialize_pool` the three routes are tried cheapest first — bounded FIFO peek requires no `key` *and* no `filter`; the key-index read requires `arc.key` to be set at all (`_ensure_key_index` returns `False` immediately when it is `None`, regardless of the filter's certification). A filter-only arc fails both, so it always lands on the third route: `place.peek(len(place))` followed by a per-firing filter-then-sort over the whole available marking, on **every enabling check** for this transition — including checks where it does not end up firing. Draining a place this way is O(N) per step, i.e. an O(N^2) drain overall.

The sharp point is that InputArc's own docs tell users to certify selection callables on a deep place, and that advice is only half true here. Certification rescues a *keyed* arc, because keying happens on the `deposit()` path and certified keys are what the persistent min-heap indexes — cafe.stations.batch_triage.batch_triage_key is that reproducer. Certifying a filter removes the executor round-trip per token, but it does not remove the O(N) scan: `_ensure_key_index` never even looks at `_filter_inline_safe` unless `arc.key` is already set. decaf_ticket below certifies cleanly and the cliff is still there.

The knob worth sweeping is **selectivity**, i.e. the decaf rate among the queue's tokens. At a 10% rate the filter still dispatches against all N tokens per check to find one ~10 deep; cost should come out flat across the rate and linear in N, and that flatness is the tell that the measured cost is the peek, not the predicate. Compare decaf rates of 0.5, 0.1, and 0.01 against a fixed depth to see it directly.

### decaf_ticket

```
decaf_ticket(token: Token) -> bool
```

InputArc.filter for the decaf line: is this ticket decaf?

Cafe role

The barista's only question. No ranking among decaf tickets — arrival order among the eligible ones is all that's left once ineligible tickets are excluded.

Demonstrates

A **certified** filter predicate: it reads only the token's own `payload` and closes over nothing mutable, so `cpnx.certification` proves it closed-world and the engine runs it inline rather than round-tripping it through the timeout-bounded expression pool. That certification pays off once per token dispatched — it does not change *how many* tokens get dispatched, which is the whole point of this station: see the module docstring for why a `key` is what would actually change that count, and this arc deliberately has none.

Source code in `benchmarks/cafe/stations/decaf.py`

```
def decaf_ticket(token: Token) -> bool:
    """[`InputArc.filter`][cpnx.InputArc] for the decaf line: is this ticket decaf?

    Cafe role:
        The barista's only question. No ranking among decaf tickets — arrival order
        among the eligible ones is all that's left once ineligible tickets are excluded.

    Demonstrates:
        A **certified** filter predicate: it reads only the token's own `payload` and
        closes over nothing mutable, so `cpnx.certification` proves it closed-world and
        the engine runs it inline rather than round-tripping it through the
        timeout-bounded expression pool. That certification pays off once per token
        dispatched — it does not change *how many* tokens get dispatched, which is the
        whole point of this station: see the module docstring for why a `key` is what
        would actually change that count, and this arc deliberately has none.
    """
    return bool(token.payload.get("decaf"))
```

### serve_decaf

```
serve_decaf(tokens: list[Token]) -> list[Token]
```

**T_Decaf_Pull**'s action: hand a decaf ticket out as a served drink.

Demonstrates

The same deliberate **minimalism as experimental hygiene** used by cafe.stations.batch_triage.serve_batch_triage — no grind/pull/steam machinery, so the only engine work this station's benchmark can be measuring is the arc's own selection cost.

Source code in `benchmarks/cafe/stations/decaf.py`

```
def serve_decaf(tokens: list[Token]) -> list[Token]:
    """**T_Decaf_Pull**'s action: hand a decaf ticket out as a served drink.

    Demonstrates:
        The same deliberate **minimalism as experimental hygiene** used by
        [`cafe.stations.batch_triage.serve_batch_triage`][cafe.stations.batch_triage.serve_batch_triage] — no
        grind/pull/steam machinery, so the only engine work this station's benchmark can be measuring
        is the arc's own selection cost.
    """
    ticket = tokens[0]
    return [ticket.evolve(payload_updates={"stage": "drink"}, color="drink")]
```

### places

```
places() -> list[Place]
```

The decaf backlog — a plain unbounded FIFO Place, holding both decaf and non-decaf tickets so the filter has something to exclude.

Demonstrates

The **shared-pool shape** the filter-only cliff needs: eligibility narrows the pool *within* the place rather than the place being pre-sorted, which is exactly what forces route 3's full-marking peek in `engine._materialize_pool`.

Source code in `benchmarks/cafe/stations/decaf.py`

```
def places() -> list[Place]:
    """The decaf backlog — a plain unbounded FIFO [`Place`][cpnx.Place], holding both decaf and non-decaf
    tickets so the filter has something to exclude.

    Demonstrates:
        The **shared-pool shape** the filter-only cliff needs: eligibility narrows the
        pool *within* the place rather than the place being pre-sorted, which is exactly
        what forces route 3's full-marking peek in `engine._materialize_pool`.
    """
    return [Place("P_Decaf_Line", schema=has_payload)]
```

### transitions

```
transitions(*, work_secs: float = 0.0) -> list[Transition]
```

**T_Decaf_Pull** — pull the next decaf ticket, in plain arrival order among decaf ones.

Demonstrates

A single input arc with a certified `filter` and **no `key`**, `count=1`, default `LEGACY` policy — the minimal shape that reproduces the filter-only cliff described in the module docstring. Nothing else on the transition (no guard, no `binding_priority_key`) so the cost measured is attributable to the arc alone.

Parameters:

| Name        | Type    | Description                                     | Default |
| ----------- | ------- | ----------------------------------------------- | ------- |
| `work_secs` | `float` | Physical seconds the station occupies a worker. | `0.0`   |

Source code in `benchmarks/cafe/stations/decaf.py`

```
def transitions(*, work_secs: float = 0.0) -> list[Transition]:
    """**T_Decaf_Pull** — pull the next decaf ticket, in plain arrival order among decaf ones.

    Demonstrates:
        A single input arc with a certified `filter` and **no `key`**, `count=1`, default
        `LEGACY` policy — the minimal shape that reproduces the filter-only cliff
        described in the module docstring. Nothing else on the transition (no guard, no
        `binding_priority_key`) so the cost measured is attributable to the arc alone.

    Args:
        work_secs: Physical seconds the station occupies a worker.
    """
    return [
        Transition(
            name="T_Decaf_Pull",
            inputs=[InputArc("P_Decaf_Line", filter=decaf_ticket, count=1)],
            outputs=[OutputArc("P_Served")],
            action=with_work(work_secs, serve_decaf),
            action_timeout_secs=0.5,
        )
    ]
```

### 🥁 The knock box — `consume_all` on a deep place

## cafe.stations.knock_box

🥁 The knock box — a `consume_all` drain gated behind a guard that is usually `False`.

Cafe role

Every spent puck gets knocked out into a bin under the bar. The bin fills all through the rush — nobody stops to empty it between orders — and only in a lull, with a group head free, does the barista pick it up and empty the whole thing into the trash in one motion.

Demonstrates

The **pathological interaction between `consume_all` and a guard**. A `consume_all` arc always takes route 3 of `engine._materialize_pool` — `place.peek(len(place))` — because routes 1 and 2 both require `not arc.consume_all`. That alone just makes the read O(marking depth) instead of O(1)/O(log N). The trap is *when* it pays that cost: `_is_transition_enabled` resolves a binding (which gathers every arc's pool, including this one's full-place peek) *before* it evaluates the guard, not after. So on every single `step()` while the lull guard is `False`, the engine still peeks the entire knock box, builds the binding, and only then discards it because the guard said no.

The consequence worth stating plainly: **the less often this transition fires, the more it costs.** A knock box that is emptied every few orders is scanned shallow, over and over. One that is emptied only during rare lulls is scanned at its deepest, over and over — the guard's whole job is to make firing rare, and rarity is exactly what makes each rejected check expensive. Nothing about a low firing rate makes this station cheap; it makes it expensive more often per unit of useful work.

This station deliberately does **not** trigger the documented `consume_all` footgun — draining ignores `key`/`filter` and a `UserWarning` fires if either is set (see the `Warning` block on InputArc) — because neither is set here. Worth noting anyway: "drain only the eligible pucks" is not a supported combination with `consume_all`; the workaround the docs point to is a large `count` instead of `consume_all`, which this station does not need since it always wants everything in the bin.

Sweep **lull frequency** (`min_pucks`, larger = rarer firings) crossed with **knock-box depth** (how many pucks are stocked before the run) — cost per rejected check should scale with depth, and total cost should climb as `min_pucks` rises even though fewer firings occur.

### make_lull_guard

```
make_lull_guard(
    min_pucks: int,
) -> Callable[[list[Token]], bool]
```

Build **T_Empty_Knock_Box**'s guard: only empty the bin once it is worth the trip.

Cafe role

A barista doesn't stoop to empty the knock box for two pucks — that's a lull worth spending on, not a rush-hour interruption. The guard stands in for "things have quieted down enough that this is worth doing now," which in a real rush is true rarely and for the rest of the time is false.

Demonstrates

A **certified guard factory**: `_dense_enough` closes over `min_pucks` — an immutable `int` captured at construction — and nothing else, so `cpnx.certification` proves it closed-world and the engine evaluates it inline under the lock rather than round-tripping it through the timeout-bounded executor. It is legitimate here to count the *whole* bound token list rather than inspect one token, because `consume_all=True` on the knock-box arc guarantees the binding already contains every available puck — there is no partial view to worry about, unlike a guard written against an arc with `count` set to something less than the pool.

The bound list also carries the `P_Espresso_Machine` permit token, so the count excludes resource tokens via Token.is_resource — only spent pucks count toward the threshold.

Parameters:

| Name        | Type  | Description                                                                                                                                                                                                       | Default    |
| ----------- | ----- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------- |
| `min_pucks` | `int` | Minimum number of pucks that must be in the bin for the guard to allow firing. This is the lull-frequency knob: a higher value makes firing rarer, and rarity is exactly what this station's cost model punishes. | *required* |

Returns:

| Type                            | Description                                                              |
| ------------------------------- | ------------------------------------------------------------------------ |
| `Callable[[list[Token]], bool]` | A guard Callable\[\[list[Token]\], bool\] suitable for Transition.guard. |

Source code in `benchmarks/cafe/stations/knock_box.py`

```
def make_lull_guard(min_pucks: int) -> Callable[[list[Token]], bool]:
    """Build **T_Empty_Knock_Box**'s guard: only empty the bin once it is worth the trip.

    Cafe role:
        A barista doesn't stoop to empty the knock box for two pucks — that's a lull
        worth spending on, not a rush-hour interruption. The guard stands in for "things
        have quieted down enough that this is worth doing now," which in a real rush is
        true rarely and for the rest of the time is false.

    Demonstrates:
        A **certified guard factory**: `_dense_enough` closes over `min_pucks` — an
        immutable `int` captured at construction — and nothing else, so
        `cpnx.certification` proves it closed-world and the engine evaluates it inline
        under the lock rather than round-tripping it through the timeout-bounded
        executor. It is legitimate here to count the *whole* bound token list rather than
        inspect one token, because `consume_all=True` on the knock-box arc guarantees the
        binding already contains every available puck — there is no partial view to worry
        about, unlike a guard written against an arc with `count` set to something less
        than the pool.

        The bound list also carries the `P_Espresso_Machine` permit token, so the count
        excludes resource tokens via [`Token.is_resource`][cpnx.Token] — only spent pucks count toward
        the threshold.

    Args:
        min_pucks: Minimum number of pucks that must be in the bin for the guard to
            allow firing. This is the lull-frequency knob: a higher value makes firing
            rarer, and rarity is exactly what this station's cost model punishes.

    Returns:
        A guard `Callable[[list[Token]], bool]` suitable for [`Transition.guard`][cpnx.Transition].
    """

    def _dense_enough(tokens: list[Token]) -> bool:
        pucks = [t for t in tokens if not t.is_resource]
        return len(pucks) >= min_pucks

    return _dense_enough
```

### empty_knock_box

```
empty_knock_box(tokens: list[Token]) -> list[Token]
```

**T_Empty_Knock_Box**'s action: tip the whole bin into the trash.

Cafe role

One motion, whatever is in the bin — there is no sorting or salvaging spent pucks, so the action just forwards every consumed puck token straight through to `P_Trash_Can` unchanged (aside from the resource permit, which is excluded here and released back to `P_Espresso_Machine` by the engine's own resource-arc bookkeeping, not by this action).

Demonstrates

The same **minimalism as experimental hygiene** used throughout this fixture (see cafe.stations.batch_triage.serve_batch_triage): the action does no work that isn't the point of the station, so the benchmark cost is attributable to the arc/guard interaction described in the module docstring, not to the action body.

Source code in `benchmarks/cafe/stations/knock_box.py`

```
def empty_knock_box(tokens: list[Token]) -> list[Token]:
    """**T_Empty_Knock_Box**'s action: tip the whole bin into the trash.

    Cafe role:
        One motion, whatever is in the bin — there is no sorting or salvaging spent
        pucks, so the action just forwards every consumed puck token straight through to
        `P_Trash_Can` unchanged (aside from the resource permit, which is excluded here
        and released back to `P_Espresso_Machine` by the engine's own resource-arc
        bookkeeping, not by this action).

    Demonstrates:
        The same **minimalism as experimental hygiene** used throughout this fixture
        (see [`cafe.stations.batch_triage.serve_batch_triage`][cafe.stations.batch_triage.serve_batch_triage]): the
        action does no work that isn't the point of the station, so the benchmark cost is attributable to the
        arc/guard interaction described in the module docstring, not to the action body.
    """
    return [t for t in tokens if not t.is_resource]
```

### places

```
places() -> list[Place]
```

The bin — a plain unbounded Place that accumulates spent pucks all through the rush.

Demonstrates

The **deep, ungated accumulator** this station's guard is built to stall against. Nothing here caps how deep the bin gets between lulls; depth is entirely a function of how long the benchmark lets the rush run before the guard admits a lull, which is what makes it a controllable experimental knob rather than a fixed property of the fixture.

Source code in `benchmarks/cafe/stations/knock_box.py`

```
def places() -> list[Place]:
    """The bin — a plain unbounded [`Place`][cpnx.Place] that accumulates spent pucks all through the
    rush.

    Demonstrates:
        The **deep, ungated accumulator** this station's guard is built to stall against.
        Nothing here caps how deep the bin gets between lulls; depth is entirely a
        function of how long the benchmark lets the rush run before the guard admits a
        lull, which is what makes it a controllable experimental knob rather than a fixed
        property of the fixture.
    """
    return [Place("P_Knock_Box", schema=has_payload)]
```

### transitions

```
transitions(
    *, work_secs: float = 0.0, min_pucks: int = 25
) -> list[Transition]
```

**T_Empty_Knock_Box** — drain the bin in one atomic motion, but only during a lull.

Demonstrates

The full pathological combination in one transition: a `consume_all=True` input arc (forcing the O(marking-depth) full-place peek on every enabling check) paired with a `guard` that is `False` most of the time (so most of those peeks are thrown away unused). The second input arc, a permit on `P_Espresso_Machine`, models "a group head is free" — the barista needs both a full bin and a spare hand before emptying it. See the module docstring for why this makes rarer firings *more* expensive overall, not less.

Parameters:

| Name        | Type    | Description                                                                                      | Default |
| ----------- | ------- | ------------------------------------------------------------------------------------------------ | ------- |
| `work_secs` | `float` | Physical seconds the station occupies a worker.                                                  | `0.0`   |
| `min_pucks` | `int`   | Minimum bin depth before the lull guard allows firing — the lull frequency knob. Defaults to 25. | `25`    |

Source code in `benchmarks/cafe/stations/knock_box.py`

```
def transitions(*, work_secs: float = 0.0, min_pucks: int = 25) -> list[Transition]:
    """**T_Empty_Knock_Box** — drain the bin in one atomic motion, but only during a lull.

    Demonstrates:
        The full pathological combination in one transition: a `consume_all=True` input
        arc (forcing the O(marking-depth) full-place peek on every enabling check) paired
        with a `guard` that is `False` most of the time (so most of those peeks are
        thrown away unused). The second input arc, a permit on `P_Espresso_Machine`,
        models "a group head is free" — the barista needs both a full bin and a spare
        hand before emptying it. See the module docstring for why this makes rarer
        firings *more* expensive overall, not less.

    Args:
        work_secs: Physical seconds the station occupies a worker.
        min_pucks: Minimum bin depth before the lull guard allows firing — the lull
            frequency knob. Defaults to 25.
    """
    return [
        Transition(
            name="T_Empty_Knock_Box",
            inputs=[
                InputArc("P_Knock_Box", consume_all=True),
                InputArc("P_Espresso_Machine", count=1),
            ],
            outputs=[OutputArc("P_Trash_Can")],
            guard=make_lull_guard(min_pucks),
            action=with_work(work_secs, empty_knock_box),
            action_timeout_secs=0.5,
        )
    ]
```

### 🧾 The specials board — an uncertified `key`

## cafe.stations.specials_board

🧾 The specials board — an InputArc.key that cannot be certified, and therefore cannot be indexed.

Cafe role

Mid-shift the lead retunes the priorities on a whiteboard behind the bar: today oat goes first, tomorrow maybe not. The barista does not memorise a fixed rule — every time they reach for the next ticket they glance up at the board and read whatever is written there right now.

Demonstrates

The **uncertified InputArc.key path**, deliberately paired against cafe.stations.batch_triage as an A/B partner: same ordering, same single-arc topology, differing only in whether that ordering closes over mutable state. batch_triage_key reads nothing but the token; specials_board_key reads a module-level mutable dict, which `cpnx.certification` rejects as closed-world. That one difference changes everything about how the engine drains the queue:

1. **No index, ever.** `_ensure_key_index` (`cpnx.engine`, ~1143-1170) refuses to build a key index unless `arc._key_inline_safe`. This is not a missed optimisation to fix later -- it is structurally impossible. Keying happens on the **deposit path**, and `deposit()` cannot block a producer waiting on the timeout-bounded expression executor just to place one token. So an uncertified key always falls to `_materialize_pool`'s route 3: `place.peek(len(place))` followed by a fresh filter-then-sort in `_order_available` (~1431) on *every single firing*. Draining a deep place this way is ≈O(N² log N), against ≈O(N log N) for the certified twin's persistent `(key, seq)` heap.
1. **A per-token round trip.** Because the key is uncertified, each token's key is not computed inline -- it goes through `_call_expr`, one `ThreadPoolExecutor.submit` + `.result(timeout=...)` round trip *per token in the pool*, all while the engine's global lock is held. That round trip runs ~10 microseconds against a predicate that is itself ~0.09 microseconds -- dispatch, not computation, dominates.

Note what does *not* bound this cost: `binding_search_limit` truncates the number of *candidate bindings* built in `_iter_candidate_bindings`, which only runs after this arc's per-token loop has already finished. Nothing bounds the per-token count, so the worst-case lock-held time for one firing of this arc is `len(place) * expr_timeout_secs` -- the whole pool, each token individually timeout-eligible.

Because a per-token lock-held round trip is a far worse contention shape than a guard's per-*candidate* round trip (see `bench_enablement.py`), this station is also the right regime to drive through `bench_cafe_concurrency.py`: it stresses the engine lock under concurrent producers/consumers in a way `batch_triage`'s certified twin structurally cannot.

### specials_board_key

```
specials_board_key(token: Token) -> tuple[int, int, float]
```

InputArc.key for the specials queue: batch_triage_key's ordering, read off a whiteboard.

Cafe role

Computes the identical two-tier grouping as `batch_triage.batch_triage_key` -- oat before dairy, on-spec before out-of-spec, ties broken by arrival time -- but instead of hardcoding which side of each grouping sorts first, it looks up `_SPECIALS_BOARD` each time. The board can be repainted between firings (a real shift lead would), and the very next ticket read honours the new priorities immediately.

Demonstrates

An **uncertified** per-token key: it reads only the token's own `payload` and `created_at`, so `verify_callable_purity` still passes (no I/O), but it also reads a module-level *mutable* dict, so `cpnx.certification.is_inline_safe` returns `False`. That single distinction is the entire experiment -- see the module docstring for what it costs.

Parameters:

| Name    | Type    | Description                                             | Default    |
| ------- | ------- | ------------------------------------------------------- | ---------- |
| `token` | `Token` | The candidate ticket, as deposited on P_Specials_Queue. | *required* |

Returns:

| Type    | Description                                                                        |
| ------- | ---------------------------------------------------------------------------------- |
| `int`   | A 3-tuple (milk_priority_group, spec_priority_group, created_at) sorted ascending, |
| `int`   | identical in shape (and, for the default board, in value) to                       |
| `float` | batch_triage_key's return.                                                         |

Source code in `benchmarks/cafe/stations/specials_board.py`

```
def specials_board_key(token: Token) -> tuple[int, int, float]:
    """[`InputArc.key`][cpnx.InputArc] for the specials queue:
    [`batch_triage_key`][cafe.stations.batch_triage.batch_triage_key]'s ordering, read off a whiteboard.

    Cafe role:
        Computes the identical two-tier grouping as `batch_triage.batch_triage_key` -- oat
        before dairy, on-spec before out-of-spec, ties broken by arrival time -- but instead of
        hardcoding which side of each grouping sorts first, it looks up `_SPECIALS_BOARD` each
        time. The board can be repainted between firings (a real shift lead would), and the
        very next ticket read honours the new priorities immediately.

    Demonstrates:
        An **uncertified** per-token key: it reads only the token's own `payload` and
        `created_at`, so `verify_callable_purity` still passes (no I/O), but it also reads a
        module-level *mutable* dict, so `cpnx.certification.is_inline_safe` returns `False`.
        That single distinction is the entire experiment -- see the module docstring for what
        it costs.

    Args:
        token: The candidate ticket, as deposited on `P_Specials_Queue`.

    Returns:
        A 3-tuple ``(milk_priority_group, spec_priority_group, created_at)`` sorted ascending,
        identical in shape (and, for the default board, in value) to
        [`batch_triage_key`][cafe.stations.batch_triage.batch_triage_key]'s return.
    """
    dairy_free = bool(token.payload.get("dairy_free"))
    on_spec = token.payload.get("weight_g", DOSE_TARGET_G) == DOSE_TARGET_G
    milk_group = 0 if dairy_free else 1
    spec_group = 0 if on_spec else 1
    # XOR against the board's priority bits: priority 0 keeps the natural (0-first) ordering,
    # priority 1 flips which side of the grouping sorts first -- so the board can retune
    # priorities without this function ever needing an `if/else` per knob.
    return (
        milk_group ^ _SPECIALS_BOARD["milk_priority"],
        spec_group ^ _SPECIALS_BOARD["spec_priority"],
        token.created_at,
    )
```

### serve_specials_board

```
serve_specials_board(tokens: list[Token]) -> list[Token]
```

**T_Specials_Serve**'s action: hand the board's next pick straight out as a drink.

Demonstrates

The same deliberate minimalism as `batch_triage.serve_batch_triage` -- no grind/pull/steam machinery -- so this station's measurements stay isolated to the uncertified-key dispatch path, not diluted by unrelated pipeline work.

Source code in `benchmarks/cafe/stations/specials_board.py`

```
def serve_specials_board(tokens: list[Token]) -> list[Token]:
    """**T_Specials_Serve**'s action: hand the board's next pick straight out as a drink.

    Demonstrates:
        The same deliberate minimalism as `batch_triage.serve_batch_triage` -- no
        grind/pull/steam machinery -- so this station's measurements stay isolated to the
        uncertified-key dispatch path, not diluted by unrelated pipeline work.
    """
    ticket = tokens[0]
    return [ticket.evolve(payload_updates={"stage": "drink"}, color="drink")]
```

### places

```
places() -> list[Place]
```

The whiteboard queue -- an unbounded FIFO Place, same shape as `P_Batch_Triage_Queue`.

Cafe role

`P_Specials_Queue` holds the tickets waiting on the specials board's current priorities; nothing about the place itself differs from a plain ticket rail.

Demonstrates

Structural symmetry with `batch_triage.places`: this station's cost lives entirely in how its arc's key is dispatched, not in any special place behaviour.

Args:

Source code in `benchmarks/cafe/stations/specials_board.py`

```
def places() -> list[Place]:
    """The whiteboard queue -- an unbounded FIFO [`Place`][cpnx.Place], same shape as `P_Batch_Triage_Queue`.

    Cafe role:
        `P_Specials_Queue` holds the tickets waiting on the specials board's current
        priorities; nothing about the place itself differs from a plain ticket rail.

    Demonstrates:
        Structural symmetry with `batch_triage.places`: this station's cost lives entirely in
        how its arc's key is dispatched, not in any special place behaviour.

    Args:
    """
    return [Place("P_Specials_Queue", schema=has_payload)]
```

### transitions

```
transitions(*, work_secs: float = 0.0) -> list[Transition]
```

**T_Specials_Serve** -- pull the board's next pick, one uncertified-key round trip at a time.

Cafe role

The barista reads the specials board and pulls the next ticket it names.

Demonstrates

A single input arc whose `key` is specials_board_key -- uncertified, so `_ensure_key_index` never indexes `P_Specials_Queue` and every firing re-materialises and re-sorts the whole available pool via `_order_available`, with each token's key dispatched through the timeout-bounded expression executor rather than evaluated inline. No guard, no filter, default `LEGACY` policy, `count=1` -- identical shape to `T_Batch_Triage_Serve`, so the only variable between the two stations is certification.

Parameters:

| Name        | Type    | Description                                     | Default |
| ----------- | ------- | ----------------------------------------------- | ------- |
| `work_secs` | `float` | Physical seconds the station occupies a worker. | `0.0`   |

Source code in `benchmarks/cafe/stations/specials_board.py`

```
def transitions(*, work_secs: float = 0.0) -> list[Transition]:
    """**T_Specials_Serve** -- pull the board's next pick, one uncertified-key round trip at a time.

    Cafe role:
        The barista reads the specials board and pulls the next ticket it names.

    Demonstrates:
        A single input arc whose `key` is [`specials_board_key`][cafe.stations.specials_board.specials_board_key] --
        uncertified, so `_ensure_key_index` never indexes `P_Specials_Queue` and every firing re-materialises
        and re-sorts the whole available pool via `_order_available`, with each token's key
        dispatched through the timeout-bounded expression executor rather than evaluated
        inline. No guard, no filter, default `LEGACY` policy, `count=1` -- identical shape to
        `T_Batch_Triage_Serve`, so the only variable between the two stations is certification.

    Args:
        work_secs: Physical seconds the station occupies a worker.
    """
    return [
        Transition(
            name="T_Specials_Serve",
            inputs=[InputArc("P_Specials_Queue", key=specials_board_key)],
            outputs=[OutputArc("P_Served")],
            action=with_work(work_secs, serve_specials_board),
            action_timeout_secs=0.5,
        )
    ]
```

### 🚫 The 86 board — a certified `key` behind an uncertified `filter`

## cafe.stations.eighty_six

🚫 The 86 board — a certified InputArc.key paired with an uncertified InputArc.filter.

Cafe role

"86 the lavender" — a syrup runs out mid-shift and goes up on the 86 board, so any ticket calling for it can't be made until the syrup is restocked. The board changes through the day as things sell out and get replenished, so nothing in the net can treat it as fixed at construction time.

Demonstrates

The **asymmetry in `PetriNet._ensure_key_index`**: certifying an arc's `key` buys nothing if the same arc's `filter` is uncertified. Indexing requires *both* callables to be certified, so one uncertified filter disqualifies the whole arc from the key-index path and it falls back to `_materialize_pool` route 3 — a full peek of every token in the place, followed by a per-firing filter-then-sort. That is exactly the cost profile the key would have had if it had never been certified at all.

Crucially this is **not a dispatch-cost decision, it is a correctness one**. Read `PetriNet._ensure_key_index`'s docstring: a capped index read returns only the leading `cap` tokens in key order. Applying an uncertified filter *after* that read would be wrong — if the filter rejects every one of those `cap` tokens while an eligible token sits deeper in the index, the arc would silently under-select and the transition would report itself disabled even though a valid binding exists. There are only two correct arrangements: the filter runs at pop time so the scan can continue past rejected tokens (which requires the filter to be inline-safe), or the index is not consulted at all. There is no third option that lets a certified key partially help — which is why this station's `filter` disqualifies the arc even though its `key` alone would qualify.

The user-facing shape of this bug is "I certified my key and got no speedup", and this station is that report's minimal reproducer. Its A/B partner is cafe.stations.batch_triage, which reuses the exact same key with no filter at all and *does* get indexed. Note also the selectivity interaction: because a rejected token stays in the index and the scan simply continues past it, a highly selective filter degrades this arc toward a full ordered walk of the place on every firing — so a *long* 86 board is worse for this station than a short one, on top of the baseline cost of not being indexed at all.

### not_86ed

```
not_86ed(token: Token) -> bool
```

InputArc.filter for the 86 board queue: is this ticket's syrup still in stock?

Cafe role

Rejects any ticket calling for a syrup currently up on the 86 board. The board is mutable through the shift — restocking a syrup should immediately let its tickets flow again, without rebuilding the net.

Demonstrates

The **uncertified** half of this station's key/filter pair. It reads the module-level mutable `_EIGHTY_SIX_BOARD` set, which is exactly the kind of external mutable state `cpnx.certification` refuses to certify — so this function passes `verify_callable_purity` (it performs no I/O, so construction succeeds) but fails certification (so `PetriNet._ensure_key_index` cannot use it). Pairing it with the certified batch_triage_key on the same arc is what proves a certified key alone cannot rescue an arc from an uncertified filter.

Source code in `benchmarks/cafe/stations/eighty_six.py`

```
def not_86ed(token: Token) -> bool:
    """[`InputArc.filter`][cpnx.InputArc] for the 86 board queue: is this ticket's syrup still in stock?

    Cafe role:
        Rejects any ticket calling for a syrup currently up on the 86 board. The board
        is mutable through the shift — restocking a syrup should immediately let its
        tickets flow again, without rebuilding the net.

    Demonstrates:
        The **uncertified** half of this station's key/filter pair. It reads the
        module-level mutable `_EIGHTY_SIX_BOARD` set, which is exactly the kind of
        external mutable state `cpnx.certification` refuses to certify — so this
        function passes `verify_callable_purity` (it performs no I/O, so construction
        succeeds) but fails certification (so `PetriNet._ensure_key_index` cannot use
        it). Pairing it with the certified [`batch_triage_key`][cafe.stations.batch_triage.batch_triage_key] on the
        same arc is what proves a certified key alone cannot rescue an arc from an uncertified filter.
    """
    return token.payload.get("syrup") not in _EIGHTY_SIX_BOARD
```

### serve_eighty_six

```
serve_eighty_six(tokens: list[Token]) -> list[Token]
```

**T_Eighty_Six_Serve**'s action: hand an in-stock ticket straight out as a drink.

Demonstrates

Minimal action, matching `batch_triage.serve_batch_triage`. This station exists to exercise arc-level indexing eligibility, not action machinery, so the action does the least possible work beyond marking the ticket served.

Source code in `benchmarks/cafe/stations/eighty_six.py`

```
def serve_eighty_six(tokens: list[Token]) -> list[Token]:
    """**T_Eighty_Six_Serve**'s action: hand an in-stock ticket straight out as a drink.

    Demonstrates:
        Minimal action, matching `batch_triage.serve_batch_triage`. This station exists
        to exercise arc-level indexing eligibility, not action machinery, so the action
        does the least possible work beyond marking the ticket served.
    """
    ticket = tokens[0]
    return [ticket.evolve(payload_updates={"stage": "drink"}, color="drink")]
```

### places

```
places() -> list[Place]
```

The 86-board queue — an unbounded FIFO Place, same shape as `P_Batch_Triage_Queue`.

Cafe role

Holds tickets waiting on whatever syrup they need, regardless of whether that syrup is currently 86'd — the filter, not the place, is what withholds them.

Source code in `benchmarks/cafe/stations/eighty_six.py`

```
def places() -> list[Place]:
    """The 86-board queue — an unbounded FIFO [`Place`][cpnx.Place], same shape as `P_Batch_Triage_Queue`.

    Cafe role:
        Holds tickets waiting on whatever syrup they need, regardless of whether that
        syrup is currently 86'd — the filter, not the place, is what withholds them.
    """
    return [Place("P_Eighty_Six_Queue", schema=has_payload)]
```

### transitions

```
transitions(*, work_secs: float = 0.0) -> list[Transition]
```

**T_Eighty_Six_Serve** — pull the next in-stock ticket in triage order.

Cafe role

Serves tickets in the same oat-before-dairy, on-spec-before-out-of-spec order as `T_Batch_Triage_Serve`, but skips anything currently 86'd.

Demonstrates

A single input arc carrying both a certified `key` (batch_triage_key, reused unchanged from cafe.stations.batch_triage) and an uncertified `filter` (not_86ed). That combination is this station's whole point: see the module docstring for why the certified key cannot rescue the arc from the uncertified filter, and why that is a correctness requirement rather than a missed optimization.

Parameters:

| Name        | Type    | Description                                     | Default |
| ----------- | ------- | ----------------------------------------------- | ------- |
| `work_secs` | `float` | Physical seconds the station occupies a worker. | `0.0`   |

Source code in `benchmarks/cafe/stations/eighty_six.py`

```
def transitions(*, work_secs: float = 0.0) -> list[Transition]:
    """**T_Eighty_Six_Serve** — pull the next in-stock ticket in triage order.

    Cafe role:
        Serves tickets in the same oat-before-dairy, on-spec-before-out-of-spec order
        as `T_Batch_Triage_Serve`, but skips anything currently 86'd.

    Demonstrates:
        A single input arc carrying both a certified `key`
        ([`batch_triage_key`][cafe.stations.batch_triage.batch_triage_key], reused unchanged from
        [`cafe.stations.batch_triage`][cafe.stations.batch_triage]) and an uncertified `filter`
        ([`not_86ed`][cafe.stations.eighty_six.not_86ed]). That combination is this station's whole point: see the
        module docstring for why the certified key cannot rescue the arc from the uncertified filter, and why that is a
        correctness requirement rather than a missed optimization.

    Args:
        work_secs: Physical seconds the station occupies a worker.
    """
    return [
        Transition(
            name="T_Eighty_Six_Serve",
            inputs=[InputArc("P_Eighty_Six_Queue", key=batch_triage_key, filter=not_86ed)],
            outputs=[OutputArc("P_Served")],
            action=with_work(work_secs, serve_eighty_six),
            action_timeout_secs=0.5,
        )
    ]
```

### 🥄 The cupping table — `count > 1` and the candidate space

## cafe.stations.cupping

🥄 The cupping table — a certified keyed arc at `count > 1`, searched under `PRIORITY`.

Cafe role

Green-coffee samples pile up on a bench for quality control. The roaster cups them in *flights* — several cups tasted side by side in one sitting, scored together against each other rather than one at a time. A flight is only meaningful if the cups are comparable, so every cup in a flight must share an origin; cupping an Ethiopian sample next to a Colombian one tells the roaster nothing. Within a flight, higher-scoring roasts and older samples (waiting longest for a verdict) are preferred.

Demonstrates

**This station is not about the `_materialize_pool` route-3 fallback** — cafe.stations.specials_board and cafe.stations.decaf already cover that ground, and this station's key is fully certified, so it never goes near route 3. cup_score is pure and closes over nothing mutable, so `_ensure_key_index` (`cpnx.engine`) happily builds the persistent `(key, seq)` heap for it even at `count=4` — the token *pool* read off that heap stays bounded to `binding_search_limit + arc.count`, exactly as `_gather_arc_pools` documents. Reading that cap short would silently truncate the candidate set, not merely cost time; this station's whole point is showing what still happens even when it is read correctly.

What actually blows up here is the **candidate space**, not the pool scan. `_arc_options` (`cpnx.engine`) yields every `count`-sized combination of the ordered pool — `C(pool, count)` groups — and `_iter_candidate_bindings` truncates each arc's option stream to `binding_search_limit + 1` groups before handing it to `itertools.product`. At `count=1` the arc yields one option per token, so that cap collapses to the familiar `limit + 1`. At `count=4` the `(limit + 1)`-th combination reaches all the way to index `limit + count - 1` in the pool ordering — which is exactly why `_gather_arc_pools` caps the pool read at `binding_search_limit + arc.count` rather than `binding_search_limit + 1`: the candidate truncation reaches further into the pool than the combination count alone would suggest.

same_origin is evaluated once per *candidate binding*, so raising `count` multiplies how many guard evaluations sit behind a single firing — `C(pool, count)` of them, capped by the search limit. `binding_policy=BindingPolicy.PRIORITY` forces the engine to actually enumerate that space (searching for the min-cup_score satisfying flight) rather than accepting the head group the way `LEGACY`/guard-free `FIRST` would. If the bench holds mixed origins and every satisfying combination happens to live beyond the first `binding_search_limit + 1` candidates the truncated prefix covers, the transition reads as **disabled** for that check — a stall reachable purely from candidate-space truncation, with a correctly-sized, correctly-indexed pool sitting right there un-scanned past the prefix. `on_binding_search_exhausted` is the signal to watch for it.

Recommend sweeping `count` in `{1, 2, 4, 8}` against several queue depths, watching where `on_binding_search_exhausted` starts firing and where flights stop forming despite a valid same-origin flight existing deeper in the bench than the search looked.

### cup_score

```
cup_score(token: Token) -> tuple[float, float]
```

InputArc.key for the cupping bench: which sample gets tasted next.

Cafe role

Higher roast scores earn a slot first — the roaster wants strong candidates confirmed early in the session, while the palate is freshest. Among samples scoring equally, the one that has waited longest on the bench goes first, so nothing sits indefinitely while newer arrivals keep cutting the line.

Demonstrates

A **certified per-token key** used at `count > 1`. It reads only the token's own `payload` and `created_at` and closes over nothing mutable, so `cpnx.certification` proves it closed-world and `_ensure_key_index` (`cpnx.engine`) indexes it with the persistent `(key, seq)` min-heap — the same fast path cafe.stations.batch_triage.batch_triage_key demonstrates at `count=1`. This module exists to show that certification alone does not make a keyed, guarded, `count > 1` search cheap: the pool this key orders is bounded and cheap to read, but the *combinations* `_arc_options` builds over that ordered pool are not.

Source code in `benchmarks/cafe/stations/cupping.py`

```
def cup_score(token: Token) -> tuple[float, float]:
    """[`InputArc.key`][cpnx.InputArc] for the cupping bench: which sample gets tasted next.

    Cafe role:
        Higher roast scores earn a slot first — the roaster wants strong candidates
        confirmed early in the session, while the palate is freshest. Among samples
        scoring equally, the one that has waited longest on the bench goes first, so
        nothing sits indefinitely while newer arrivals keep cutting the line.

    Demonstrates:
        A **certified per-token key** used at `count > 1`. It reads only the token's own
        `payload` and `created_at` and closes over nothing mutable, so
        `cpnx.certification` proves it closed-world and `_ensure_key_index` (`cpnx.engine`)
        indexes it with the persistent `(key, seq)` min-heap — the same fast path
        [`cafe.stations.batch_triage.batch_triage_key`][cafe.stations.batch_triage.batch_triage_key] demonstrates at
        `count=1`. This module exists to show that certification alone does not make a keyed, guarded,
        `count > 1` search cheap: the pool this key orders is bounded and cheap to read,
        but the *combinations* `_arc_options` builds over that ordered pool are not.
    """
    return (-token.payload.get("roast_score", 0.0), token.created_at)
```

### same_origin

```
same_origin(tokens: list[Token]) -> bool
```

**T_Cupping_Flight**'s guard: every cup in the flight must be comparable.

Cafe role

Cupping is a side-by-side comparison. Scoring an Ethiopian sample against a Colombian one in the same flight produces a meaningless number — the guard is the roaster's rule that a flight only forms when every cup on the tray shares a single-origin lot.

Demonstrates

A **certified guard evaluated per candidate binding**, under BindingPolicy.PRIORITY. It reads only each bound token's `payload["origin"]`, so `cpnx.certification` proves it closed-world and the engine runs it inline under the lock rather than round-tripping it through the timeout-bounded executor. What it costs is not the per-evaluation price — it is *how many* evaluations happen: one per candidate `count`-sized combination `_iter_candidate_bindings` yields, up to `binding_search_limit + 1` of them. A bench with several origins mixed together forces the search to actually walk combinations looking for one that satisfies this guard, rather than accepting the head group outright — the genuine search this station exists to exercise.

Source code in `benchmarks/cafe/stations/cupping.py`

```
def same_origin(tokens: list[Token]) -> bool:
    """**T_Cupping_Flight**'s guard: every cup in the flight must be comparable.

    Cafe role:
        Cupping is a side-by-side comparison. Scoring an Ethiopian sample against a
        Colombian one in the same flight produces a meaningless number — the guard is the
        roaster's rule that a flight only forms when every cup on the tray shares a
        single-origin lot.

    Demonstrates:
        A **certified guard evaluated per candidate binding**, under
        [`BindingPolicy.PRIORITY`][cpnx.BindingPolicy]. It reads only each bound token's `payload["origin"]`,
        so `cpnx.certification` proves it closed-world and the engine runs it inline under
        the lock rather than round-tripping it through the timeout-bounded executor. What
        it costs is not the per-evaluation price — it is *how many* evaluations happen:
        one per candidate `count`-sized combination `_iter_candidate_bindings` yields, up
        to `binding_search_limit + 1` of them. A bench with several origins mixed together
        forces the search to actually walk combinations looking for one that satisfies
        this guard, rather than accepting the head group outright — the genuine search
        this station exists to exercise.
    """
    origins = {t.payload.get("origin") for t in tokens}
    return len(origins) == 1
```

### score_flight

```
score_flight(tokens: list[Token]) -> list[Token]
```

**T_Cupping_Flight**'s action: record one score for the flight.

Cafe role

The roaster tastes every cup in the flight and writes down a single verdict for the lot, rather than a per-cup note — cupping judges the flight as a group.

Demonstrates

Deliberate **minimalism as experimental hygiene**, matching cafe.stations.batch_triage.serve_batch_triage: this station exists to exercise the keyed-arc/guard/`PRIORITY` candidate-space search, and re-modelling any real scoring logic here would put unrelated engine work in the measurement. It reports the count of cups actually tasted, which lets a caller confirm `count` samples were bound (not merely that the shared origin held) without inspecting the raw binding.

Source code in `benchmarks/cafe/stations/cupping.py`

```
def score_flight(tokens: list[Token]) -> list[Token]:
    """**T_Cupping_Flight**'s action: record one score for the flight.

    Cafe role:
        The roaster tastes every cup in the flight and writes down a single verdict for
        the lot, rather than a per-cup note — cupping judges the flight as a group.

    Demonstrates:
        Deliberate **minimalism as experimental hygiene**, matching
        [`cafe.stations.batch_triage.serve_batch_triage`][cafe.stations.batch_triage.serve_batch_triage]: this station
        exists to exercise the keyed-arc/guard/`PRIORITY` candidate-space search, and re-modelling any real
        scoring logic here would put unrelated engine work in the measurement. It reports
        the count of cups actually tasted, which lets a caller confirm `count` samples
        were bound (not merely that the shared origin held) without inspecting the raw
        binding.
    """
    origin = tokens[0].payload.get("origin")
    return [
        Token(
            payload={"origin": origin, "cups_tasted": len(tokens)},
            color="cupping_score",
        )
    ]
```

### places

```
places() -> list[Place]
```

The bench — an unbounded, unordered Place holding green-coffee samples.

Cafe role

Where samples sit until a flight is called. Nothing about the place itself enforces same-origin grouping; that constraint lives entirely in `T_Cupping_Flight`'s guard, so the bench can hold as many mixed origins at once as a real cupping session would.

Demonstrates

A plain Place, same shape as every other cafe queue — the interesting engine behavior in this station lives in the arc's `key`/`count` and the transition's guard/`binding_policy`, not in the place.

Args:

Returns:

| Type          | Description                                      |
| ------------- | ------------------------------------------------ |
| `list[Place]` | A single-element list containing P_Sample_Queue. |

Source code in `benchmarks/cafe/stations/cupping.py`

```
def places() -> list[Place]:
    """The bench — an unbounded, unordered [`Place`][cpnx.Place] holding green-coffee samples.

    Cafe role:
        Where samples sit until a flight is called. Nothing about the place itself
        enforces same-origin grouping; that constraint lives entirely in
        `T_Cupping_Flight`'s guard, so the bench can hold as many mixed origins at once
        as a real cupping session would.

    Demonstrates:
        A plain [`Place`][cpnx.Place], same shape as every other cafe queue — the interesting engine
        behavior in this station lives in the arc's `key`/`count` and the transition's
        guard/`binding_policy`, not in the place.

    Args:

    Returns:
        A single-element list containing `P_Sample_Queue`.
    """
    return [Place("P_Sample_Queue", schema=has_payload)]
```

### transitions

```
transitions(
    *, work_secs: float = 0.0, count: int = 4
) -> list[Transition]
```

**T_Cupping_Flight** — cup `count` same-origin samples together and score the flight.

Cafe role

The roaster calls a flight: pull `count` samples off the bench, all one origin, taste them side by side, write down one score. Raising `count` models a larger cupping table (more cups tasted per sitting); a deeper, more mixed-origin bench models a busier QC queue.

Demonstrates

The full combination this station exists to isolate: a **certified InputArc.key at `count > 1`**, gating a **certified guard evaluated per candidate binding**, under `binding_policy=BindingPolicy.PRIORITY` so the engine actually enumerates rather than accepting the head group. See the module docstring for why this is a candidate-space cost, not a pool-scan one, and for the sweep this station is meant to drive.

Parameters:

| Name        | Type    | Description                                                                                                                                                                     | Default |
| ----------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| `work_secs` | `float` | Physical seconds the station occupies a worker.                                                                                                                                 | `0.0`   |
| `count`     | `int`   | Number of samples per flight. Defaults to 4. Sweeping this against queue depth and origin mix is what exposes the candidate-space truncation described in the module docstring. | `4`     |

Returns:

| Type               | Description                                        |
| ------------------ | -------------------------------------------------- |
| `list[Transition]` | A single-element list containing T_Cupping_Flight. |

Source code in `benchmarks/cafe/stations/cupping.py`

```
def transitions(*, work_secs: float = 0.0, count: int = 4) -> list[Transition]:
    """**T_Cupping_Flight** — cup `count` same-origin samples together and score the flight.

    Cafe role:
        The roaster calls a flight: pull `count` samples off the bench, all one origin,
        taste them side by side, write down one score. Raising `count` models a larger
        cupping table (more cups tasted per sitting); a deeper, more mixed-origin bench
        models a busier QC queue.

    Demonstrates:
        The full combination this station exists to isolate: a **certified [`InputArc.key`][cpnx.InputArc]
        at `count > 1`**, gating a **certified guard evaluated per candidate binding**,
        under `binding_policy=BindingPolicy.PRIORITY` so the engine actually enumerates
        rather than accepting the head group. See the module docstring for why this is a
        candidate-space cost, not a pool-scan one, and for the sweep this station is meant
        to drive.

    Args:
        work_secs: Physical seconds the station occupies a worker.
        count: Number of samples per flight. Defaults to 4. Sweeping this against queue
            depth and origin mix is what exposes the candidate-space truncation described
            in the module docstring.

    Returns:
        A single-element list containing `T_Cupping_Flight`.
    """
    return [
        Transition(
            name="T_Cupping_Flight",
            inputs=[InputArc("P_Sample_Queue", count=count, key=cup_score)],
            outputs=[OutputArc("P_Served")],
            guard=same_origin,
            binding_policy=BindingPolicy.PRIORITY,
            action=with_work(work_secs, score_flight),
            action_timeout_secs=0.5,
        )
    ]
```

### 🥐 The pastry case — a `SubstitutionTransition`

## cafe.stations.pastry_case

🥐 The pastry case — the fixture's only SubstitutionTransition.

Cafe role

Food orders don't go through the espresso bar at all. A ticket goes straight back to the kitchen, where a pastry is unwrapped, warmed in a small oven that can only take one at a time, and plated. From the bar's point of view that whole sequence is one step: "the kitchen handled it." The base cafe fixture deliberately has no such station — its topology notes a "kitchen" subnet as the natural extension, without building it. This module is that extension.

Demonstrates

**Hierarchical decomposition, and its per-firing cost.** Every other station in this package adds a place or two and a handful of ordinary Transitions, each firing is one action call. `T_Pastry_Case` is different: each firing drives an entire nested PetriNet — the kitchen — to quiescence, bounded by `subnet_deadline_secs`. The parent pays not one action but a whole nested run, and that fixed per-firing overhead is charged once per food order. It is the only place in the corpus where engine cost is not proportional to a single action.

**Isolation, precisely.** The child subnet carries no reference to its parent — it is constructed, run, and drained as a wholly separate PetriNet with its own lock, its own clock, its own thread pools. Communication crosses the boundary *only* through `port_socket_map`: a port place inside the subnet (`P_Pastry_In`, `P_Pastry_Out`) is bound by name to a socket place in the parent (`P_Food_Order`, `P_Served`). Nothing else crosses — the oven's contention is invisible to the bar, and the bar's ticket line is invisible to the kitchen.

**Open performance questions this station exists to answer** (nothing in the corpus currently measures them):

- How does per-firing overhead scale with subnet size — a 3-transition kitchen versus a 10-transition one?
- Is the *parent's* engine lock held across the child's entire run? If so, every food order is a global stall on the parent net, not just a slow local one — this is the thing worth checking first.
- How does `subnet_deadline_secs` interact with the parent's own quiescence, e.g. when the kitchen itself can't drain (oven contention, a stuck action) within the budget it's given?

See `cpnx.transitions.SubstitutionTransition` and `cpnx.PetriNet._execute_substitution_transition` for the mechanism.

Warning

**A subnet instance may be wrapped by only one SubstitutionTransition at a time** — `SubstitutionTransition.__post_init__` tracks every subnet it has ever wrapped in a process-wide `weakref.WeakSet` and raises `ValueError` the moment a second transition tries to wrap the same instance. `transitions()` below therefore calls `build_kitchen_subnet()` to construct a **brand-new** PetriNet on every invocation — never a module-level singleton reused across calls. Two consecutive `transitions()` calls in the same process must both succeed; if you ever see this module raise on the second call, someone hoisted the subnet out of the function body.

### unwrap_pastry

```
unwrap_pastry(tokens: list[Token]) -> list[Token]
```

**T_Unwrap**'s action: pull a pastry from its wrapper.

Cafe role

The first thing that happens to a food ticket in the kitchen — no oven contention yet, just unwrapping.

Demonstrates

The kitchen subnet's first internal stage. Purely structural: it exists so the subnet has more than one hop between its port places, the same reason cafe.transitions chains multiple ordinary stations on the bar side.

Source code in `benchmarks/cafe/stations/pastry_case.py`

```
def unwrap_pastry(tokens: list[Token]) -> list[Token]:
    """**T_Unwrap**'s action: pull a pastry from its wrapper.

    Cafe role:
        The first thing that happens to a food ticket in the kitchen — no oven
        contention yet, just unwrapping.

    Demonstrates:
        The kitchen subnet's first internal stage. Purely structural: it exists so
        the subnet has more than one hop between its port places, the same reason
        [`cafe.transitions`][cafe.transitions] chains multiple ordinary stations on the bar side.
    """
    ticket = tokens[0]
    return [ticket.evolve(payload_updates={"stage": "unwrapped"})]
```

### warm_pastry

```
warm_pastry(tokens: list[Token]) -> list[Token]
```

**T_Warm**'s action: hold the pastry in the oven for its cycle.

Cafe role

The oven only fits one pastry at a time, so this is where a rush queues up inside the kitchen — invisible from the bar, which just sees "kitchen is handling it."

Demonstrates

The **resource-return contract** applied inside a subnet exactly as it works in the parent net: the oven permit consumed alongside the pastry is not returned here. The engine deposits any consumed-but-unreturned resource token back into its source place once the action completes — and because `P_Oven` is a PacedResourcePlace, that deposit starts a cooldown, so the oven stays occupied for `pacing_secs` after each pastry even though the action itself already returned. Only the data token is produced here.

Source code in `benchmarks/cafe/stations/pastry_case.py`

```
def warm_pastry(tokens: list[Token]) -> list[Token]:
    """**T_Warm**'s action: hold the pastry in the oven for its cycle.

    Cafe role:
        The oven only fits one pastry at a time, so this is where a rush queues up
        inside the kitchen — invisible from the bar, which just sees "kitchen is
        handling it."

    Demonstrates:
        The **resource-return contract** applied inside a subnet exactly as it works
        in the parent net: the oven permit consumed alongside the pastry is not
        returned here. The engine deposits any consumed-but-unreturned resource token
        back into its source place once the action completes — and because
        `P_Oven` is a [`PacedResourcePlace`][cpnx.PacedResourcePlace], that deposit starts a cooldown, so the
        oven stays occupied for `pacing_secs` after each pastry even though the
        action itself already returned. Only the data token is produced here.
    """
    pastry = next(t for t in tokens if not t.is_resource)
    return [pastry.evolve(payload_updates={"stage": "warmed"})]
```

### plate_pastry

```
plate_pastry(tokens: list[Token]) -> list[Token]
```

**T_Plate**'s action: plate the warmed pastry and hand it to the port.

Cafe role

The last kitchen step — onto a plate and out through `P_Pastry_Out`, where the parent's `port_socket_map` picks it up as a served drink/food item.

Demonstrates

The subnet's exit: the token reaching `P_Pastry_Out` is what `PetriNet._retrieve_subnet_outputs` collects and hands back to the parent as this firing's output, to be deposited into whichever parent place `port_socket_map` names for that port.

Source code in `benchmarks/cafe/stations/pastry_case.py`

```
def plate_pastry(tokens: list[Token]) -> list[Token]:
    """**T_Plate**'s action: plate the warmed pastry and hand it to the port.

    Cafe role:
        The last kitchen step — onto a plate and out through `P_Pastry_Out`, where
        the parent's `port_socket_map` picks it up as a served drink/food item.

    Demonstrates:
        The subnet's exit: the token reaching `P_Pastry_Out` is what
        `PetriNet._retrieve_subnet_outputs` collects and hands back to the parent as
        this firing's output, to be deposited into whichever parent place
        `port_socket_map` names for that port.
    """
    warmed = tokens[0]
    return [warmed.evolve(payload_updates={"stage": "plated"}, color="drink")]
```

### build_kitchen_subnet

```
build_kitchen_subnet(
    *,
    oven_capacity: int = 1,
    oven_pacing_secs: float = 0.05,
    work_secs: float = 0.0,
) -> PetriNet
```

Construct a fresh kitchen PetriNet — unwrap → warm (oven) → plate.

Cafe role

The kitchen behind the pastry case: a small, self-contained workflow with its own genuine bottleneck (one oven), modelled as a subnet rather than inlined into the bar's topology.

Demonstrates

The **structure a SubstitutionTransition wraps**. Three places (`P_Pastry_In`, `P_Warming_Rack`, `P_Warmed_Rack`... — see below — and `P_Pastry_Out`) and a PacedResourcePlace oven give the subnet real internal back-pressure (only one pastry warms at a time; every pastry after it waits out `oven_pacing_secs`) rather than being a trivial pass-through pipe. That back-pressure is entirely internal to this PetriNet — the parent net that eventually wraps this one in a SubstitutionTransition never sees `P_Oven`, only whatever arrives at `P_Pastry_Out`.

Warning

Returns a **new** PetriNet instance every call, deliberately. A subnet instance can be wrapped by at most one SubstitutionTransition for the lifetime of the process (see the module docstring) — reusing one across two SubstitutionTransition constructions raises `ValueError`. Callers building more than one pastry-case transition (or calling `transitions()` more than once) must call this again for each one.

Parameters:

| Name               | Type    | Description                                                                                                                                                                 | Default |
| ------------------ | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| `oven_capacity`    | `int`   | Number of pastries the oven can warm concurrently. Defaults to 1, the whole point of the bottleneck.                                                                        | `1`     |
| `oven_pacing_secs` | `float` | Cooldown the oven needs between pastries, modelling its recovery time between bakes. Defaults to a small 0.05s so the subnet reaches quiescence quickly in a benchmark run. | `0.05`  |
| `work_secs`        | `float` | Wall-clock seconds each kitchen action sleeps before returning, mirroring cafe.support.with_work on the bar side.                                                           | `0.0`   |

Returns:

| Type       | Description                                                         |
| ---------- | ------------------------------------------------------------------- |
| `PetriNet` | An unstarted PetriNet with port places P_Pastry_In and P_Pastry_Out |
| `PetriNet` | already populated, ready to be wrapped by a SubstitutionTransition. |

Source code in `benchmarks/cafe/stations/pastry_case.py`

```
def build_kitchen_subnet(*, oven_capacity: int = 1, oven_pacing_secs: float = 0.05, work_secs: float = 0.0) -> PetriNet:
    """Construct a fresh kitchen [`PetriNet`][cpnx.PetriNet] — unwrap → warm (oven) → plate.

    Cafe role:
        The kitchen behind the pastry case: a small, self-contained workflow with its
        own genuine bottleneck (one oven), modelled as a subnet rather than inlined
        into the bar's topology.

    Demonstrates:
        The **structure a [`SubstitutionTransition`][cpnx.SubstitutionTransition] wraps**. Three places
        (`P_Pastry_In`, `P_Warming_Rack`, `P_Warmed_Rack`... — see below — and
        `P_Pastry_Out`) and a [`PacedResourcePlace`][cpnx.PacedResourcePlace] oven give the subnet real internal
        back-pressure (only one pastry warms at a time; every pastry after it waits
        out `oven_pacing_secs`) rather than being a trivial pass-through pipe. That
        back-pressure is entirely internal to this [`PetriNet`][cpnx.PetriNet] — the parent net that
        eventually wraps this one in a [`SubstitutionTransition`][cpnx.SubstitutionTransition] never sees `P_Oven`,
        only whatever arrives at `P_Pastry_Out`.

    Warning:
        Returns a **new** [`PetriNet`][cpnx.PetriNet] instance every call, deliberately. A subnet
        instance can be wrapped by at most one [`SubstitutionTransition`][cpnx.SubstitutionTransition] for the
        lifetime of the process (see the module docstring) — reusing one across two
        [`SubstitutionTransition`][cpnx.SubstitutionTransition] constructions raises `ValueError`. Callers building
        more than one pastry-case transition (or calling `transitions()` more than
        once) must call this again for each one.

    Args:
        oven_capacity: Number of pastries the oven can warm concurrently. Defaults
            to 1, the whole point of the bottleneck.
        oven_pacing_secs: Cooldown the oven needs between pastries, modelling its
            recovery time between bakes. Defaults to a small 0.05s so the subnet
            reaches quiescence quickly in a benchmark run.
        work_secs: Wall-clock seconds each kitchen action sleeps before returning,
            mirroring [`cafe.support.with_work`][cafe.support.with_work] on the bar side.

    Returns:
        An unstarted [`PetriNet`][cpnx.PetriNet] with port places `P_Pastry_In` and `P_Pastry_Out`
        already populated, ready to be wrapped by a [`SubstitutionTransition`][cpnx.SubstitutionTransition].
    """
    p_pastry_in = Place(P_PASTRY_IN, schema=has_payload)
    p_unwrapped = Place("P_Unwrapped", schema=has_payload)
    p_oven = PacedResourcePlace("P_Oven", capacity=oven_capacity, pacing_secs=oven_pacing_secs)
    p_warmed = Place("P_Warmed", schema=has_payload)
    p_pastry_out = Place(P_PASTRY_OUT, schema=has_payload)

    t_unwrap = Transition(
        name="T_Unwrap",
        inputs=[InputArc(P_PASTRY_IN)],
        outputs=[OutputArc("P_Unwrapped")],
        action=with_work(work_secs, unwrap_pastry),
        action_timeout_secs=0.5,
    )
    t_warm = Transition(
        name="T_Warm",
        inputs=[InputArc("P_Unwrapped"), InputArc("P_Oven")],
        outputs=[OutputArc("P_Warmed")],
        action=with_work(work_secs, warm_pastry),
        action_timeout_secs=0.5,
    )
    t_plate = Transition(
        name="T_Plate",
        inputs=[InputArc("P_Warmed")],
        outputs=[OutputArc(P_PASTRY_OUT)],
        action=with_work(work_secs, plate_pastry),
        action_timeout_secs=0.5,
    )

    return PetriNet(
        places=[p_pastry_in, p_unwrapped, p_oven, p_warmed, p_pastry_out],
        transitions=[t_unwrap, t_warm, t_plate],
    )
```

### places

```
places() -> list[Place]
```

The socket the ticket comes in on — `P_Food_Order`, a plain unbounded Place.

Demonstrates

The **parent-side half of a substitution boundary**. `P_Food_Order` holds no special machinery of its own; everything interesting (the oven, the unwrap/warm/plate stages) lives inside the kitchen subnet that `T_Pastry_Case` wraps. This place is only ever the socket named by that transition's `port_socket_map`.

Source code in `benchmarks/cafe/stations/pastry_case.py`

```
def places() -> list[Place]:
    """The socket the ticket comes in on — `P_Food_Order`, a plain unbounded [`Place`][cpnx.Place].

    Demonstrates:
        The **parent-side half of a substitution boundary**. `P_Food_Order` holds no
        special machinery of its own; everything interesting (the oven, the
        unwrap/warm/plate stages) lives inside the kitchen subnet that
        `T_Pastry_Case` wraps. This place is only ever the socket named by that
        transition's `port_socket_map`.
    """
    return [Place("P_Food_Order", schema=has_payload)]
```

### transitions

```
transitions(
    *,
    work_secs: float = 0.0,
    oven_capacity: int = 1,
    oven_pacing_secs: float = 0.05,
    subnet_deadline_secs: float = 5.0,
) -> list[Transition]
```

**T_Pastry_Case** — the fixture's only SubstitutionTransition.

Cafe role

One ticket in, one ready-to-serve pastry out — the whole unwrap/warm/plate sequence happens behind this single step, exactly as it does for a customer watching the counter: they see a ticket go back, and a pastry come out.

Demonstrates

**Firing a SubstitutionTransition**: `port_socket_map` binds the kitchen's `P_Pastry_In` to this net's `P_Food_Order` socket, and its `P_Pastry_Out` to `P_Served`. Each firing drives `build_kitchen_subnet()`'s three internal transitions to quiescence (or to `subnet_deadline_secs`, whichever comes first) before this transition can be said to have completed — see the module docstring for the performance questions that per-firing cost raises.

Warning

Calls `build_kitchen_subnet()` fresh on **every** call to `transitions()`, never reusing a cached subnet. A subnet instance can be wrapped by only one SubstitutionTransition in the lifetime of the process (enforced by a process-wide `weakref.WeakSet` in `SubstitutionTransition.__post_init__`); calling `transitions()` twice with a hoisted, shared subnet would make the second call raise `ValueError`. This module's own usage — one fresh subnet per call — is exactly what keeps repeated calls (e.g. from tests, or from `build_cafe` being invoked more than once in a process) safe.

Parameters:

| Name                   | Type    | Description                                                                                                                                                                                                                                                                                                              | Default |
| ---------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------- |
| `work_secs`            | `float` | Wall-clock seconds each kitchen action sleeps before returning.                                                                                                                                                                                                                                                          | `0.0`   |
| `oven_capacity`        | `int`   | Number of pastries the kitchen oven can warm concurrently. Defaults to 1 — see build_kitchen_subnet.                                                                                                                                                                                                                     | `1`     |
| `oven_pacing_secs`     | `float` | Cooldown the oven needs between pastries. Defaults to 0.05.                                                                                                                                                                                                                                                              | `0.05`  |
| `subnet_deadline_secs` | `float` | Maximum wall-clock seconds given to the kitchen subnet to reach quiescence on each firing. Defaults to 5.0 — generous for a three-transition kitchen with a sub-tenth-second oven cooldown, but see the module docstring's open question about how this interacts with the parent's own quiescence when it is too tight. | `5.0`   |

Source code in `benchmarks/cafe/stations/pastry_case.py`

```
def transitions(
    *,
    work_secs: float = 0.0,
    oven_capacity: int = 1,
    oven_pacing_secs: float = 0.05,
    subnet_deadline_secs: float = 5.0,
) -> list[Transition]:
    """**T_Pastry_Case** — the fixture's only [`SubstitutionTransition`][cpnx.SubstitutionTransition].

    Cafe role:
        One ticket in, one ready-to-serve pastry out — the whole unwrap/warm/plate
        sequence happens behind this single step, exactly as it does for a customer
        watching the counter: they see a ticket go back, and a pastry come out.

    Demonstrates:
        **Firing a [`SubstitutionTransition`][cpnx.SubstitutionTransition]**: `port_socket_map` binds the kitchen's
        `P_Pastry_In` to this net's `P_Food_Order` socket, and its `P_Pastry_Out` to
        `P_Served`. Each firing drives `build_kitchen_subnet()`'s three internal
        transitions to quiescence (or to `subnet_deadline_secs`, whichever comes
        first) before this transition can be said to have completed — see the module
        docstring for the performance questions that per-firing cost raises.

    Warning:
        Calls `build_kitchen_subnet()` fresh on **every** call to `transitions()`,
        never reusing a cached subnet. A subnet instance can be wrapped by only one
        [`SubstitutionTransition`][cpnx.SubstitutionTransition] in the lifetime of the process (enforced by a
        process-wide `weakref.WeakSet` in `SubstitutionTransition.__post_init__`);
        calling `transitions()` twice with a hoisted, shared subnet would make the
        second call raise `ValueError`. This module's own usage — one fresh subnet
        per call — is exactly what keeps repeated calls (e.g. from tests, or from
        `build_cafe` being invoked more than once in a process) safe.

    Args:
        work_secs: Wall-clock seconds each kitchen action sleeps before returning.
        oven_capacity: Number of pastries the kitchen oven can warm concurrently.
            Defaults to 1 — see [`build_kitchen_subnet`][cafe.stations.pastry_case.build_kitchen_subnet].
        oven_pacing_secs: Cooldown the oven needs between pastries. Defaults to 0.05.
        subnet_deadline_secs: Maximum wall-clock seconds given to the kitchen subnet
            to reach quiescence on each firing. Defaults to 5.0 — generous for a
            three-transition kitchen with a sub-tenth-second oven cooldown, but see
            the module docstring's open question about how this interacts with the
            parent's own quiescence when it is too tight.
    """
    kitchen = build_kitchen_subnet(
        oven_capacity=oven_capacity, oven_pacing_secs=oven_pacing_secs, work_secs=work_secs
    )
    return [
        SubstitutionTransition(
            name="T_Pastry_Case",
            inputs=[InputArc("P_Food_Order")],
            outputs=[OutputArc("P_Served")],
            action=None,  # type: ignore[assignment] — SubstitutionTransition fires the subnet, not an action.
            subnet=kitchen,
            port_socket_map={P_PASTRY_IN: "P_Food_Order", P_PASTRY_OUT: "P_Served"},
            subnet_deadline_secs=subnet_deadline_secs,
        )
    ]
```

## Shared helpers

## cafe.support

Shared helpers for the ☕ Concurrency Cafe fixture.

Nothing here models a cafe station in its own right — these are the small pieces every station borrows: the dose target the whole net is calibrated around, the tolerance-band arithmetic that turns one `dose_tolerance_g` knob into the `[low, high]` pair three inscriptions close over, and the `work_secs` wrapper that gives an otherwise-instant action some GIL-releasing physical duration.

### is_order

```
is_order(payload: Mapping) -> bool
```

Place `schema` predicate for order-ticket queues: a real ticket carries its dose weight.

Every order is seeded with `weight_g` and each pipeline step derives new tokens via Token.evolve (which preserves payload), so grounds and milk tickets keep it too. A bare or mis-wired token reaching one of these places would be missing it and get dead-lettered — which is the point: unlike the old `schema=dict` (always true, since every payload is a mapping), this actually rejects something.

Source code in `benchmarks/cafe/support.py`

```
def is_order(payload: Mapping) -> bool:
    """Place `schema` predicate for order-ticket queues: a real ticket carries its dose weight.

    Every order is seeded with ``weight_g`` and each pipeline step derives new tokens via
    [`Token.evolve`][cpnx.Token.evolve] (which preserves payload), so grounds and milk tickets
    keep it too. A bare or mis-wired token reaching one of these places would be missing it and
    get dead-lettered — which is the point: unlike the old ``schema=dict`` (always true, since
    every payload is a mapping), this actually rejects something.
    """
    return "weight_g" in payload
```

### has_payload

```
has_payload(payload: Mapping) -> bool
```

Place `schema` predicate for mixed/terminal queues: every cafe *data* token has a payload.

Used where a place legitimately holds heterogeneous tokens — freshly-assembled drinks (`{"components": ...}`), cold-brew batches (`{"batch": ...}`), station-specific tickets — so no single key can be required, but a payload-*less* data token still signals a wiring bug. Resource permits are exempt from schema validation, so their empty payload never trips this.

Source code in `benchmarks/cafe/support.py`

```
def has_payload(payload: Mapping) -> bool:
    """Place `schema` predicate for mixed/terminal queues: every cafe *data* token has a payload.

    Used where a place legitimately holds heterogeneous tokens — freshly-assembled drinks
    (``{"components": ...}``), cold-brew batches (``{"batch": ...}``), station-specific tickets —
    so no single key can be required, but a payload-*less* data token still signals a wiring bug.
    Resource permits are exempt from schema validation, so their empty payload never trips this.
    """
    return len(payload) > 0
```

### dose_band

```
dose_band(
    dose_tolerance_g: float | None,
) -> tuple[float | None, float | None]
```

Expand a half-width tolerance into the `(low, high)` band the guards close over.

Returns `(None, None)` when *dose_tolerance_g* is `None`, which is the fixture's signal to omit the dose guard (and `T_Rework_Dose`) entirely and reproduce the cheap guard-free binding-search path for A/B comparison.

Source code in `benchmarks/cafe/support.py`

```
def dose_band(dose_tolerance_g: float | None) -> tuple[float | None, float | None]:
    """Expand a half-width tolerance into the ``(low, high)`` band the guards close over.

    Returns ``(None, None)`` when *dose_tolerance_g* is ``None``, which is the fixture's
    signal to omit the dose guard (and `T_Rework_Dose`) entirely and reproduce the cheap
    guard-free binding-search path for A/B comparison.
    """
    if dose_tolerance_g is None:
        return (None, None)
    return (DOSE_TARGET_G - dose_tolerance_g, DOSE_TARGET_G + dose_tolerance_g)
```

### with_work

```
with_work(work_secs: float, action: Action) -> Action
```

Wrap *action* so it sleeps `work_secs` before running, unless `work_secs` is 0.

Models the physical time a barista actually spends at a station, as opposed to PacedResourcePlace.pacing_secs, which models a *machine's* recovery time. `time.sleep` releases the GIL, which is the whole point — it is what makes parallel speedup across the engine's thread pool observable instead of purely theoretical, since an instant pure-Python action would just measure CPython.

Returns *action* unchanged when `work_secs <= 0`, so the default configuration adds no wrapper frame to the profile.

Source code in `benchmarks/cafe/support.py`

```
def with_work(work_secs: float, action: Action) -> Action:
    """Wrap *action* so it sleeps ``work_secs`` before running, unless ``work_secs`` is 0.

    Models the physical time a barista actually spends at a station, as opposed to
    [`PacedResourcePlace.pacing_secs`][cpnx.PacedResourcePlace], which models a *machine's* recovery time.
    ``time.sleep`` releases the GIL, which is the whole point — it is what makes
    parallel speedup across the engine's thread pool observable instead of purely
    theoretical, since an instant pure-Python action would just measure CPython.

    Returns *action* unchanged when ``work_secs <= 0``, so the default configuration
    adds no wrapper frame to the profile.
    """
    if work_secs <= 0:
        return action

    def _wrapped(tokens: list[Token]) -> list[Token]:
        time.sleep(work_secs)
        return action(tokens)

    return _wrapped
```
