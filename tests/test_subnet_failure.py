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
import gc
import itertools
import math
import threading
import time
import weakref
from collections import Counter
from fractions import Fraction

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


def _wait_until_settled(net: PetriNet, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while (net._pending_restore is not None or net._running_count) and time.monotonic() < deadline:
        time.sleep(0.005)
    assert net._pending_restore is None and net._running_count == 0, "the subnet never settled"


def _cap_drive(net: PetriNet, **limits: int) -> None:
    """Hold *net*'s subnet drive to *limits*, over whatever its parent passes down."""
    drive = net.drive_to_quiescence
    net.drive_to_quiescence = lambda **passed: drive(**{**passed, **limits})


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
    _cap_drive(subnet, max_ticks=5)
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


def _hold_until_aborted(net: PetriNet, timeout: float = 5.0) -> None:
    """Block an action until a firing enclosing *net* has failed."""
    give_up = time.monotonic() + timeout
    while not net._aborted():
        assert time.monotonic() < give_up, "the enclosing firing never failed"
        time.sleep(0.005)


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
            _wait_until_settled(subnet)
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
            _wait_until_settled(subnet)
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
                subnet_deadline_secs=1.0,
                max_retries=0,
            )
        ],
    )
    try:
        with parent:
            parent.deposit("Queue", Token(payload={"order": 0}))
            parent.run(deadline=time.monotonic() + 1.3)
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
    _wait_until_settled(subnet)
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
    _wait_until_settled(front)
    _wait_until_settled(kitchen)
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
    _wait_until_settled(front)
    _wait_until_settled(kitchen)
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
            _hold_until_aborted(subnet)
            time.sleep(0.2)
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
        places=[Place("Orders"), Place("Ready"), SinkPlace("Spoiled", keep_last=10)],
        max_workers=1,
        error_place="Spoiled",
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
    subnet_errors = _collect_errors(subnet)
    dead_letters: list[Token] = []
    subnet.on_token_dead_lettered = lambda _name, token: dead_letters.append(token)
    try:
        with parent:
            parent.deposit("Queue", Token(payload={"order": 1}))
            parent.deposit("Queue", Token(payload={"order": 2}))
            parent.run(deadline=time.monotonic() + 0.5)
    finally:
        release.set()
    _wait_until_settled(subnet)
    assert len(calls) == 1
    assert subnet_errors == [] and dead_letters == [], "a firing that never started reported a failure"
    assert subnet.places["Spoiled"].stats()["absorbed"] == 0
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
    _wait_until_settled(front)
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
            _wait_until_settled(kitchen)
            _wait_until_settled(front)
    finally:
        release.set()
    assert len(parent.marking["failed"]) == 2
    assert [t.payload["stamps"] for t in kitchen.marking["StampCard"]] == [0]


def test_net_with_an_unsettled_subnet_is_not_finished():
    _, front, kitchen = _stamping_front(_stamp, kitchen_deadline=1.0, front_deadline=1.0, orders=1)
    assert front._is_finished()
    kitchen._pending_restore = (kitchen._checkpoint_marking(), [], time.monotonic())
    assert not front._is_finished()
    kitchen._pending_restore = None


def test_enclosing_failure_stops_new_work_two_levels_down():
    """The front of house times out while the kitchen is still on its first stage. The kitchen's
    own deadline is longer, but its next stage (charging the card) must not start for the failed
    order, or the parent's retry charges it a second time."""
    charges: list[int] = []
    slow = threading.Event()

    def prep(tokens: list[Token]) -> list[Token]:
        if not slow.is_set():
            slow.set()
            _hold_until_aborted(kitchen)
        return tokens

    def charge(tokens: list[Token]) -> list[Token]:
        charges.append(tokens[0].payload["order"])
        return tokens

    kitchen = PetriNet(
        places=[Place("Orders"), Place("Prepped"), Place("Ready")],
        transitions=[
            Transition("Prep", [InputArc("Orders")], [OutputArc("Prepped")], prep),
            Transition("Charge", [InputArc("Prepped")], [OutputArc("Ready")], charge),
        ],
    )
    front = PetriNet(
        places=[Place("Orders"), Place("Ready")],
        transitions=[
            SubstitutionTransition(
                name="Kitchen",
                inputs=[InputArc("Orders")],
                outputs=[OutputArc("Ready")],
                action=lambda tokens: tokens,
                subnet=kitchen,
                port_socket_map={"Orders": "Orders", "Ready": "Ready"},
                subnet_deadline_secs=2.0,
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
                subnet=front,
                port_socket_map={"Orders": "Queue", "Ready": "Served"},
                subnet_deadline_secs=0.2,
                max_retries=2,
                retry_policy=RetryPolicy(delay=0.0),
            )
        ],
    )
    with parent:
        parent.deposit("Queue", Token(payload={"order": 1}))
        parent.run(deadline=time.monotonic() + 5.0)
    assert _served(parent) == [1]
    assert charges == [1]


