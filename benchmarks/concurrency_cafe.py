"""☕ The Concurrency Cafe — a whimsical, illustrative ``cpnx`` reference topology.

Picture a single-bar specialty coffee shop during the morning rush. Tickets pile
up at the register, baristas share a small pool of digital scales, there is
exactly one burr grinder (which needs a breather after every dose), a barista
won't grind a ticket whose declared dose misses spec (it goes back for a
re-dose instead), and a finished drink is only "done" once *both* the espresso
shot and the steamed milk have landed on the same tray. That whole scene maps
almost one-to-one onto ``cpnx``'s vocabulary of places, resources, thresholds,
guards, and sinks — which is why it makes a good end-to-end tour of the
library.

Warning:
    This is an **illustrative benchmark/demo, not a conservation-checked CPN**.
    Its transitions *transform* tokens (an order token is consumed and becomes
    a ground-coffee token, then an espresso token, then part of a drink token)
    rather than merely moving fixed colours between places. That is deliberate
    and idiomatic for ``cpnx`` (see ``ResourcePlace`` vs. data places in the
    library docs), but it means you should not expect the total token count, or
    any single colour's count, to be invariant across a run the way it would be
    in a strict place/transition conservation model. Treat the numbers this
    script prints as "a cafe served some drinks and binned some botched shots",
    not as an audited ledger.

Token colours in play:

- ``None`` (order tickets) — an uncoloured data token carrying the customer's
  order as its ``payload``: ``ratio``, ``weight_g``, ``dairy_free``,
  ``mobile_pickup``.
- ``"resource"`` — permit tokens pre-filled into :class:`~cpnx.ResourcePlace`
  and :class:`~cpnx.PacedResourcePlace` instances (scales, the grinder). These
  are returned automatically by the engine once consumed; the action code
  never has to hand them back explicitly.
- ``"ground_coffee"`` / ``"milk_ticket"`` — intermediate work-in-progress
  tokens produced by the grind step, one feeding the espresso line and one
  feeding the milk line.
- ``"espresso"`` / ``"oat_milk"`` / ``"dairy_milk"`` — finished component
  tokens that accumulate on the order tray.
- ``"drink"`` — the final assembled beverage, deposited into the terminal
  ``P_Served`` sink.

Station legend (cpnx type -> what it models):

| Place              | cpnx type             | Cafe role                                          |
|--------------------|------------------------|-----------------------------------------------------|
| ``P_Ticket_Line``  | ``Place``              | Unbounded FIFO of incoming order tickets            |
| ``P_Digital_Scales``| ``ResourcePlace``     | Shared pool of 3 scales                             |
| ``P_Burr_Grinder`` | ``PacedResourcePlace``  | The one grinder, with an 8s cooldown after dosing   |
| ``P_Ground_Coffee``| ``Place``               | Ground coffee awaiting a shot to be pulled          |
| ``P_Milk_Queue``   | ``Place``               | Milk tickets awaiting steaming                      |
| ``P_Order_Tray``   | ``ThresholdPlace``      | Holds shot + milk until both have arrived           |
| ``P_Served``       | ``SinkPlace``           | Terminal place for completed drinks                 |
| ``P_Trash_Can``    | ``SinkPlace``           | Dead-letter bin for botched shots (also error_place) |

Run it directly:

    python benchmarks/concurrency_cafe.py
"""

import random
import sys
import time
from pathlib import Path

if __name__ == "__main__":  # pragma: no cover - path shim for standalone execution
    # Mirrors how the repo's pytest config makes ``src`` importable
    # (``pythonpath = ["src"]`` in pyproject.toml): when this file is run directly
    # rather than through pytest, ``cpnx`` is not yet on sys.path, so add it here.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cpnx import (  # noqa: E402
    BindingPolicy,
    InputArc,
    OutputArc,
    PacedResourcePlace,
    PetriNet,
    Place,
    ResourcePlace,
    SinkPlace,
    ThresholdPlace,
    Token,
    Transition,
)

# NOTE: A SubstitutionTransition ("kitchen" subnet wrapping, say, the grind+shot
# steps as a nested PetriNet) is a natural extension here — cpnx supports
# hierarchical CPNs precisely for this kind of station-within-a-station
# decomposition. It is intentionally omitted from this demo: getting the
# port_socket_map wiring and subnet_deadline_secs right adds real complexity
# for an illustrative example, and the flat topology below already exercises
# every other corner of the API. A follow-up could carve ``P_Ground_Coffee``
# through ``P_Order_Tray`` out into its own subnet behind a single
# SubstitutionTransition("T_Kitchen", ...).


#: Target dose in grams. Orders in this demo cluster around 18g (a typical single-shot dose);
#: the tolerance band is centered on it.
_DOSE_TARGET_G = 18.0


