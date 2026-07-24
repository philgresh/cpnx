"""The core cafe's transition *actions* — the work a barista actually does.

Actions are the one part of a cpnx net that is explicitly allowed side effects:
they run on the engine's thread pool, **outside** the engine lock, and are not
purity-verified. That is the whole reason guards and binding keys live in
[`cafe.inscriptions`][cafe.inscriptions] instead — those run *under* the lock and must stay pure and
trivially cheap.

A recurring idiom below: an action that consumes a permit filters its input with
`is_resource` rather than indexing `tokens[0]`, because arc order does not
guarantee token position in the binding.
"""

import random

from cafe.support import DOSE_TARGET_G
from cpnx import Token


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
