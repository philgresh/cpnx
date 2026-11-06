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
    ThresholdPlace,
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


def _await_settled(net: PetriNet, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while (net._pending_restore is not None or net._running_count) and time.monotonic() < deadline:
        time.sleep(0.005)
    assert net._pending_restore is None and net._running_count == 0, "the subnet never settled"


def _collect_errors(net: PetriNet) -> list[Exception]:
    errors: list[Exception] = []
    net.on_error = lambda _name, exc, _token: errors.append(exc)
    return errors


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
    errors = _collect_errors(parent)
    _fail_then_serve(parent, subnet, grinder, recipe, logical=True)
    assert len(errors) == 1
    assert type(errors[0]) is RuntimeError
    assert "clock advance" in str(errors[0])


def test_wall_clock_subnet_past_its_deadline_is_purged():
    grinder = _Grinder(jammed=True)
    parent, subnet, recipe = _cafe(grinder, deadline=0.2)
    errors = _collect_errors(parent)
    _fail_then_serve(parent, subnet, grinder, recipe, logical=False)
    assert len(errors) == 1
    assert type(errors[0]) is TimeoutError
    assert "subnet_deadline_secs=0.2" in str(errors[0])


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


def _counter_cafe(brew, *, baristas: int, deadline: float, max_retries: int = 0) -> tuple[PetriNet, PetriNet]:
    """A cafe whose brews share nothing but the barista pool, so a straggler only holds a permit."""
    subnet = PetriNet(
        places=[Place("Orders"), Place("Ready"), ResourcePlace("Barista", capacity=baristas)],
        transitions=[
            Transition(
                "Brew", [InputArc("Orders"), InputArc("Barista")], [OutputArc("Ready"), OutputArc("Barista")], brew
            )
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
                subnet_deadline_secs=deadline,
                max_retries=max_retries,
            )
        ],
    )
    return parent, subnet