def _make_dose_guard(low: float, high: float):
    """Build the ``T_Weigh_And_Grind`` guard: a barista won't grind an out-of-spec dose.

    Weighing the dose is the whole point of the scale step — a reading outside the shop's
    tolerance band (too little grounds under-extracts, too much over-extracts) doesn't get
    ground; it goes back for a re-dose instead (see ``T_Rework_Dose``). This is a *callable*
    guard, not a string expression, because it closes over the configured ``low``/``high``
    band, exactly like ``_make_pull_shot`` closes over ``failure_rate`` below.
    """

    def _dose_in_spec(tokens: list[Token]) -> bool:
        order = next(t for t in tokens if not t.is_resource)
        return low <= order.payload.get("weight_g", _DOSE_TARGET_G) <= high

    return _dose_in_spec


def _make_rework_dose(low: float, high: float):
    """Build the ``T_Rework_Dose`` action: adjust the grinder and re-weigh an out-of-spec ticket.

    Clamps to the *nearest* bound rather than snapping to the band's center, so a single
    rework always lands the weight back in `[low, high]` — satisfying ``T_Weigh_And_Grind``'s
    guard on the next pass — instead of overshooting past the opposite bound and bouncing
    between the two transitions forever.
    """

    def _rework_dose(tokens: list[Token]) -> list[Token]:
        ticket = tokens[0]
        weight = ticket.payload.get("weight_g", _DOSE_TARGET_G)
        return [ticket.evolve(payload_updates={"weight_g": min(max(weight, low), high)})]

    return _rework_dose


def _weigh_and_grind(tokens: list[Token]) -> list[Token]:
    """Consume the order ticket and hand off two parallel work items: grounds and a milk ticket.

    The scale and grinder resource tokens consumed alongside the order do not need to be
    returned here — the engine automatically deposits any consumed-but-unreturned resource
    token back into its source place once the action completes (see ``ResourcePlace`` docs),
    so this action only needs to produce the *data* tokens that carry the order forward.
    """
    order = next(t for t in tokens if not t.is_resource)
    grounds = order.evolve(payload_updates={"stage": "grounds"}, color="ground_coffee")
    milk_ticket = order.evolve(payload_updates={"stage": "milk_ticket"}, color="milk_ticket")
    return [grounds, milk_ticket]


def _mobile_pickup_first(tokens: list[Token]) -> tuple[int, float]:
    """PRIORITY key: mobile-pickup tickets (0) jump ahead of walk-ins (1); ties broken by age."""
    order = next(t for t in tokens if t.color is None)
    return (0 if order.payload.get("mobile_pickup") else 1, order.created_at)


def _make_pull_shot(failure_rate: float, seed: int | None):
    """Build the ``T_Pull_Shot`` action with a configurable channeling failure rate.

    A ``failure_rate`` of ~0.15 stands in for a channeled/uneven extraction. Combined with the
    transition's ``max_retries=1``, a channeled shot gets one automatic retry (the grounds token
    is rolled back to ``P_Ground_Coffee``) before the engine dead-letters it to ``P_Trash_Can``
    (this net's ``error_place``) so a bad dose doesn't loop forever.

    Passing ``seed`` swaps the global ``random`` module for a private ``random.Random(seed)``,
    which makes a channeling run reproducible — but **only at ``max_workers=1``**. Actions run
    on a thread pool, so above one worker the *order* in which concurrent ``T_Pull_Shot``
    firings draw from the shared generator is scheduler-dependent, and a fixed seed no longer
    pins which shots channel. ``random.Random`` is also not documented as thread-safe. The
    benchmark's channeling regime therefore runs single-worker only.
    """
    rng = random.Random(seed) if seed is not None else random

    def _pull_shot(tokens: list[Token]) -> list[Token]:
        grounds = tokens[0]
        if failure_rate and rng.random() < failure_rate:
            raise RuntimeError("channeling detected — shot pulled unevenly, discarding grounds")
        return [grounds.evolve(payload_updates={"stage": "espresso"}, color="espresso")]

    return _pull_shot


def _steam_milk(tokens: list[Token]) -> list[Token]:
    """Steam oat or dairy milk depending on the original order's dairy_free flag."""
    ticket = tokens[0]
    color = "oat_milk" if ticket.payload.get("dairy_free") else "dairy_milk"
    return [ticket.evolve(payload_updates={"stage": color}, color=color)]


def _serve_drink(tokens: list[Token]) -> list[Token]:
    """Assemble whatever pair of tray tokens is at hand into a single served drink.

    Illustrative simplification: the tray is a plain FIFO ``ThresholdPlace``, so the two
    tokens retrieved for a given firing are whichever espresso/milk tokens happen to be at
    the head of the queue, not guaranteed to be the *same* customer's shot and milk. That's
    fine for a benchmark meant to exercise concurrency and station wiring, but it is exactly
    the kind of thing conservation-checking (out of scope here) would normally catch.
    """
    components = sorted(t.color for t in tokens)
    return [Token(color="drink", payload={"components": components})]