def test_logical_drive_stops_new_work_two_levels_down():
    """The same order on the logical clock: the front of house gives up on the kitchen's first
    stage by its spin bound, and the kitchen must not go on to charge the card."""
    charges: list[int] = []
    slow = threading.Event()

    def prep(tokens: list[Token]) -> list[Token]:
        if not slow.is_set():
            slow.set()
            _hold_until_aborted(kitchen)
        return tokens

    def charge(tokens: list[Token]) -> list[Token]:
        charges.append(tokens[0].payload["order"])
        return tokens

    kitchen = PetriNet(
        places=[Place("Orders"), Place("Prepped"), Place("Ready")],
        transitions=[
            Transition("Prep", [InputArc("Orders")], [OutputArc("Prepped")], prep),
            Transition("Charge", [InputArc("Prepped")], [OutputArc("Ready")], charge),
        ],
    )
    front = PetriNet(
        places=[Place("Orders"), Place("Ready")],
        transitions=[
            SubstitutionTransition(
                name="Kitchen",
                inputs=[InputArc("Orders")],
                outputs=[OutputArc("Ready")],
                action=lambda tokens: tokens,
                subnet=kitchen,
                port_socket_map={"Orders": "Orders", "Ready": "Ready"},
            )
        ],
    )
    first_drive = itertools.count()
    drive_front = front.drive_to_quiescence
    front.drive_to_quiescence = lambda **limits: drive_front(
        **{**limits, "max_spins": 2000 if next(first_drive) == 0 else 10_000_000}
    )
    parent = PetriNet(
        places=[Place("Queue"), Place("Served")],
        transitions=[
            SubstitutionTransition(
                name="Cafe",
                inputs=[InputArc("Queue")],
                outputs=[OutputArc("Served")],
                action=lambda tokens: tokens,
                subnet=front,
                port_socket_map={"Orders": "Queue", "Ready": "Served"},
                max_retries=2,
                retry_policy=RetryPolicy(delay=0.0),
            )
        ],
    )
    parent.deposit("Queue", Token(payload={"order": 1}))
    done = threading.Event()

    def drive() -> None:
        try:
            parent.drive_to_quiescence()
        finally:
            done.set()

    threading.Thread(target=drive, daemon=True).start()
    assert done.wait(10.0)
    assert _served(parent) == [1]
    assert charges == [1]


def test_restore_runs_even_if_the_parent_rollback_raises():
    """The parent's error place rejects the dead letter, so its rollback raises. The subnet must
    still be restored."""

    def burn(tokens: list[Token]) -> list[Token]:
        raise ValueError("burnt")

    subnet = PetriNet(
        places=[Place("Orders"), Place("Ground"), Place("Ready")],
        error_place="Spoiled",
        transitions=[
            Transition("Grind", [InputArc("Orders")], [OutputArc("Ground")], lambda tokens: tokens),
            Transition("Brew", [InputArc("Ground")], [OutputArc("Ready")], burn, max_retries=0),
        ],
    )
    parent = PetriNet(
        places=[Place("Queue"), Place("Served"), Place("Rejected", color_set={"error"})],
        error_place="Rejected",
        transitions=[
            SubstitutionTransition(
                name="Cafe",
                inputs=[InputArc("Queue")],
                outputs=[OutputArc("Served")],
                action=lambda tokens: tokens,
                subnet=subnet,
                port_socket_map={"Orders": "Queue", "Ready": "Served"},
                subnet_deadline_secs=0.5,
                max_retries=0,
            )
        ],
    )
    with parent:
        parent.deposit("Queue", Token(payload={"order": 1}))
        parent.run(deadline=time.monotonic() + 2.0)
    _wait_until_settled(subnet)
    assert all(tokens == () for tokens in subnet.marking.values())
    assert subnet._nested_undo is None


def test_parent_and_subnet_are_freed_without_a_gc_pass():
    gc.disable()
    try:
        subnet = PetriNet(places=[Place("Orders"), Place("Ready")])
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
        refs = weakref.ref(parent), weakref.ref(subnet)
        del parent, subnet
        assert [ref() for ref in refs] == [None, None], "the subnet's link to its parent formed a cycle"
    finally:
        gc.enable()


def test_two_settle_attempts_restore_only_once():
    """The failed firing and the last straggler can both try to settle at once. Only one may
    restore, or the second could wipe a new firing's deposits."""
    _, front, kitchen = _stamping_front(_stamp, kitchen_deadline=1.0, front_deadline=1.0, orders=1)
    entered = threading.Event()
    release = threading.Event()
    restores: list[int] = []
    original = kitchen._restore_marking

    def slow_restore(checkpoint):
        restores.append(1)
        entered.set()
        release.wait(5.0)
        original(checkpoint)

    kitchen._restore_marking = slow_restore
    kitchen._pending_restore = (kitchen._checkpoint_marking(), [], time.monotonic())
    first = threading.Thread(target=kitchen._try_settle)
    first.start()
    assert entered.wait(5.0)
    second = threading.Thread(target=kitchen._try_settle)
    second.start()
    second.join(0.2)
    release.set()
    first.join(5.0)
    second.join(5.0)
    assert restores == [1]
    assert kitchen._pending_restore is None


def test_queued_work_does_not_start_while_the_drive_is_judged():
    """The first brew outlives the deadline; the second is queued behind it on one worker. The
    first brew is released only once the check that judges the drive has begun, and the check
    waits for the second brew to be decided, so the second brew always starts inside the window.
    It must still not run."""
    calls: list[int] = []
    release = threading.Event()

    def brew(tokens: list[Token]) -> list[Token]:
        calls.append(tokens[0].payload["order"])
        if tokens[0].payload["order"] == 1:
            release.wait(5.0)
        return tokens

    subnet = PetriNet(
        places=[Place("Orders"), Place("Ready")],
        max_workers=1,
        transitions=[Transition("Brew", [InputArc("Orders")], [OutputArc("Ready")], brew)],
    )
    judged = subnet._is_finished

    def slow_verdict() -> bool:
        verdict = judged()
        if not subnet._closing:
            return verdict
        release.set()
        deadline = time.monotonic() + 5.0
        while subnet._running_count and time.monotonic() < deadline:
            time.sleep(0.005)
        return verdict

    subnet._is_finished = slow_verdict
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
            parent.run(deadline=time.monotonic() + 5.0)
    finally:
        release.set()
    _wait_until_settled(subnet)
    assert calls == [1]


