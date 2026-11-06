"""A failed `SubstitutionTransition` firing must leave its subnet as it found it (issue #38).

The subnet instance is reused across firings, so anything a failed firing leaves inside it
(the deposited port copies, half-processed intermediates, dead letters) is picked up by the
next firing. Each failure route below fails one order, then fires a clean one and checks that
the clean firing serves exactly that order and leaves the subnet at its pre-firing marking.

The subnet is a small cafe: an order in `Orders` is brewed by `Brew`, which borrows the
single `Barista` permit and reads the subnet-owned `Recipe` card (an initial marking that
must survive every purge), and lands in `Ready`.
"""

import functools
import math
import threading
import time
from collections import Counter

import pytest

from cpnx import (
    InputArc,
    OutputArc,
    PacedResourcePlace,
    PetriNet,
    Place,
    ResourcePlace,
    RetryPolicy,
    SinkPlace,
    SubstitutionTransition,
    Token,
    Transition,
)


class _Grinder:
    """Brew action that fails while `jammed` is set, and records every order it brews."""

    def __init__(self, jammed: bool = False) -> None:
        self.jammed = jammed
        self.brewed: list[int] = []

    def __call__(self, tokens: list[Token]) -> list[Token]:
        order, recipe = tokens[0], tokens[1]
        if self.jammed:
            raise RuntimeError("grinder jammed")
        self.brewed.append(order.payload["order"])
        return [order.evolve({"brewed": True}), recipe]


def _cafe(
    grinder: _Grinder,
    *,
    subnet_retries: int | None = None,
    deadline: float = 30.0,
    barista: ResourcePlace | None = None,
    max_workers: int = 1,
) -> tuple[PetriNet, PetriNet, Token]:
    recipe = Token(payload={"recipe": "flat white"})
    subnet = PetriNet(
        places=[
            Place("Orders"),
            Place("Ready"),
            Place("Recipe", initial_marking=[recipe]),
            barista or ResourcePlace("Barista", capacity=1),
        ],
        transitions=[
            Transition(
                "Brew",
                [InputArc("Orders"), InputArc("Recipe"), InputArc("Barista")],
                [OutputArc("Ready"), OutputArc("Recipe"), OutputArc("Barista")],
                grinder,
                max_retries=subnet_retries,
                retry_policy=RetryPolicy(delay=0.01),
            )
        ],
    )
    parent = PetriNet(
        places=[Place("Queue"), Place("Served")],
        max_workers=max_workers,
        transitions=[
            SubstitutionTransition(
                name="Cafe",
                inputs=[InputArc("Queue")],
                outputs=[OutputArc("Served")],
                action=lambda tokens: tokens,
                subnet=subnet,
                port_socket_map={"Orders": "Queue", "Ready": "Served"},
                subnet_deadline_secs=deadline,
                max_retries=0,
            )
        ],
    )
    return parent, subnet, recipe


def _drive(net: PetriNet, logical: bool) -> None:
    if logical:
        net.drive_to_quiescence()
    else:
        net.run(deadline=time.monotonic() + 10.0)


def _served(net: PetriNet) -> list[int]:
    return [t.payload["order"] for t in net.places["Served"].tokens]


def _assert_subnet_at_rest(subnet: PetriNet, recipe: Token) -> None:
    marking = subnet.marking
    assert marking["Orders"] == ()
    assert marking["Ready"] == ()
    assert marking["failed"] == ()
    assert marking["Recipe"] == (recipe,), "the subnet's own initial marking was lost or duplicated"
    assert len(marking["Barista"]) == 1, "a resource permit was lost or duplicated"


def _fail_then_serve(parent: PetriNet, subnet: PetriNet, grinder: _Grinder, recipe: Token, logical: bool) -> None:
    with parent:
        parent.deposit("Queue", Token(payload={"order": 1}))
        _drive(parent, logical)
        assert [t.payload["order"] for t in parent.marking["failed"]] == [1]
        assert _served(parent) == []
        _assert_subnet_at_rest(subnet, recipe)

        grinder.jammed = False
        parent.deposit("Queue", Token(payload={"order": 2}))
        _drive(parent, logical)
        assert _served(parent) == [2], "the next firing served a stranded order from the failed one"
        assert grinder.brewed == [2]
        _assert_subnet_at_rest(subnet, recipe)


def test_logical_subnet_over_its_tick_cap_is_purged():
    grinder = _Grinder(jammed=True)
    parent, subnet, recipe = _cafe(grinder)
    subnet.drive_to_quiescence = functools.partial(subnet.drive_to_quiescence, max_ticks=5)
    _fail_then_serve(parent, subnet, grinder, recipe, logical=True)


def test_wall_clock_subnet_past_its_deadline_is_purged():
    grinder = _Grinder(jammed=True)
    parent, subnet, recipe = _cafe(grinder, deadline=0.2)
    _fail_then_serve(parent, subnet, grinder, recipe, logical=False)