def test_place_holding_one_token_object_twice_is_restored_exactly():
    """`ResourcePlace` documents `Place(initial_marking=[Token(...)] * n)` as its CPN equivalent,
    so a place may hold the same object several times. A restore must not multiply it."""
    cup = Token(color="cup")
    cups = Place("Cups", initial_marking=[cup, cup])
    snapshot = cups.tokens
    cups.retrieve(1)
    cups.deposit(Token(color="mug"))
    assert cups._restore_tokens(snapshot)
    assert cups.tokens == (cup, cup)

    def brew(tokens: list[Token]) -> list[Token]:
        return [tokens[0].evolve({"brewed": True}), tokens[1]]

    subnet = PetriNet(
        places=[Place("Orders"), Place("Ready"), Place("Shelf", color_set={"cup"}, initial_marking=[cup, cup])],
        transitions=[
            Transition("Brew", [InputArc("Orders"), InputArc("Shelf")], [OutputArc("Ready"), OutputArc("Shelf")], brew)
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
    assert subnet.marking["Shelf"] == (cup, cup)


def _stamping_kitchen() -> tuple[SubstitutionTransition, PetriNet]:
    def brew(tokens: list[Token]) -> list[Token]:
        order, card = tokens
        return [order.evolve({"brewed": True}), card.evolve({"stamps": card.payload["stamps"] + 1})]

    kitchen = PetriNet(
        places=[Place("Tickets"), Place("Plated"), Place("StampCard", initial_marking=[Token(payload={"stamps": 0})])],
        transitions=[
            Transition(
                "Brew",
                [InputArc("Tickets"), InputArc("StampCard")],
                [OutputArc("Plated"), OutputArc("StampCard")],
                brew,
            )
        ],
    )
    station = SubstitutionTransition(
        name="Kitchen",
        inputs=[InputArc("Orders")],
        outputs=[OutputArc("Ready")],
        action=lambda tokens: tokens,
        subnet=kitchen,
        port_socket_map={"Tickets": "Orders", "Plated": "Ready"},
        max_retries=0,
    )
    return station, kitchen


def _nested_cafe(counter: Place) -> tuple[PetriNet, PetriNet, PetriNet]:
    station, kitchen = _stamping_kitchen()
    front = PetriNet(places=[Place("Orders"), Place("Ready")], transitions=[station])
    parent = PetriNet(
        places=[Place("Queue"), counter],
        max_workers=1,
        transitions=[
            SubstitutionTransition(
                name="Cafe",
                inputs=[InputArc("Queue")],
                outputs=[OutputArc(counter.name)],
                action=lambda tokens: tokens,
                subnet=front,
                port_socket_map={"Orders": "Queue", "Ready": counter.name},
                max_retries=3,
            )
        ],
    )
    return parent, front, kitchen


@pytest.mark.parametrize("logical", [True, False], ids=["logical", "wall"])
def test_rollback_reaches_a_nested_subnet(logical):
    """The front of house hands each order to a kitchen subnet, which commits and stamps its card.
    When the outer firing then rolls back, the kitchen's stamp must roll back with it."""
    parent, front, kitchen = _nested_cafe(Place("Counter", color_set={"latte"}))
    with parent:
        parent.deposit("Queue", Token(payload={"order": 1}))
        _drive(parent, logical)
    assert [t.payload["order"] for t in parent.marking["failed"]] == [1]
    assert [t.payload["stamps"] for t in kitchen.marking["StampCard"]] == [0]
    assert all(front.marking[name] == () for name in ("Orders", "Ready", "failed"))
    assert all(kitchen.marking[name] == () for name in ("Tickets", "Plated", "failed"))


@pytest.mark.parametrize("logical", [True, False], ids=["logical", "wall"])
def test_committed_nested_firings_keep_their_effects(logical):
    parent, _, kitchen = _nested_cafe(Place("Counter"))
    with parent:
        for order in (1, 2):
            parent.deposit("Queue", Token(payload={"order": order}))
            _drive(parent, logical)
    assert [t.payload["order"] for t in parent.marking["Counter"]] == [1, 2]
    assert [t.payload["stamps"] for t in kitchen.marking["StampCard"]] == [2]


def test_subnet_sink_keeps_what_a_failed_firing_absorbed():
    """A sink is never restored: it cannot enable anything, and its counters record what each
    attempt actually did."""

    def brew(tokens: list[Token]) -> list[Token]:
        return [tokens[0].evolve({"brewed": True}), Token(color="receipt")]

    receipts = SinkPlace("Receipts", keep_last=10)
    subnet = PetriNet(
        places=[Place("Orders"), Place("Ready"), receipts],
        transitions=[Transition("Brew", [InputArc("Orders")], [OutputArc("Ready"), OutputArc("Receipts")], brew)],
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
                max_retries=2,
            )
        ],
    )
    with parent:
        parent.deposit("Queue", Token(payload={"order": 1}))
        parent.drive_to_quiescence()
    assert [t.payload["order"] for t in parent.marking["failed"]] == [1]
    assert receipts.stats()["absorbed"] == 3
    with pytest.raises(TypeError):
        receipts._restore_tokens(())
    with pytest.raises(TypeError):
        ResourcePlace("Barista", capacity=1)._restore_tokens(())


def test_restored_token_alone_re_enables_its_transition():
    """The incremental scheduler only re-evaluates transitions whose input places changed. The
    espresso machine starts cold; warming it up is enabled by the cold token alone, so after a
    purge puts the cold token back, nothing but the restore can tell the scheduler."""

    class _Barista:
        jammed = True

        def __call__(self, tokens: list[Token]) -> list[Token]:
            if self.jammed:
                raise RuntimeError("milk ran out")
            return [tokens[0].evolve({"brewed": True}), tokens[1]]

    barista = _Barista()
    subnet = PetriNet(
        places=[
            Place("Orders"),
            Place("Ready"),
            Place("Cold", initial_marking=[Token(color="machine")]),
            Place("Warm"),
        ],
        transitions=[
            Transition("WarmUp", [InputArc("Cold")], [OutputArc("Warm")], lambda tokens: tokens),
            Transition(
                "Brew",
                [InputArc("Orders"), InputArc("Warm")],
                [OutputArc("Ready"), OutputArc("Warm")],
                barista,
                max_retries=0,
            ),
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
                max_retries=0,
            )
        ],
    )
    assert subnet._incremental_eligible
    with parent:
        parent.deposit("Queue", Token(payload={"order": 1}))
        parent.drive_to_quiescence()
        assert [t.payload["order"] for t in parent.marking["failed"]] == [1]
        assert len(subnet.marking["Cold"]) == 1
        assert subnet.marking["Warm"] == ()

        barista.jammed = False
        parent.deposit("Queue", Token(payload={"order": 2}))
        parent.drive_to_quiescence()
    assert _served(parent) == [2]