def test_unstarted_brew_does_not_burn_a_paced_permit():
    """Each order splits into two jobs on a 3-barista paced pool. Order 1's first job outlives the
    deadline, so its second job never starts. That job must not leave a barista cooling down, or
    order 2 finds one barista where it needs two."""
    calls: list[tuple[int, int]] = []

    def split(tokens: list[Token]) -> list[Token]:
        order = tokens[0].payload["order"]
        return [Token(payload={"order": order, "job": 1}), Token(payload={"order": order, "job": 2})]

    def brew(tokens: list[Token]) -> list[Token]:
        job = next(t for t in tokens if not t.is_resource)
        calls.append((job.payload["order"], job.payload["job"]))
        time.sleep(0.8 if job.payload == {"order": 1, "job": 1} else 0.01)
        return [job]

    baristas = PacedResourcePlace("Barista", capacity=3, pacing_secs=5.0)
    subnet = PetriNet(
        places=[Place("Orders"), Place("Jobs"), Place("Done"), Place("Ready"), baristas],
        max_workers=1,
        transitions=[
            Transition("Split", [InputArc("Orders")], [OutputArc("Jobs", count=2)], split),
            Transition(
                "Brew", [InputArc("Jobs"), InputArc("Barista")], [OutputArc("Done"), OutputArc("Barista")], brew
            ),
            Transition(
                "Join",
                [InputArc("Done", count=2)],
                [OutputArc("Ready")],
                lambda tokens: [Token(payload={"order": tokens[0].payload["order"]})],
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
                subnet_deadline_secs=0.5,
                max_retries=0,
            )
        ],
    )
    with parent:
        parent.deposit("Queue", Token(payload={"order": 1}))
        parent.run(deadline=time.monotonic() + 3.0)
        now = time.monotonic()
        assert calls == [(1, 1)]
        assert sum(1 for t in baristas.tokens if t.available_at > now) == 1
        parent.deposit("Queue", Token(payload={"order": 2}))
        parent.run(deadline=time.monotonic() + 3.0)
    assert _served(parent) == [2]


def test_logical_drive_returns_only_once_its_subnet_has_settled():
    """The subnet's own drive gives up on a slow brew, so the firing fails with the brew still
    running. The parent's drive must not return until that brew has finished and the subnet is
    restored."""
    release = threading.Event()
    released: list[bool] = []

    def slow(tokens: list[Token]) -> list[Token]:
        released.append(release.wait(5.0))
        return tokens

    subnet = PetriNet(
        places=[Place("Orders"), Place("Ready")],
        transitions=[Transition("Brew", [InputArc("Orders")], [OutputArc("Ready")], slow)],
    )
    _cap_drive(subnet, max_spins=2000)
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
                max_retries=0,
            )
        ],
    )
    await_settled = parent._await_subnets_settled

    def release_then_await(**kwargs) -> None:
        release.set()
        await_settled(**kwargs)

    parent._await_subnets_settled = release_then_await
    parent.deposit("Queue", Token(payload={"order": 1}))
    try:
        parent.drive_to_quiescence()
        assert subnet._pending_restore is None
        assert parent.is_quiescent()
    finally:
        release.set()
    assert released == [True]
    assert [t.payload["order"] for t in parent.marking["failed"]] == [1]
    assert all(tokens == () for tokens in subnet.marking.values())


@pytest.mark.parametrize("incremental", [True, False], ids=["incremental", "full-scan"])
@pytest.mark.parametrize("logical", [True, False], ids=["logical", "wall"])
def test_priority_decides_between_a_busy_subnet_and_a_competing_consumer(logical, incremental):
    """The kitchen (priority 1) and the overflow counter (priority 10) both take from the queue.
    A kitchen firing in progress must not hand the next orders to the lower-priority overflow."""
    subnet = PetriNet(
        places=[Place("Orders"), Place("Ready")],
        max_workers=1,
        seed=7,
        transitions=[Transition("Cook", [InputArc("Orders")], [OutputArc("Ready")], lambda tokens: tokens)],
    )
    parent = PetriNet(
        places=[Place("Queue"), Place("Served"), Place("Overflow")],
        max_workers=1,
        seed=7,
        transitions=[
            SubstitutionTransition(
                name="Kitchen",
                inputs=[InputArc("Queue")],
                outputs=[OutputArc("Served")],
                action=lambda tokens: tokens,
                subnet=subnet,
                port_socket_map={"Orders": "Queue", "Ready": "Served"},
                priority=1,
            ),
            Transition(
                "SendToOverflow", [InputArc("Queue")], [OutputArc("Overflow")], lambda tokens: tokens, priority=10
            ),
        ],
    )
    if not incremental:
        parent.add_place(PacedResourcePlace("Bell", capacity=1, pacing_secs=0.0))
    assert parent._incremental_eligible is incremental
    with parent:
        for order in range(50):
            parent.deposit("Queue", Token(payload={"order": order}))
        _drive(parent, logical)
    assert len(parent.marking["Served"]) == 50
    assert parent.marking["Overflow"] == ()