def build_cafe(
    *,
    pacing_secs: float = 8.0,
    channel_failure_rate: float = 0.15,
    channel_seed: int | None = None,
    max_workers: int = 4,
    dose_tolerance_g: float | None = 1.0,
) -> PetriNet:
    """Wire up the Concurrency Cafe topology and return the (unstarted) PetriNet.

    Flow: ``P_Ticket_Line`` -> (weigh & grind, gated by the dose guard, using a scale + the
    grinder) -> ``P_Ground_Coffee`` / ``P_Milk_Queue`` in parallel -> (pull shot / steam milk)
    -> ``P_Order_Tray`` (waits for both a shot and a milk) -> (serve) -> ``P_Served``. A ticket
    whose declared dose misses the tolerance band is reworked (``T_Rework_Dose``) and
    returned to the back of ``P_Ticket_Line`` rather than ever reaching the grinder. Botched
    shots are dead-lettered to ``P_Trash_Can``.

    This net is illustrative and **not conservation-checked**: transitions transform token
    colours/payloads rather than merely relocating fixed tokens, so per-colour counts are
    not expected to balance across a run. See the module docstring for the full caveat.

    Args:
        pacing_secs: Grinder cooldown window. The default 8.0 models a real spin-down; the
            throughput benchmark keeps it non-zero (real back-pressure) but drives the net on
            a logical clock so the wait costs no wall-clock time.
        channel_failure_rate: Probability that ``T_Pull_Shot`` "channels" and dead-letters a
            shot. The default 0.15 exercises the retry/dead-letter path; passing 0.0 makes the
            run draw no RNG at all, so it reproduces step-for-step at any worker count.
        channel_seed: Seed for a private channeling RNG, so a non-zero ``channel_failure_rate``
            still reproduces. Only effective at ``max_workers=1`` — see ``_make_pull_shot``.
            ``None`` (the default) uses the global ``random`` module, i.e. non-reproducible.
        max_workers: Size of the engine's action thread pool.
        dose_tolerance_g: Half-width, in grams, of the acceptable dose band around the 18g
            target (default 1.0 -> `[17, 19]`). This is the knob that drives per-candidate
            guard evaluation cost (see ADR 0001): a tighter band rejects more tickets (more
            guard evaluations, more ``T_Rework_Dose`` churn), a wider one accepts nearly
            everything, and `None` removes the guard entirely (`T_Weigh_And_Grind.guard` is
            left unset and `T_Rework_Dose` is omitted), reproducing the cheap guard-free
            binding-search path for A/B comparison.
    """
    places = [
        # Unbounded FIFO: the register never turns a customer away, it just queues them.
        Place("P_Ticket_Line"),
        # A shared pool of 3 scales; a barista grabs one to weigh the dose and returns it
        # immediately (the engine auto-returns consumed-but-unreturned resource tokens).
        ResourcePlace("P_Digital_Scales", capacity=3),
        # The single grinder is unavailable for 8s after dispensing while the burrs spin
        # down and the chute is brushed out — models a hard rate limit on throughput.
        PacedResourcePlace("P_Burr_Grinder", capacity=1, pacing_secs=pacing_secs),
        # Grounds waiting for a barista to pull a shot; restricted colour catches wiring bugs.
        Place("P_Ground_Coffee", color_set={"ground_coffee"}),
        # Milk tickets waiting to be steamed; restricted colour catches wiring bugs.
        Place("P_Milk_Queue", color_set={"milk_ticket"}),
        # A drink isn't handed off until BOTH the espresso shot and the steamed milk have
        # landed on the tray — threshold=2 encodes that rendezvous directly on the place.
        ThresholdPlace("P_Order_Tray", threshold=2),
        # Terminal: completed drinks are absorbed and counted, never retrieved again.
        SinkPlace("P_Served"),
        # Holds the last 10 botched shots for quality inspection; also doubles as the
        # net's error_place so failed T_Pull_Shot firings dead-letter here automatically.
        SinkPlace("P_Trash_Can", keep_last=10),
    ]

    dose_low, dose_high = (
        (_DOSE_TARGET_G - dose_tolerance_g, _DOSE_TARGET_G + dose_tolerance_g)
        if dose_tolerance_g is not None
        else (None, None)
    )

    transitions = [
        Transition(
            name="T_Weigh_And_Grind",
            inputs=[
                InputArc("P_Ticket_Line"),
                InputArc("P_Digital_Scales"),
                InputArc("P_Burr_Grinder"),
            ],
            outputs=[OutputArc("P_Ground_Coffee"), OutputArc("P_Milk_Queue")],
            action=_weigh_and_grind,
            # Short timeout: weighing and dosing is a quick, bounded action in this demo.
            action_timeout_secs=1.0,
            # Dose tolerance gate: a barista won't grind a dose outside spec. This is the
            # PRIORITY transition draining the deep P_Ticket_Line, so it is exactly where
            # per-candidate guard evaluation (the combinatorial search's dominant cost —
            # see ADR 0001) actually happens. `None` (dose_tolerance_g unset) leaves the
            # guard unset entirely, reproducing the cheap guard-free path.
            guard=_make_dose_guard(dose_low, dose_high) if dose_tolerance_g is not None else None,
            # Mobile-pickup tickets jump the in-store line: PRIORITY enumerates satisfying
            # bindings and picks the one minimizing binding_priority_key.
            binding_policy=BindingPolicy.PRIORITY,
            binding_priority_key=_mobile_pickup_first,
        ),
        Transition(
            name="T_Pull_Shot",
            inputs=[InputArc("P_Ground_Coffee")],
            outputs=[OutputArc("P_Order_Tray")],
            action=_make_pull_shot(channel_failure_rate, channel_seed),
            # A stuck/uneven pull shouldn't hang the bar for long.
            action_timeout_secs=0.5,
            # One retry for a channeled shot, then dead-letter — a barista doesn't keep
            # re-pulling the same ruined dose forever.
            max_retries=1,
        ),
        Transition(
            name="T_Steam_Milk",
            inputs=[InputArc("P_Milk_Queue")],
            # Route the finished milk by colour so oat vs. dairy stays visible in the
            # event log even though both simply land on the shared tray.
            outputs=[
                OutputArc.on_color("oat_milk", "P_Order_Tray"),
                OutputArc.on_color("dairy_milk", "P_Order_Tray"),
            ],
            action=_steam_milk,
            action_timeout_secs=0.5,
        ),
        Transition(
            name="T_Serve_Drink",
            # Threshold=2 on the tray means this only becomes enabled once a shot AND a
            # milk are both present; count=2 drains exactly one drink's worth per firing.
            inputs=[InputArc("P_Order_Tray", count=2)],
            outputs=[OutputArc("P_Served")],
            action=_serve_drink,
            action_timeout_secs=0.5,
        ),
    ]

    if dose_tolerance_g is not None:
        transitions.append(
            Transition(
                name="T_Rework_Dose",
                # LEGACY (the net default): only ever inspects the head of P_Ticket_Line. A
                # ticket that isn't at the head yet is simply skipped over by
                # T_Weigh_And_Grind's PRIORITY search, so it reaches the head eventually
                # without T_Rework_Dose needing to enumerate for it.
                inputs=[InputArc("P_Ticket_Line")],
                outputs=[OutputArc("P_Ticket_Line")],
                action=_make_rework_dose(dose_low, dose_high),
                action_timeout_secs=0.5,
                # String guard (the complement of T_Weigh_And_Grind's callable one) so this
                # demo exercises both guard flavours: string guards run through
                # SandboxEvaluator's compile cache, callables through the timed expression
                # pool. Clamping in the action always lands back in [dose_low, dose_high],
                # so this can't ping-pong with T_Weigh_And_Grind's guard.
                guard=(
                    f"tokens[0].payload.get('weight_g', {_DOSE_TARGET_G}) < {dose_low} "
                    f"or tokens[0].payload.get('weight_g', {_DOSE_TARGET_G}) > {dose_high}"
                ),
            )
        )

    return PetriNet(
        max_workers=max_workers,
        error_place="P_Trash_Can",
        places=places,
        transitions=transitions,
        # Fast rollback so a channeled shot's grounds are eligible for a retry quickly
        # instead of the 1s default — keeps this demo snappy.
        retry_delay=0.2,
    )


if __name__ == "__main__":
    orders = [
        {"ratio": "1:2", "weight_g": 18, "dairy_free": True, "mobile_pickup": False},
        {"ratio": "1:2", "weight_g": 18, "dairy_free": False, "mobile_pickup": True},
        {"ratio": "1:2.5", "weight_g": 20, "dairy_free": False, "mobile_pickup": False},
        {"ratio": "1:2", "weight_g": 18, "dairy_free": True, "mobile_pickup": True},
    ]

    with build_cafe() as net:
        for payload in orders:
            net.deposit("P_Ticket_Line", Token(payload=payload))

        net.run(deadline=time.monotonic() + 2.0)

        marking = net.marking
        print("☕ Concurrency Cafe — final marking:")
        for place_name, tokens in marking.items():
            print(f"  {place_name:20s} {len(tokens)} token(s)")

        served = net.places["P_Served"].stats()
        trashed = net.places["P_Trash_Can"].stats()
        print(f"\nServed: {served['absorbed']} drink(s)")
        print(f"Trashed: {trashed['absorbed']} botched shot(s)")
