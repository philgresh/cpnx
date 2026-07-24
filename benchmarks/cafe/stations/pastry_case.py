"""🥐 The pastry case — the fixture's only [`SubstitutionTransition`][cpnx.SubstitutionTransition].

Cafe role:
    Food orders don't go through the espresso bar at all. A ticket goes straight back
    to the kitchen, where a pastry is unwrapped, warmed in a small oven that can only
    take one at a time, and plated. From the bar's point of view that whole sequence
    is one step: "the kitchen handled it." The base cafe fixture deliberately has no
    such station — its topology notes a "kitchen" subnet as the natural extension,
    without building it. This module is that extension.

Demonstrates:
    **Hierarchical decomposition, and its per-firing cost.** Every other station in
    this package adds a place or two and a handful of ordinary [`Transition`][cpnx.Transition]s, each
    firing is one action call. `T_Pastry_Case` is different: each firing drives an
    entire nested [`PetriNet`][cpnx.PetriNet] — the kitchen — to quiescence, bounded by
    `subnet_deadline_secs`. The parent pays not one action but a whole nested run, and
    that fixed per-firing overhead is charged once per food order. It is the only
    place in the corpus where engine cost is not proportional to a single action.

    **Isolation, precisely.** The child subnet carries no reference to its parent —
    it is constructed, run, and drained as a wholly separate [`PetriNet`][cpnx.PetriNet] with its own
    lock, its own clock, its own thread pools. Communication crosses the boundary
    *only* through `port_socket_map`: a port place inside the subnet (`P_Pastry_In`,
    `P_Pastry_Out`) is bound by name to a socket place in the parent
    (`P_Food_Order`, `P_Served`). Nothing else crosses — the oven's contention is
    invisible to the bar, and the bar's ticket line is invisible to the kitchen.

    **Open performance questions this station exists to answer** (nothing in the
    corpus currently measures them):

    - How does per-firing overhead scale with subnet size — a 3-transition kitchen
      versus a 10-transition one?
    - Is the *parent's* engine lock held across the child's entire run? If so, every
      food order is a global stall on the parent net, not just a slow local one —
      this is the thing worth checking first.
    - How does `subnet_deadline_secs` interact with the parent's own quiescence,
      e.g. when the kitchen itself can't drain (oven contention, a stuck action)
      within the budget it's given?

    See `cpnx.transitions.SubstitutionTransition` and
    `cpnx.PetriNet._execute_substitution_transition` for the mechanism.

Warning:
    **A subnet instance may be wrapped by only one [`SubstitutionTransition`][cpnx.SubstitutionTransition] at a
    time** — `SubstitutionTransition.__post_init__` tracks every subnet it has ever
    wrapped in a process-wide `weakref.WeakSet` and raises `ValueError` the moment a
    second transition tries to wrap the same instance. `transitions()` below
    therefore calls `build_kitchen_subnet()` to construct a **brand-new** [`PetriNet`][cpnx.PetriNet]
    on every invocation — never a module-level singleton reused across calls. Two
    consecutive `transitions()` calls in the same process must both succeed; if you
    ever see this module raise on the second call, someone hoisted the subnet out of
    the function body.
"""

from cafe.support import with_work
from cpnx import InputArc, OutputArc, PacedResourcePlace, PetriNet, Place, SubstitutionTransition, Token, Transition

#: Port place names inside the kitchen subnet — see
#: [`build_kitchen_subnet`][cafe.stations.pastry_case.build_kitchen_subnet].
P_PASTRY_IN = "P_Pastry_In"
P_PASTRY_OUT = "P_Pastry_Out"


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
    p_pastry_in = Place(P_PASTRY_IN)
    p_unwrapped = Place("P_Unwrapped")
    p_oven = PacedResourcePlace("P_Oven", capacity=oven_capacity, pacing_secs=oven_pacing_secs)
    p_warmed = Place("P_Warmed")
    p_pastry_out = Place(P_PASTRY_OUT)

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


def places() -> list[Place]:
    """The socket the ticket comes in on — `P_Food_Order`, a plain unbounded [`Place`][cpnx.Place].

    Demonstrates:
        The **parent-side half of a substitution boundary**. `P_Food_Order` holds no
        special machinery of its own; everything interesting (the oven, the
        unwrap/warm/plate stages) lives inside the kitchen subnet that
        `T_Pastry_Case` wraps. This place is only ever the socket named by that
        transition's `port_socket_map`.
    """
    return [Place("P_Food_Order")]


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