def _three_level_cafe(stage, *, outer_deadline: float, inner_deadline: float, middle_workers: int = 1, orders: int = 1):
    """Front of house (outer) -> kitchen (middle, with a sink for dead letters) -> station (inner)."""
    station = PetriNet(
        places=[Place("Orders"), Place("Ready")],
        transitions=[Transition("Stage", [InputArc("Orders")], [OutputArc("Ready")], stage)],
    )
    kitchen = PetriNet(
        places=[Place("Orders"), Place("Ready"), SinkPlace("Spoiled", keep_last=10)],
        error_place="Spoiled",
        max_workers=middle_workers,
        transitions=[
            SubstitutionTransition(
                name="Station",
                inputs=[InputArc("Orders")],
                outputs=[OutputArc("Ready")],
                action=lambda tokens: tokens,
                subnet=station,
                port_socket_map={"Orders": "Orders", "Ready": "Ready"},
                subnet_deadline_secs=inner_deadline,
                max_retries=0,
            )
        ],
    )
    front = PetriNet(
        places=[Place("Queue"), Place("Served")],
        max_workers=1,
        transitions=[
            SubstitutionTransition(
                name="Kitchen",
                inputs=[InputArc("Queue", count=orders)],
                outputs=[OutputArc("Served", count=orders)],
                action=lambda tokens: tokens,
                subnet=kitchen,
                port_socket_map={"Orders": "Queue", "Ready": "Served"},
                subnet_deadline_secs=outer_deadline,
                max_retries=0,
            )
        ],
    )
    return front, kitchen, station


def test_nested_firing_cut_short_by_an_enclosing_failure_reports_nothing():
    """The front of house gives up at 0.2 s while the kitchen's station is still on its first
    stage, under its own 2 s deadline. The station's firing is cut short because of the outer
    failure, not its own, so the kitchen must not report a timeout or dead-letter the order."""
    slow = threading.Event()

    def stage(tokens: list[Token]) -> list[Token]:
        if not slow.is_set():
            slow.set()
            time.sleep(0.5)
        return tokens

    front, kitchen, station = _three_level_cafe(stage, outer_deadline=0.2, inner_deadline=2.0)
    kitchen_errors = _collect_errors(kitchen)
    dead_letters: list[Token] = []
    kitchen.on_token_dead_lettered = lambda _name, token: dead_letters.append(token)
    with front:
        front.deposit("Queue", Token(payload={"order": 1}))
        front.run(deadline=time.monotonic() + 5.0)
    _wait_until_settled(kitchen)
    _wait_until_settled(station)
    assert [t.payload["order"] for t in front.marking["failed"]] == [1]
    assert kitchen_errors == [] and dead_letters == []
    assert kitchen.places["Spoiled"].stats()["absorbed"] == 0


def test_firing_waiting_to_settle_reports_nothing_once_its_net_is_aborted():
    """The kitchen's first station firing has a straggler, so its second waits for the station
    to settle. The front of house fails meanwhile; the waiting firing belongs to that failure
    and must not later report its own timeout."""
    slow = threading.Event()

    def stage(tokens: list[Token]) -> list[Token]:
        if not slow.is_set():
            slow.set()
            time.sleep(1.5)
        return tokens

    front, kitchen, station = _three_level_cafe(
        stage, outer_deadline=0.6, inner_deadline=0.4, middle_workers=2, orders=2
    )
    kitchen_errors = _collect_errors(kitchen)
    with front:
        front.deposit("Queue", Token(payload={"order": "a"}))
        front.deposit("Queue", Token(payload={"order": "b"}))
        front.run(deadline=time.monotonic() + 5.0)
    _wait_until_settled(kitchen)
    _wait_until_settled(station)
    assert [type(e) for e in kitchen_errors] == [TimeoutError], "only the station's own timeout is real"
    assert kitchen.places["Spoiled"].stats()["absorbed"] == 1


def test_a_subnet_cannot_enclose_its_own_parent():
    front = PetriNet(places=[Place("Queue"), Place("Served")])
    kitchen = PetriNet(places=[Place("Queue"), Place("Served")])

    def wrap(outer: PetriNet, inner: PetriNet, name: str) -> None:
        outer.add_transition(
            SubstitutionTransition(
                name=name,
                inputs=[InputArc("Queue")],
                outputs=[OutputArc("Served")],
                action=lambda tokens: tokens,
                subnet=inner,
                port_socket_map={"Queue": "Queue", "Served": "Served"},
            )
        )

    wrap(front, kitchen, "Kitchen")
    with pytest.raises(ValueError, match="enclosing it"):
        wrap(kitchen, front, "Front")
    assert "Front" not in kitchen.transitions
    lonely = PetriNet(places=[Place("Queue"), Place("Served")])
    with pytest.raises(ValueError, match="this net"):
        wrap(lonely, lonely, "Self")
    assert "Self" not in lonely.transitions