@pytest.mark.parametrize("logical", [True, False], ids=["logical", "wall"])
def test_dead_letter_inside_the_subnet_is_rolled_back_with_the_firing(logical):
    """The subnet contains the action's error itself (dead-letters the order), so the parent
    fails on the missing output. The dead letter belongs to the rolled-back firing."""
    grinder = _Grinder(jammed=True)
    parent, subnet, recipe = _cafe(grinder, subnet_retries=0)
    _fail_then_serve(parent, subnet, grinder, recipe, logical=logical)


@pytest.mark.parametrize("logical", [True, False], ids=["logical", "wall"])
def test_port_rejecting_a_deposit_is_purged(logical):
    """A port stricter than its socket raises mid-deposit, after an earlier port was filled.

    The port is stricter by schema, not colour set: hierarchical CPN requires a port and its
    socket to share a colour set, so a colour mismatch is a malformed net rather than a route."""
    recipe = Token(payload={"recipe": "flat white"})
    subnet = PetriNet(
        places=[
            Place("Orders"),
            Place("Cups", schema=lambda payload: payload.get("size") in {"small", "large"}),
            Place("Ready"),
            Place("Recipe", initial_marking=[recipe]),
            ResourcePlace("Barista", capacity=1),
        ],
        transitions=[
            Transition(
                "Brew",
                [InputArc("Orders"), InputArc("Recipe"), InputArc("Barista"), InputArc("Cups")],
                [OutputArc("Ready"), OutputArc("Recipe"), OutputArc("Barista")],
                lambda tokens: [tokens[0].evolve({"brewed": True}), tokens[1]],
            )
        ],
    )
    parent = PetriNet(
        places=[Place("Queue"), Place("CupShelf"), Place("Served")],
        max_workers=1,
        transitions=[
            SubstitutionTransition(
                name="Cafe",
                inputs=[InputArc("Queue"), InputArc("CupShelf")],
                outputs=[OutputArc("Served")],
                action=lambda tokens: tokens,
                subnet=subnet,
                port_socket_map={"Orders": "Queue", "Cups": "CupShelf", "Ready": "Served"},
                max_retries=0,
            )
        ],
    )
    with parent:
        parent.deposit("Queue", Token(payload={"order": 1}))
        parent.deposit("CupShelf", Token(payload={"size": "bucket"}))
        _drive(parent, logical)
        assert len(parent.marking["failed"]) == 2
        assert subnet.marking["Orders"] == (), "the order deposited before the rejected cup was stranded"

        parent.deposit("Queue", Token(payload={"order": 2}))
        parent.deposit("CupShelf", Token(payload={"size": "small"}))
        _drive(parent, logical)
        assert _served(parent) == [2]
        assert all(subnet.marking[name] == () for name in ("Orders", "Cups", "Ready", "failed"))
        assert subnet.marking["Recipe"] == (recipe,)


def test_rollback_after_the_subnet_finished_does_not_double_apply_its_state():
    """The subnet quiesces cleanly but the parent cannot commit (its counter takes lattes only).
    The firing rolls back, so the subnet's stamp card must not keep the stamp it added, or each
    retry would stamp it again."""

    def brew(tokens: list[Token]) -> list[Token]:
        order, card = tokens
        return [order.evolve({"brewed": True}), card.evolve({"stamps": card.payload["stamps"] + 1})]

    subnet = PetriNet(
        places=[Place("Orders"), Place("Ready"), Place("StampCard", initial_marking=[Token(payload={"stamps": 0})])],
        transitions=[
            Transition(
                "Brew",
                [InputArc("Orders"), InputArc("StampCard")],
                [OutputArc("Ready"), OutputArc("StampCard")],
                brew,
            )
        ],
    )
    parent = PetriNet(
        places=[Place("Queue"), Place("Counter", color_set={"latte"})],
        max_workers=1,
        transitions=[
            SubstitutionTransition(
                name="Cafe",
                inputs=[InputArc("Queue")],
                outputs=[OutputArc("Counter")],
                action=lambda tokens: tokens,
                subnet=subnet,
                port_socket_map={"Orders": "Queue", "Ready": "Counter"},
                max_retries=3,
            )
        ],
    )
    with parent:
        parent.deposit("Queue", Token(payload={"order": 1}))
        parent.drive_to_quiescence()
        assert [t.payload["order"] for t in parent.marking["failed"]] == [1]
        assert [t.payload["stamps"] for t in subnet.marking["StampCard"]] == [0]
        assert subnet.marking["Ready"] == ()


@pytest.mark.parametrize("logical", [True, False], ids=["logical", "wall"])
def test_parent_tokens_are_rolled_back_exactly_once(logical):
    grinder = _Grinder(jammed=True)
    parent, subnet, _ = _cafe(grinder, deadline=0.2)
    parent.transitions["Cafe"].max_retries = 1
    parent.transitions["Cafe"].retry_policy = RetryPolicy(delay=3600.0)
    if logical:
        subnet.drive_to_quiescence = functools.partial(subnet.drive_to_quiescence, max_ticks=5)
        parent.advance_time(1.0)
    original = Token(payload={"order": 1})
    with parent:
        parent.deposit("Queue", original)
        assert parent.step()
        parent._await_inflight()
        everywhere = [t for tokens in parent.marking.values() for t in tokens]
        assert len(everywhere) == 1, f"parent holds {len(everywhere)} tokens after one failed firing"
        (returned,) = parent.marking["Queue"]
        assert returned.payload == original.payload
        assert returned.attempts == 1