def test_rollback_undoes_every_nested_firing_newest_first():
    """Two orders reach the kitchen in one outer firing, so it stamps twice. Rolling back must
    restore the oldest checkpoint last, leaving the card unstamped."""
    station, kitchen = _stamping_kitchen()
    front = PetriNet(places=[Place("Orders"), Place("Ready")], transitions=[station])
    parent = PetriNet(
        places=[Place("Queue"), Place("Counter", color_set={"latte"})],
        max_workers=1,
        transitions=[
            SubstitutionTransition(
                name="Cafe",
                inputs=[InputArc("Queue", count=2)],
                outputs=[OutputArc("Counter", count=2)],
                action=lambda tokens: tokens,
                subnet=front,
                port_socket_map={"Orders": "Queue", "Ready": "Counter"},
                max_retries=0,
            )
        ],
    )
    with parent:
        parent.deposit("Queue", Token(payload={"order": 1}))
        parent.deposit("Queue", Token(payload={"order": 2}))
        parent.drive_to_quiescence()
    assert len(parent.marking["failed"]) == 2
    assert [t.payload["stamps"] for t in kitchen.marking["StampCard"]] == [0]


def test_rollback_reaches_three_levels():
    """Front of house hands orders to a kitchen, whose pastry station has the stamp card."""
    pastry, pantry = _stamping_kitchen()
    kitchen = PetriNet(places=[Place("Orders"), Place("Ready")], transitions=[pastry])
    station = SubstitutionTransition(
        name="Kitchen",
        inputs=[InputArc("Orders")],
        outputs=[OutputArc("Ready")],
        action=lambda tokens: tokens,
        subnet=kitchen,
        port_socket_map={"Orders": "Orders", "Ready": "Ready"},
        max_retries=0,
    )
    front = PetriNet(places=[Place("Orders"), Place("Ready")], transitions=[station])
    parent = PetriNet(
        places=[Place("Queue"), Place("Counter", color_set={"latte"})],
        max_workers=1,
        transitions=[
            SubstitutionTransition(
                name="Cafe",
                inputs=[InputArc("Queue")],
                outputs=[OutputArc("Counter")],
                action=lambda tokens: tokens,
                subnet=front,
                port_socket_map={"Orders": "Queue", "Ready": "Counter"},
                max_retries=2,
            )
        ],
    )
    with parent:
        parent.deposit("Queue", Token(payload={"order": 1}))
        parent.drive_to_quiescence()
    assert [t.payload["order"] for t in parent.marking["failed"]] == [1]
    assert [t.payload["stamps"] for t in pantry.marking["StampCard"]] == [0]


def _release_after(event: threading.Event, secs: float) -> None:
    timer = threading.Timer(secs, event.set)
    timer.daemon = True
    timer.start()


def test_straggler_finishes_through_its_arcs_before_the_restore():
    """A brew still running at the deadline is not cut short. Until it finishes the subnet is
    unsettled, and the parent is not quiescent. Once it finishes, its permit is back in the
    pool through its own arc (still cooling down) and the restore puts the subnet back: no
    brewed order in `Ready`, the recipe card exactly once."""
    release = threading.Event()

    def stuck_brew(tokens: list[Token]) -> list[Token]:
        release.wait(5.0)
        return [tokens[0].evolve({"brewed": True}), tokens[1]]

    barista = PacedResourcePlace("Barista", capacity=2, pacing_secs=60.0)
    parent, subnet, recipe = _cafe(_Grinder(), deadline=0.2, barista=barista)
    subnet.transitions["Brew"].action = stuck_brew
    errors = _collect_errors(parent)
    try:
        with parent:
            parent.deposit("Queue", Token(payload={"order": 1}))
            parent.run(deadline=time.monotonic() + 0.6)
            assert [t.payload["order"] for t in parent.marking["failed"]] == [1]
            assert [type(e) for e in errors] == [TimeoutError], "a busy subnet must not count as finished"
            assert subnet._pending_restore is not None
            assert not parent.is_quiescent(), "the parent reported quiescent while its subnet was still running"
            release.set()
            _await_settled(subnet)
            assert parent.is_quiescent()
            permits = subnet.marking["Barista"]
            assert len(permits) == 2 and len({t.id for t in permits}) == 2
            assert len([t for t in permits if t.available_at > time.monotonic()]) == 1
            assert subnet.marking["Ready"] == ()
            assert subnet.marking["Recipe"] == (recipe,)

            parent.deposit("Queue", Token(payload={"order": 2}))
            parent.run(deadline=time.monotonic() + 5.0)
    finally:
        release.set()
    assert _served(parent) == [2]