def test_one_kitchen_cannot_serve_two_front_counters():
    """Adding the same substitution transition to a second net would re-point its subnet's
    parent link, so aborts and settle signals would reach only the last net."""
    kitchen = PetriNet(places=[Place("Queue"), Place("Served")])
    station = SubstitutionTransition(
        name="Kitchen",
        inputs=[InputArc("Queue")],
        outputs=[OutputArc("Served")],
        action=lambda tokens: tokens,
        subnet=kitchen,
        port_socket_map={"Queue": "Queue", "Served": "Served"},
    )
    first = PetriNet(places=[Place("Queue"), Place("Served")], transitions=[station])
    second = PetriNet(places=[Place("Queue"), Place("Served")])
    with pytest.raises(ValueError, match="another net"):
        second.add_transition(station)
    assert kitchen._parent_net() is first
    assert "Kitchen" not in second.transitions


def test_replacing_a_kitchen_by_name_releases_its_subnet():
    kitchen = PetriNet(places=[Place("Queue"), Place("Served")])
    station = SubstitutionTransition(
        name="Kitchen",
        inputs=[InputArc("Queue")],
        outputs=[OutputArc("Served")],
        action=lambda tokens: tokens,
        subnet=kitchen,
        port_socket_map={"Queue": "Queue", "Served": "Served"},
    )
    first = PetriNet(places=[Place("Queue"), Place("Served")], transitions=[station])
    first.add_transition(Transition("Kitchen", [InputArc("Queue")], [OutputArc("Served")], lambda tokens: tokens))
    assert kitchen._parent_net() is None and kitchen not in first._subnets
    second = PetriNet(places=[Place("Queue"), Place("Served")], transitions=[station])
    assert kitchen._parent_net() is second
    del second.transitions["Kitchen"]
    third = PetriNet(places=[Place("Queue"), Place("Served")], transitions=[station])
    assert kitchen._parent_net() is third
    kitchen._pending_restore = (kitchen._checkpoint_marking(), [], time.monotonic())
    assert second.is_quiescent() and not third.is_quiescent()
    kitchen._pending_restore = None


def test_a_wrapped_subnet_cannot_be_swapped():
    kitchen = PetriNet(places=[Place("Queue"), Place("Served")])
    station = SubstitutionTransition(
        name="Kitchen",
        inputs=[InputArc("Queue")],
        outputs=[OutputArc("Served")],
        action=lambda tokens: tokens,
        subnet=kitchen,
        port_socket_map={"Queue": "Queue", "Served": "Served"},
    )
    with pytest.raises(AttributeError, match="cannot be reassigned"):
        station.subnet = PetriNet(places=[Place("Queue"), Place("Served")])
    assert station.subnet is kitchen


@pytest.mark.parametrize("logical", [True, False], ids=["logical", "wall"])
def test_unbounded_deadline_serves_on_both_clocks(logical):
    """`subnet_deadline_secs=None` removes the bound on the drive and on the settle wait."""
    subnet = PetriNet(
        places=[Place("Orders"), Place("Ready")],
        transitions=[Transition("Brew", [InputArc("Orders")], [OutputArc("Ready")], lambda tokens: tokens)],
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
                subnet_deadline_secs=None,
                max_retries=0,
            )
        ],
    )
    errors = _collect_errors(parent)
    with parent:
        parent.deposit("Queue", Token(payload={"order": 1}))
        _drive(parent, logical)
    assert _served(parent) == [1] and errors == []


def test_logical_drive_raises_when_a_subnet_will_not_settle_in_time():
    """The subnet's drive gives up at once, so the firing fails with a brew still running. The
    brew is held until after the assertion, so the parent's settle wait must run out.

    The drive's one `max_spins` also caps its wait for that firing to fail, which needs the
    subnet's thread to be scheduled; on a loaded machine that can take any number of spins. That
    wait is left at its default cap, so only the settle wait under test is held to 1,000 spins
    and nothing here depends on how long a spin takes."""
    release = threading.Event()

    def slow(tokens: list[Token]) -> list[Token]:
        release.wait(60.0)
        return tokens

    subnet = PetriNet(
        places=[Place("Orders"), Place("Ready")],
        transitions=[Transition("Brew", [InputArc("Orders")], [OutputArc("Ready")], slow)],
    )
    _cap_drive(subnet, max_spins=0)
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
                max_retries=0,
            )
        ],
    )
    await_inflight = parent._await_inflight
    parent._await_inflight = lambda **_capped: await_inflight()
    parent.deposit("Queue", Token(payload={"order": 1}))
    try:
        with pytest.raises(RuntimeError, match="still settling"):
            parent.drive_to_quiescence(max_spins=1_000)
    finally:
        release.set()
    _wait_until_settled(subnet)


def test_serialized_firings_report_their_own_duration():
    """Firings of one substitution transition run one at a time; the time a firing spends queued
    behind the others is not its duration."""
    durations: list[float] = []

    def cook(tokens: list[Token]) -> list[Token]:
        time.sleep(0.2)
        return tokens

    subnet = PetriNet(
        places=[Place("Orders"), Place("Ready")],
        transitions=[Transition("Cook", [InputArc("Orders")], [OutputArc("Ready")], cook)],
    )
    parent = PetriNet(
        places=[Place("Queue"), Place("Served")],
        max_workers=4,
        transitions=[
            SubstitutionTransition(
                name="Kitchen",
                inputs=[InputArc("Queue")],
                outputs=[OutputArc("Served")],
                action=lambda tokens: tokens,
                subnet=subnet,
                port_socket_map={"Orders": "Queue", "Ready": "Served"},
            )
        ],
    )
    parent.on_transition_fired = lambda _name, secs: durations.append(secs)
    with parent:
        for order in range(4):
            parent.deposit("Queue", Token(payload={"order": order}))
        parent.run(deadline=time.monotonic() + 5.0)
    assert len(durations) == 4
    assert max(durations) < 0.5, "a firing's duration included time queued behind the others"