def test_concurrent_firings_of_one_subnet_keep_their_own_orders():
    """`max_workers > 1` lets the parent fire the same subnet from several threads. Each firing
    must collect its own order: sharing the subnet's ports duplicated some orders and lost others."""

    def slow_brew(tokens: list[Token]) -> list[Token]:
        time.sleep(0.005)
        return [tokens[0].evolve({"brewed": True})]

    subnet = PetriNet(
        places=[Place("Orders"), Place("Ready")],
        transitions=[Transition("Brew", [InputArc("Orders")], [OutputArc("Ready")], slow_brew)],
    )
    parent = PetriNet(
        places=[Place("Queue"), SinkPlace("Served", keep_last=100)],
        max_workers=4,
        transitions=[
            SubstitutionTransition(
                name="Cafe",
                inputs=[InputArc("Queue")],
                outputs=[OutputArc("Served")],
                action=lambda tokens: tokens,
                subnet=subnet,
                port_socket_map={"Orders": "Queue", "Ready": "Served"},
            )
        ],
    )
    n = 20
    with parent:
        for i in range(n):
            parent.deposit("Queue", Token(payload={"order": i}))
        parent.run(deadline=time.monotonic() + 30.0)
    served = Counter(t.payload["order"] for t in parent.places["Served"].tokens)
    assert served == Counter(range(n))


def test_brew_still_running_at_the_deadline_returns_only_its_permit():
    """An action still brewing when the deadline passes finishes after the purge. Its permit
    goes back to the pool (still cooling down: the pool is never restored from the checkpoint,
    which would duplicate it), and its order and recipe card are dropped: the purge already put
    the recipe card back, and the order belongs to the rolled-back firing."""
    release = threading.Event()

    def stuck_brew(tokens: list[Token]) -> list[Token]:
        release.wait(5.0)
        return [tokens[0].evolve({"brewed": True}), tokens[1]]

    barista = PacedResourcePlace("Barista", capacity=2, pacing_secs=60.0)
    parent, subnet, recipe = _cafe(_Grinder(), deadline=0.2, barista=barista)
    subnet.transitions["Brew"].action = stuck_brew
    with parent:
        parent.deposit("Queue", Token(payload={"order": 1}))
        parent.run(deadline=time.monotonic() + 5.0)
        assert [t.payload["order"] for t in parent.marking["failed"]] == [1]
        release.set()
        subnet.run(deadline=time.monotonic() + 5.0)
        permits = subnet.marking["Barista"]
        assert len(permits) == 2, "the purge duplicated or destroyed a permit"
        assert len({t.id for t in permits}) == 2
        cooling = [t for t in permits if t.available_at > time.monotonic()]
        assert len(cooling) == 1, "the permit the stuck brew used should still be cooling down"
        assert subnet.marking["Ready"] == (), "the late brew's output leaked into the subnet"
        assert subnet.marking["Recipe"] == (recipe,)

        parent.deposit("Queue", Token(payload={"order": 2}))
        parent.run(deadline=time.monotonic() + 5.0)
        assert _served(parent) == [2]


@pytest.mark.parametrize("logical", [True, False], ids=["logical", "wall"])
def test_token_parked_at_infinity_does_not_fail_the_firing(logical):
    """A token at `available_at = inf` never enables anything, so the subnet is in a timed dead
    marking and the firing completes, even though `is_quiescent` counts the token as pending."""

    def brew(tokens: list[Token]) -> list[Token]:
        return [tokens[0].evolve({"brewed": True}), Token(payload={"punch": 1}, available_at=math.inf)]

    subnet = PetriNet(
        places=[Place("Orders"), Place("Ready"), Place("Punches")],
        transitions=[
            Transition("Brew", [InputArc("Orders")], [OutputArc("Ready"), OutputArc("Punches")], brew),
            Transition("Redeem", [InputArc("Punches")], [OutputArc("Ready")], lambda tokens: tokens),
        ],
    )
    parent = PetriNet(
        places=[Place("Queue"), Place("Served")],
        max_workers=1,
        transitions=[
            SubstitutionTransition(
                name="Cafe",
                inputs=[InputArc("Queue")],
                outputs=[OutputArc("Served")],
                action=lambda tokens: tokens,
                subnet=subnet,
                port_socket_map={"Orders": "Queue", "Ready": "Served"},
                subnet_deadline_secs=0.2,
                max_retries=0,
            )
        ],
    )
    with parent:
        parent.deposit("Queue", Token(payload={"order": 1}))
        _drive(parent, logical)
    assert _served(parent) == [1]
    assert parent.marking["failed"] == ()
    assert len(subnet.marking["Punches"]) == 1