def test_next_firing_waits_for_the_subnet_to_settle_then_runs_clean():
    release = threading.Event()
    brewed: list[int] = []

    def brew(tokens: list[Token]) -> list[Token]:
        if tokens[0].payload["order"] == 1:
            release.wait(5.0)
        brewed.append(tokens[0].payload["order"])
        return [tokens[0].evolve({"brewed": True})]

    parent, subnet = _counter_cafe(brew, baristas=2, deadline=1.0)
    try:
        with parent:
            parent.deposit("Queue", Token(payload={"order": 1}))
            parent.run(deadline=time.monotonic() + 1.3)
            started = time.monotonic()
            _release_after(release, 0.3)
            parent.deposit("Queue", Token(payload={"order": 2}))
            parent.run(deadline=time.monotonic() + 5.0)
            waited = time.monotonic() - started
    finally:
        release.set()
    assert [t.payload["order"] for t in parent.marking["failed"]] == [1]
    assert _served(parent) == [2]
    assert brewed == [1, 2]
    assert waited >= 0.25, "order 2 started before the straggler finished"
    assert len(subnet.marking["Barista"]) == 2 and subnet.marking["Ready"] == ()


def test_firing_that_cannot_wait_out_a_straggler_fails_without_depositing():
    """With only conditional parent outputs, a firing that ran beside the straggler could have
    committed nothing and left its order behind. It now fails before depositing anything."""
    release = threading.Event()
    brewed: list[int] = []

    def brew(tokens: list[Token]) -> list[Token]:
        if tokens[0].payload["order"] == 1:
            release.wait(5.0)
        brewed.append(tokens[0].payload["order"])
        return [tokens[0].evolve({"brewed": True})]

    parent, subnet = _counter_cafe(brew, baristas=1, deadline=0.2)
    parent.add_place(Place("Rejected"))
    parent.transitions["Cafe"].outputs = [
        OutputArc.on_color(Token().color, "Served"),
        OutputArc.on_color("error", "Rejected"),
    ]
    errors = _collect_errors(parent)
    try:
        with parent:
            for order in (1, 2):
                parent.deposit("Queue", Token(payload={"order": order}))
                parent.run(deadline=time.monotonic() + 0.6)
            assert [t.payload["order"] for t in parent.marking["failed"]] == [1, 2]
            assert [type(e) for e in errors] == [TimeoutError, RuntimeError]
            assert "still running an action of a failed firing" in str(errors[1])
            assert subnet.marking["Orders"] == ()
            release.set()
            _await_settled(subnet)
            parent.deposit("Queue", Token(payload={"order": 3}))
            parent.run(deadline=time.monotonic() + 5.0)
    finally:
        release.set()
    assert _served(parent) == [3]
    assert brewed == [1, 3]