@pytest.mark.parametrize("logical", [True, False], ids=["logical", "wall"])
def test_failure_before_the_drive_is_reported_not_lost(logical):
    """Taking the checkpoint fails before the subnet is driven. The order must still be rolled
    back and dead-lettered with the error, as for any failure."""
    subnet = PetriNet(
        places=[Place("Orders"), Place("Ready")],
        transitions=[Transition("Brew", [InputArc("Orders")], [OutputArc("Ready")], lambda tokens: tokens)],
    )

    def broken_checkpoint():
        raise OSError("checkpoint unavailable")

    subnet._checkpoint_marking = broken_checkpoint
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
    errors = _collect_errors(parent)
    with parent:
        parent.deposit("Queue", Token(payload={"order": 1}))
        _drive(parent, logical)
    assert [t.payload["order"] for t in parent.marking["failed"]] == [1]
    assert [type(e) for e in errors] == [OSError]
    assert subnet.marking["Orders"] == ()


@pytest.mark.parametrize(
    ("deadline", "error"),
    [(0, ValueError), (-1.0, ValueError), (float("nan"), ValueError), ("soon", TypeError), (True, TypeError)],
)
def test_deadline_must_be_positive_or_none(deadline, error):
    subnet = PetriNet(places=[Place("Orders"), Place("Ready")])
    with pytest.raises(error, match="positive number or None"):
        SubstitutionTransition(
            name="Cafe",
            inputs=[InputArc("Queue")],
            outputs=[OutputArc("Served")],
            action=lambda tokens: tokens,
            subnet=subnet,
            port_socket_map={"Orders": "Queue", "Ready": "Served"},
            subnet_deadline_secs=deadline,
        )
    assert subnet not in SubstitutionTransition._mapped_subnets


@pytest.mark.parametrize("deadline", [None, 5.0], ids=["unbounded", "bounded"])
@pytest.mark.parametrize("logical", [True, False], ids=["logical", "wall"])
def test_a_subnet_held_by_back_pressure_is_served_without_waiting_out_its_deadline(logical, deadline):
    """The kitchen's tray is full, so `Jam` stays enabled but can never fire: the kitchen is
    finished without ever being quiescent. The drive must return once it has finished."""
    kitchen = PetriNet(
        places=[
            Place("Orders"),
            Place("Ready"),
            Place("Stuck", initial_marking=[Token(payload={"jam": 1})]),
            Place("Tray", bound=1, initial_marking=[Token(payload={"tray": 1})]),
        ],
        transitions=[
            Transition("Serve", [InputArc("Orders")], [OutputArc("Ready")], lambda tokens: tokens),
            Transition("Jam", [InputArc("Stuck")], [OutputArc("Tray")], lambda tokens: tokens),
        ],
    )
    parent = PetriNet(
        places=[Place("Queue"), Place("Served")],
        transitions=[
            SubstitutionTransition(
                name="Cafe",
                inputs=[InputArc("Queue")],
                outputs=[OutputArc("Served")],
                action=lambda tokens: tokens,
                subnet=kitchen,
                port_socket_map={"Orders": "Queue", "Ready": "Served"},
                subnet_deadline_secs=deadline,
            )
        ],
    )
    done = threading.Event()

    def drive() -> None:
        try:
            with parent:
                parent.deposit("Queue", Token(payload={"order": 1}))
                _drive(parent, logical)
        finally:
            done.set()

    threading.Thread(target=drive, daemon=True).start()
    assert done.wait(2.5)
    assert _served(parent) == [1]


@pytest.mark.parametrize("deadline", [None, math.inf], ids=["none", "inf"])
def test_unbounded_drive_steps_a_subnet_with_no_cooldown_interval(deadline):
    kitchen = PetriNet(
        places=[Place("Orders"), Place("Ready")],
        cooldown_interval=0.0,
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
                subnet=kitchen,
                port_socket_map={"Orders": "Queue", "Ready": "Served"},
                subnet_deadline_secs=deadline,
            )
        ],
    )
    with parent:
        parent.deposit("Queue", Token(payload={"order": 1}))
        parent.run(deadline=time.monotonic() + 5.0)
    assert _served(parent) == [1]


def test_firing_cut_short_by_an_enclosing_failure_still_cools_its_permit():
    """The station had started on the order when the front of house gave up, so the oven it
    borrowed was used: it goes back cooling down, as after any failed firing."""

    def prep(tokens: list[Token]) -> list[Token]:
        _hold_until_aborted(station)
        return tokens

    station = PetriNet(
        places=[Place("Orders"), Place("Prepped"), Place("Ready"), ResourcePlace("Oven", capacity=0)],
        transitions=[
            Transition("Prep", [InputArc("Orders")], [OutputArc("Prepped")], prep),
            Transition("Bake", [InputArc("Prepped")], [OutputArc("Ready")], lambda tokens: tokens),
        ],
    )
    oven = PacedResourcePlace("Oven", capacity=1, pacing_secs=60.0)
    kitchen = PetriNet(
        places=[Place("Orders"), Place("Ready"), oven],
        transitions=[
            SubstitutionTransition(
                name="Station",
                inputs=[InputArc("Orders"), InputArc("Oven")],
                outputs=[OutputArc("Ready")],
                action=lambda tokens: tokens,
                subnet=station,
                port_socket_map={"Orders": "Orders", "Ready": "Ready", "Oven": "Oven"},
                subnet_deadline_secs=5.0,
                max_retries=0,
            )
        ],
    )
    front = PetriNet(
        places=[Place("Queue"), Place("Served")],
        transitions=[
            SubstitutionTransition(
                name="Kitchen",
                inputs=[InputArc("Queue")],
                outputs=[OutputArc("Served")],
                action=lambda tokens: tokens,
                subnet=kitchen,
                port_socket_map={"Orders": "Queue", "Ready": "Served"},
                subnet_deadline_secs=0.2,
                max_retries=0,
            )
        ],
    )
    with front:
        front.deposit("Queue", Token(payload={"order": 1}))
        front.run(deadline=time.monotonic() + 5.0)
    _wait_until_settled(kitchen)
    _wait_until_settled(station)
    assert [t.payload["order"] for t in front.marking["failed"]] == [1]
    assert [t.available_at > time.monotonic() for t in oven.tokens] == [True]