def test_two_stage_subnet_never_strands_an_order():
    """Prep moves each order to `Mid`, where Bake waits for the one oven. Order 0's bake outlives
    the deadline; order 1 must not leave its prepped order behind for order 2 to deliver."""
    release = threading.Event()

    def bake(tokens: list[Token]) -> list[Token]:
        order = next(t for t in tokens if not t.is_resource)
        if order.payload["order"] == 0:
            release.wait(5.0)
        return [Token(color="pizza", payload=dict(order.payload))]

    subnet = PetriNet(
        places=[Place("Orders"), Place("Mid"), Place("Ready"), ResourcePlace("Oven", capacity=1)],
        transitions=[
            Transition("Prep", [InputArc("Orders")], [OutputArc("Mid")], lambda tokens: tokens),
            Transition("Bake", [InputArc("Mid"), InputArc("Oven")], [OutputArc("Ready"), OutputArc("Oven")], bake),
        ],
    )
    parent = PetriNet(
        places=[Place("Queue"), Place("Served")],
        max_workers=1,
        transitions=[
            SubstitutionTransition(
                name="Kitchen",
                inputs=[InputArc("Queue")],
                outputs=[OutputArc.on_color("pizza", "Served")],
                action=lambda tokens: tokens,
                subnet=subnet,
                port_socket_map={"Orders": "Queue", "Ready": "Served"},
                subnet_deadline_secs=0.2,
                max_retries=0,
            )
        ],
    )
    try:
        with parent:
            parent.deposit("Queue", Token(payload={"order": 0}))
            parent.run(deadline=time.monotonic() + 0.5)
            _release_after(release, 0.1)
            for order in (1, 2):
                parent.deposit("Queue", Token(payload={"order": order}))
                parent.run(deadline=time.monotonic() + 5.0)
    finally:
        release.set()
    assert [t.payload["order"] for t in parent.marking["failed"]] == [0]
    assert [t.payload["order"] for t in parent.marking["Served"]] == [1, 2]
    assert all(subnet.marking[name] == () for name in ("Orders", "Mid", "Ready"))


def test_batching_port_keeps_what_committed_firings_left():
    """A committed firing keeps the state it reached, so a port that gathers two tickets per
    tray still batches across firings."""
    subnet = PetriNet(
        places=[ThresholdPlace("Tickets", threshold=2), Place("Trays")],
        transitions=[
            Transition(
                "Bake",
                [InputArc("Tickets", count=2)],
                [OutputArc("Trays")],
                lambda tokens: [Token(payload={"batch": [t.payload["order"] for t in tokens]})],
            )
        ],
    )
    parent = PetriNet(
        places=[Place("Queue"), Place("Out")],
        max_workers=1,
        transitions=[
            SubstitutionTransition(
                name="Oven",
                inputs=[InputArc("Queue")],
                outputs=[OutputArc.on_color(Token().color, "Out")],
                action=lambda tokens: tokens,
                subnet=subnet,
                port_socket_map={"Tickets": "Queue", "Trays": "Out"},
                max_retries=0,
            )
        ],
    )
    with parent:
        for order in (1, 2, 3, 4):
            parent.deposit("Queue", Token(payload={"order": order}))
            parent.drive_to_quiescence()
    assert [t.payload["batch"] for t in parent.marking["Out"]] == [(1, 2), (3, 4)]
    assert parent.marking["failed"] == ()


def test_purge_never_hands_a_held_machine_to_the_next_firing():
    """`Place(initial_marking=[machine])` is the CPN form of a one-permit pool. The next order
    waits for the straggler to give the machine back, so no two orders brew on it at once."""
    release = threading.Event()
    active = peak = 0
    lock = threading.Lock()

    def brew(tokens: list[Token]) -> list[Token]:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            if tokens[0].payload["order"] == 1:
                release.wait(5.0)
        finally:
            with lock:
                active -= 1
        return [tokens[0].evolve({"brewed": True}), tokens[1]]

    machine = Token(color="machine")
    subnet = PetriNet(
        places=[Place("Orders"), Place("Ready"), Place("Machine", color_set={"machine"}, initial_marking=[machine])],
        transitions=[
            Transition(
                "Brew", [InputArc("Orders"), InputArc("Machine")], [OutputArc("Ready"), OutputArc("Machine")], brew
            )
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
                subnet_deadline_secs=1.0,
                max_retries=0,
            )
        ],
    )
    try:
        with parent:
            parent.deposit("Queue", Token(payload={"order": 1}))
            parent.run(deadline=time.monotonic() + 1.3)
            _release_after(release, 0.2)
            parent.deposit("Queue", Token(payload={"order": 2}))
            parent.run(deadline=time.monotonic() + 5.0)
    finally:
        release.set()
    _await_settled(subnet)
    assert peak == 1
    assert _served(parent) == [2]
    assert subnet.marking["Machine"] == (machine,)


def test_nested_straggler_leaves_one_machine():
    """The kitchen's slow brew outlives the front of house's deadline. When everything has
    settled, the kitchen holds exactly one machine, in its state from before the failed firing."""

    def brew(tokens: list[Token]) -> list[Token]:
        order, machine = tokens
        time.sleep(0.6 if order.payload["order"] == 2 else 0.01)
        return [order.evolve({"brewed": True}), machine.evolve({"uses": machine.payload["uses"] + 1})]

    kitchen = PetriNet(
        places=[
            Place("Tickets"),
            Place("Plated"),
            Place("Machine", color_set={"machine"}, initial_marking=[Token(color="machine", payload={"uses": 0})]),
        ],
        transitions=[
            Transition(
                "Brew", [InputArc("Tickets"), InputArc("Machine")], [OutputArc("Plated"), OutputArc("Machine")], brew
            )
        ],
    )
    front = PetriNet(
        places=[Place("Orders"), Place("Ready")],
        max_workers=1,
        transitions=[
            SubstitutionTransition(
                name="Kitchen",
                inputs=[InputArc("Orders")],
                outputs=[OutputArc("Ready")],
                action=lambda tokens: tokens,
                subnet=kitchen,
                port_socket_map={"Tickets": "Orders", "Plated": "Ready"},
                subnet_deadline_secs=1.0,
                max_retries=0,
            )
        ],
    )
    parent = PetriNet(
        places=[Place("Queue"), Place("Served")],
        max_workers=1,
        transitions=[
            SubstitutionTransition(
                name="Cafe",
                inputs=[InputArc("Queue", count=2)],
                outputs=[OutputArc("Served", count=2)],
                action=lambda tokens: tokens,
                subnet=front,
                port_socket_map={"Orders": "Queue", "Ready": "Served"},
                subnet_deadline_secs=0.2,
                max_retries=0,
            )
        ],
    )
    with parent:
        parent.deposit("Queue", Token(payload={"order": 1}))
        parent.deposit("Queue", Token(payload={"order": 2}))
        parent.run(deadline=time.monotonic() + 5.0)
    _await_settled(front)
    _await_settled(kitchen)
    assert len(parent.marking["failed"]) == 2
    assert [t.payload for t in kitchen.marking["Machine"]] == [{"uses": 0}]
    assert all(kitchen.marking[name] == () for name in ("Tickets", "Plated"))


def test_rollback_does_not_store_a_returned_machine_twice():
    """The kitchen's straggler gives its machine back after the kitchen's own purge, while the
    front of house is still waiting on another station. The front's rollback must leave one
    machine, not replay the hand-back onto a checkpoint that already has it."""
    machine = Token(color="machine")

    def brew(tokens: list[Token]) -> list[Token]:
        order, held = tokens
        time.sleep(0.5 if order.payload["order"] == 2 else 0.01)
        return [order.evolve({"brewed": True}), held]

    kitchen = PetriNet(
        places=[Place("Tickets"), Place("Plated"), Place("Machine", color_set={"machine"}, initial_marking=[machine])],
        transitions=[
            Transition(
                "Brew", [InputArc("Tickets"), InputArc("Machine")], [OutputArc("Plated"), OutputArc("Machine")], brew
            )
        ],
    )
    front = PetriNet(
        places=[Place("Orders"), Place("Ready"), Place("Timer"), Place("Rang")],
        max_workers=2,
        transitions=[
            SubstitutionTransition(
                name="Kitchen",
                inputs=[InputArc("Orders")],
                outputs=[OutputArc("Ready")],
                action=lambda tokens: tokens,
                subnet=kitchen,
                port_socket_map={"Tickets": "Orders", "Plated": "Ready"},
                subnet_deadline_secs=0.2,
                max_retries=0,
            ),
            Transition("Wait", [InputArc("Timer")], [OutputArc("Rang")], lambda tokens: (time.sleep(0.8), tokens)[1]),
        ],
    )
    parent = PetriNet(
        places=[Place("Queue"), Place("Timer"), Place("Served")],
        max_workers=1,
        transitions=[
            SubstitutionTransition(
                name="Cafe",
                inputs=[InputArc("Queue", count=2), InputArc("Timer")],
                outputs=[OutputArc("Served", count=2)],
                action=lambda tokens: tokens,
                subnet=front,
                port_socket_map={"Orders": "Queue", "Ready": "Served", "Timer": "Timer"},
                subnet_deadline_secs=2.0,
                max_retries=0,
            )
        ],
    )
    with parent:
        parent.deposit("Queue", Token(payload={"order": 1}))
        parent.deposit("Queue", Token(payload={"order": 2}))
        parent.deposit("Timer", Token(payload={"order": 0}))
        parent.run(deadline=time.monotonic() + 5.0)
    _await_settled(front)
    _await_settled(kitchen)
    assert kitchen.marking["Machine"] == (machine,)