def test_a_permit_cooling_down_during_the_finished_check_keeps_the_net_unfinished():
    """`_is_finished` reads deadness and the next availability time separately. A permit whose
    cooldown ends between the two reads must not make a net that can fire look finished."""
    oven = PacedResourcePlace("Oven", capacity=1, pacing_secs=1.0)
    net = PetriNet(
        places=[Place("Orders"), Place("Ready"), oven],
        transitions=[Transition("Bake", [InputArc("Orders"), InputArc("Oven")], [OutputArc("Ready")], lambda t: t)],
    )
    is_dead = net.is_dead

    def dead_then_wait() -> bool:
        dead = is_dead()
        (permit,) = oven.tokens
        while time.monotonic() <= permit.available_at:
            time.sleep(0.01)
        return dead

    with net:
        net.deposit("Orders", Token(payload={"order": 1}))
        assert net.step()
        net._await_inflight()
        net.deposit("Orders", Token(payload={"order": 2}))
        assert net.is_dead()
        net.is_dead = dead_then_wait
        assert not net._is_finished()


def test_deadline_is_checked_on_assignment_and_stored_as_a_float():
    subnet = PetriNet(places=[Place("Orders"), Place("Ready")])
    cafe = SubstitutionTransition(
        name="Cafe",
        inputs=[InputArc("Queue")],
        outputs=[OutputArc("Served")],
        action=lambda tokens: tokens,
        subnet=subnet,
        port_socket_map={"Orders": "Queue", "Ready": "Served"},
        subnet_deadline_secs=Fraction(1, 2),
    )
    assert cafe.subnet_deadline_secs == 0.5
    cafe.subnet_deadline_secs = 10**400
    assert cafe.subnet_deadline_secs == math.inf
    for bad in (float("nan"), -(10**400)):
        with pytest.raises(ValueError, match="positive number or None"):
            cafe.subnet_deadline_secs = bad
    assert cafe.subnet_deadline_secs == math.inf


def test_nested_drive_that_raises_after_an_enclosing_failure_reports_nothing():
    """On the logical clock the front of house gives up while the station is still being driven.
    The station's drive then raises; that failure belongs to the outer one, so the front must not
    report it or dead-letter the order."""
    station = PetriNet(
        places=[Place("Orders"), Place("Ready")],
        transitions=[Transition("Brew", [InputArc("Orders")], [OutputArc("Ready")], lambda tokens: tokens)],
    )
    front = PetriNet(
        places=[Place("Orders"), Place("Ready"), SinkPlace("Spoiled", keep_last=10)],
        error_place="Spoiled",
        max_workers=1,
        transitions=[
            SubstitutionTransition(
                name="Station",
                inputs=[InputArc("Orders")],
                outputs=[OutputArc("Ready")],
                action=lambda tokens: tokens,
                subnet=station,
                port_socket_map={"Orders": "Orders", "Ready": "Ready"},
                max_retries=0,
            )
        ],
    )

    def station_drive(*_args, **_kwargs):
        deadline = time.monotonic() + 5.0
        while not front._aborted() and time.monotonic() < deadline:
            time.sleep(0.005)
        raise RuntimeError("station drive gave up")

    station.drive_to_quiescence = station_drive
    _cap_drive(front, max_spins=2000)
    parent = PetriNet(
        places=[Place("Queue"), Place("Served")],
        max_workers=1,
        transitions=[
            SubstitutionTransition(
                name="Front",
                inputs=[InputArc("Queue")],
                outputs=[OutputArc("Served")],
                action=lambda tokens: tokens,
                subnet=front,
                port_socket_map={"Orders": "Queue", "Ready": "Served"},
                max_retries=0,
            )
        ],
    )
    front_errors = _collect_errors(front)
    dead_letters: list[Token] = []
    front.on_token_dead_lettered = lambda _name, token: dead_letters.append(token)
    parent.deposit("Queue", Token(payload={"order": 1}))
    parent.drive_to_quiescence()
    _wait_until_settled(front)
    assert [t.payload["order"] for t in parent.marking["failed"]] == [1]
    assert front_errors == [] and dead_letters == []
    assert front.places["Spoiled"].stats()["absorbed"] == 0


def _slow_kitchen(brew, *, deadline: float, workers: int) -> tuple[PetriNet, PetriNet]:
    subnet = PetriNet(
        places=[Place("Orders"), Place("Ready")],
        transitions=[Transition("Brew", [InputArc("Orders")], [OutputArc("Ready")], brew)],
    )
    parent = PetriNet(
        places=[Place("Queue"), Place("Served")],
        max_workers=workers,
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
    return parent, subnet


def test_firings_queued_behind_a_hung_action_share_one_settle_wait():
    """Firings parked on the transition at the same time wait out one bound together, so a
    backlog behind a hung action does not hold the parent's shutdown for N deadlines."""
    deadline = 0.2
    orders = 5
    release = threading.Event()

    def hung(tokens: list[Token]) -> list[Token]:
        release.wait(10.0)
        return tokens

    parent, subnet = _slow_kitchen(hung, deadline=deadline, workers=orders)
    failed_at: list[float] = []
    done = threading.Event()

    def on_error(_name: str, _exc: Exception, _token: Token | None) -> None:
        failed_at.append(time.monotonic())
        if len(failed_at) == orders:
            done.set()

    parent.on_error = on_error
    with parent:
        for order in range(orders):
            parent.deposit("Queue", Token(payload={"order": order}))
        try:
            parent.run(deadline=time.monotonic() + 10.0, stop_event=done)
            assert done.is_set()
        finally:
            release.set()
        _wait_until_settled(subnet)
    assert failed_at[-1] - failed_at[0] < 2.5 * deadline


def test_firing_parked_before_the_failure_gets_a_full_bound_to_wait_out_a_straggler():
    """The wait runs from the failure, not from when the firing arrived, so an order that
    spent its bound parked behind the failed firing is not failed by a brief straggler."""
    deadline = 0.2
    first = threading.Event()

    def brew(tokens: list[Token]) -> list[Token]:
        if not first.is_set():
            first.set()
            time.sleep(deadline * 1.5)
        return tokens

    parent, subnet = _slow_kitchen(brew, deadline=deadline, workers=2)
    with parent:
        parent.deposit("Queue", Token(payload={"order": 1}))
        parent.deposit("Queue", Token(payload={"order": 2}))
        parent.run(deadline=time.monotonic() + 10.0)
    # Which order takes the lock first is up to the scheduler: exactly one is the failed firing.
    assert len(_served(parent)) == 1
    assert len(parent.marking["failed"]) == 1


def _paced_kitchen() -> tuple[PetriNet, PetriNet]:
    """Serving five orders takes the kitchen four clock advances, one per cooldown."""
    subnet = PetriNet(
        places=[Place("Orders"), Place("Ready"), PacedResourcePlace("Grinder", capacity=1, pacing_secs=1.0)],
        transitions=[
            Transition(
                "Brew",
                [InputArc("Orders"), InputArc("Grinder")],
                [OutputArc("Ready")],
                lambda tokens: [t for t in tokens if not t.is_resource],
            )
        ],
    )
    parent = PetriNet(
        places=[Place("Queue"), Place("Served")],
        transitions=[
            SubstitutionTransition(
                name="Cafe",
                inputs=[InputArc("Queue", count=5)],
                outputs=[OutputArc("Served", count=5)],
                action=lambda tokens: tokens,
                subnet=subnet,
                port_socket_map={"Orders": "Queue", "Ready": "Served"},
                max_retries=0,
            )
        ],
    )
    for order in range(5):
        parent.deposit("Queue", Token(payload={"order": order}))
    return parent, subnet


def test_parent_max_ticks_bounds_a_logical_subnet():
    parent, _ = _paced_kitchen()
    errors = _collect_errors(parent)
    with parent:
        parent.drive_to_quiescence(max_ticks=2)
    assert _served(parent) == []
    assert len(parent.marking["failed"]) == 5
    assert len(errors) == 5, "on_error reports each rolled-back order"
    assert all("after 2 clock advance" in str(error) for error in errors)


def test_parent_drive_limits_reach_the_subnet_drive():
    parent, subnet = _paced_kitchen()
    seen: list[dict[str, int | bool]] = []
    drive = subnet.drive_to_quiescence

    def recording_drive(**limits: int):
        seen.append(limits)
        return drive(**limits)

    subnet.drive_to_quiescence = recording_drive
    with parent:
        parent.drive_to_quiescence(max_ticks=50, max_spins=5_000_000)
    assert seen == [{"max_ticks": 50, "max_spins": 5_000_000, "inline_actions": False}]
    assert len(_served(parent)) == 5


def test_parent_inline_actions_reach_the_subnet_drive():
    parent, subnet = _paced_kitchen()
    seen: list[dict[str, int | bool]] = []
    drive = subnet.drive_to_quiescence

    def recording_drive(**limits: int | bool):
        seen.append(limits)
        return drive(**limits)

    subnet.drive_to_quiescence = recording_drive
    with parent:
        parent.drive_to_quiescence(inline_actions=True)
    assert [limits["inline_actions"] for limits in seen] == [True]
    assert len(_served(parent)) == 5


def test_drive_limits_do_not_outlive_the_drive():
    """A later logical `run()` drives its subnets with the defaults, not the last drive's caps."""
    parent, _ = _paced_kitchen()
    with parent:
        parent.drive_to_quiescence(max_ticks=2)
        assert len(parent.marking["failed"]) == 5
        for order in range(5, 10):
            parent.deposit("Queue", Token(payload={"order": order}))
        parent.run(deadline=time.monotonic() + 10.0)
    assert sorted(_served(parent)) == [5, 6, 7, 8, 9]