def test_one_slow_brew_does_not_cascade_through_a_one_worker_subnet():
    """With one worker, a firing that started while the straggler ran would spend its deadline
    queued behind it, miss, and fail the next firing in turn. It waits for the subnet instead."""
    calls: list[int] = []
    spiked = threading.Event()

    def brew(tokens: list[Token]) -> list[Token]:
        calls.append(tokens[0].payload["order"])
        if tokens[0].payload["order"] == 1 and not spiked.is_set():
            spiked.set()
            time.sleep(1.2)
        else:
            time.sleep(0.05)
        return tokens

    subnet = PetriNet(
        places=[Place("Orders"), Place("Ready")],
        max_workers=1,
        transitions=[Transition("Brew", [InputArc("Orders")], [OutputArc("Ready")], brew)],
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
                subnet_deadline_secs=1.0,
                max_retries=5,
                retry_policy=RetryPolicy(delay=0.0),
            )
        ],
    )
    errors = _collect_errors(parent)
    with parent:
        parent.deposit("Queue", Token(payload={"order": 1}))
        parent.deposit("Queue", Token(payload={"order": 2}))
        parent.run(deadline=time.monotonic() + 10.0)
    assert sorted(_served(parent)) == [1, 2]
    assert len(errors) == 1
    assert sorted(calls) == [1, 1, 2]


def test_work_queued_by_a_failed_firing_never_runs():
    """Both of a firing's orders queue on a one-worker subnet. When the firing fails, the brew
    still queued belongs to it and must not act once it gets the worker."""
    release = threading.Event()
    calls: list[int] = []

    def brew(tokens: list[Token]) -> list[Token]:
        calls.append(tokens[0].payload["order"])
        release.wait(5.0)
        return tokens

    subnet = PetriNet(
        places=[Place("Orders"), Place("Ready")],
        max_workers=1,
        transitions=[Transition("Brew", [InputArc("Orders")], [OutputArc("Ready")], brew)],
    )
    parent = PetriNet(
        places=[Place("Queue"), Place("Served")],
        max_workers=1,
        transitions=[
            SubstitutionTransition(
                name="Cafe",
                inputs=[InputArc("Queue", count=2)],
                outputs=[OutputArc("Served", count=2)],
                action=lambda tokens: tokens,
                subnet=subnet,
                port_socket_map={"Orders": "Queue", "Ready": "Served"},
                subnet_deadline_secs=0.2,
                max_retries=0,
            )
        ],
    )
    try:
        with parent:
            parent.deposit("Queue", Token(payload={"order": 1}))
            parent.deposit("Queue", Token(payload={"order": 2}))
            parent.run(deadline=time.monotonic() + 0.5)
    finally:
        release.set()
    _await_settled(subnet)
    assert len(calls) == 1
    assert subnet.marking["Orders"] == () and subnet.marking["Ready"] == ()


def test_subnet_with_unbounded_workers_fires():
    subnet = PetriNet(
        places=[Place("Orders"), Place("Ready")],
        max_workers=None,
        transitions=[Transition("Brew", [InputArc("Orders")], [OutputArc("Ready")], lambda tokens: tokens)],
    )
    parent = PetriNet(
        places=[Place("Queue"), Place("Served")],
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
    with parent:
        parent.deposit("Queue", Token(payload={"order": 1}))
        parent.run(deadline=time.monotonic() + 5.0)
    assert _served(parent) == [1]


def _stamping_front(
    brew, *, kitchen_deadline: float, front_deadline: float, orders: int
) -> tuple[PetriNet, PetriNet, PetriNet]:
    """A parent whose front of house hands orders to a kitchen subnet with a stamp card."""
    kitchen = PetriNet(
        places=[Place("Tickets"), Place("Plated"), Place("StampCard", initial_marking=[Token(payload={"stamps": 0})])],
        transitions=[
            Transition(
                "Brew",
                [InputArc("Tickets"), InputArc("StampCard")],
                [OutputArc("Plated"), OutputArc("StampCard")],
                brew,
            )
        ],
    )
    front = PetriNet(
        places=[Place("Orders"), Place("Ready")],
        max_workers=1,
        transitions=[
            SubstitutionTransition(
                name="Kitchen",
                inputs=[InputArc("Orders")],
                outputs=[OutputArc("Ready")],
                action=lambda tokens: tokens,
                subnet=kitchen,
                port_socket_map={"Tickets": "Orders", "Plated": "Ready"},
                subnet_deadline_secs=kitchen_deadline,
                max_retries=0,
            )
        ],
    )
    parent = PetriNet(
        places=[Place("Queue"), Place("Counter", color_set={"latte"})],
        max_workers=1,
        transitions=[
            SubstitutionTransition(
                name="Cafe",
                inputs=[InputArc("Queue", count=orders)],
                outputs=[OutputArc("Counter", count=orders)],
                action=lambda tokens: tokens,
                subnet=front,
                port_socket_map={"Orders": "Queue", "Ready": "Counter"},
                subnet_deadline_secs=front_deadline,
                max_retries=0,
            )
        ],
    )
    return parent, front, kitchen


def _stamp(tokens: list[Token]) -> list[Token]:
    order, card = tokens
    return [order.evolve({"brewed": True}), card.evolve({"stamps": card.payload["stamps"] + 1})]


def test_nested_firing_that_commits_after_the_outer_failure_is_undone():
    """The kitchen's only brew outlives the front of house's deadline but not its own, so it
    commits a stamp after the outer firing has failed. The outer restore must undo it."""

    def brew(tokens: list[Token]) -> list[Token]:
        time.sleep(0.5)
        return _stamp(tokens)

    parent, front, kitchen = _stamping_front(brew, kitchen_deadline=2.0, front_deadline=0.2, orders=1)
    with parent:
        parent.deposit("Queue", Token(payload={"order": 1}))
        parent.run(deadline=time.monotonic() + 5.0)
    _await_settled(front)
    assert [t.payload["order"] for t in parent.marking["failed"]] == [1]
    assert [t.payload["stamps"] for t in kitchen.marking["StampCard"]] == [0]


def test_outer_restore_waits_for_a_nested_subnet_to_settle():
    """Order 1's brew commits a stamp; order 2's brew is still holding the card when both the
    kitchen and the front of house fail. The front's rollback to before order 1 must come after
    the kitchen's own restore, or the kitchen's restore would bring order 1's stamp back."""
    release = threading.Event()

    def brew(tokens: list[Token]) -> list[Token]:
        if tokens[0].payload["order"] == 2:
            release.wait(5.0)
        return _stamp(tokens)

    parent, front, kitchen = _stamping_front(brew, kitchen_deadline=0.2, front_deadline=0.5, orders=2)
    try:
        with parent:
            parent.deposit("Queue", Token(payload={"order": 1}))
            parent.deposit("Queue", Token(payload={"order": 2}))
            parent.run(deadline=time.monotonic() + 0.8)
            assert front._pending_restore is not None and kitchen._pending_restore is not None
            release.set()
            _await_settled(kitchen)
            _await_settled(front)
    finally:
        release.set()
    assert len(parent.marking["failed"]) == 2
    assert [t.payload["stamps"] for t in kitchen.marking["StampCard"]] == [0]


def test_net_with_an_unsettled_subnet_is_not_finished():
    _, front, kitchen = _stamping_front(_stamp, kitchen_deadline=1.0, front_deadline=1.0, orders=1)
    assert front._is_finished()
    kitchen._pending_restore = (kitchen._checkpoint_marking(), [])
    assert not front._is_finished()
    kitchen._pending_restore = None
