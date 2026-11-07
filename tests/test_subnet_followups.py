"""Follow-ups to the subnet transaction of #38: permits, surplus outputs, colour sets and parked tokens.

Permits survive a failed firing (`CPN-PINV`), a firing conserves its outputs, a port and its
socket share one colour set (`CPN-COMPOUND-COLSET`), and a token no finite time releases does
not keep a net busy.
"""

import math
import time
from dataclasses import replace

import pytest

from cpnx import (
    InputArc,
    OutputArc,
    PacedResourcePlace,
    PetriNet,
    Place,
    ResourcePlace,
    RetryPolicy,
    SubstitutionTransition,
    Token,
    Transition,
)


def _drive(net: PetriNet, logical: bool) -> None:
    if logical:
        net.drive_to_quiescence()
    else:
        net.run(deadline=time.monotonic() + 5.0)


def _order(number: int) -> Token:
    return Token(payload={"order": number})


def _served(net: PetriNet) -> list[int]:
    return sorted(token.payload["order"] for token in net.marking["Served"])


class _Jam:
    """A subnet action that fails while `on` is set."""

    def __init__(self) -> None:
        self.on = True

    def __call__(self, tokens: list[Token]) -> list[Token]:
        if self.on:
            raise RuntimeError("jammed")
        return [tokens[0]]


def _permit_port_cafe(jam: _Jam, barista: ResourcePlace) -> tuple[PetriNet, PetriNet]:
    """A parent `Staff` pool mapped onto the subnet's `Barista` pool as a port."""
    subnet = PetriNet(
        places=[Place("Orders"), Place("Ready"), barista],
        transitions=[
            Transition(
                "Brew", [InputArc("Orders"), InputArc("Barista")], [OutputArc("Ready")], action=jam, max_retries=0
            )
        ],
    )
    kitchen = SubstitutionTransition(
        "Kitchen",
        [InputArc("Queue"), InputArc("Staff")],
        [OutputArc("Served")],
        action=lambda tokens: tokens,
        subnet=subnet,
        port_socket_map={"Orders": "Queue", "Barista": "Staff", "Ready": "Served"},
        max_retries=2,
        retry_policy=RetryPolicy(delay=0.0),
    )
    parent = PetriNet(
        places=[Place("Queue"), Place("Served"), ResourcePlace("Staff", capacity=1)],
        transitions=[kitchen],
    )
    return parent, subnet


@pytest.mark.parametrize("logical", [True, False], ids=["logical", "wall"])
def test_failed_firings_leave_no_permit_copy_in_a_resource_port(logical):
    parent, subnet = _permit_port_cafe(_Jam(), ResourcePlace("Barista", capacity=0))
    parent.deposit("Queue", _order(1))

    _drive(parent, logical)

    assert subnet.marking["Barista"] == ()
    assert len(parent.marking["Staff"]) == 1


@pytest.mark.parametrize("logical", [True, False], ids=["logical", "wall"])
def test_a_resource_port_keeps_its_own_permits_through_a_failure(logical):
    parent, subnet = _permit_port_cafe(_Jam(), ResourcePlace("Barista", capacity=2))
    parent.deposit("Queue", _order(1))

    _drive(parent, logical)

    assert len(subnet.marking["Barista"]) == 2


def test_dropping_a_port_copy_keeps_the_cooldown_a_failed_firing_started():
    """The copy is the permit that can go at once, so the one still cooling stays."""
    pool = PacedResourcePlace("Barista", capacity=1, pacing_secs=60.0)
    (own,) = pool.tokens
    cooling = replace(own, available_at=time.monotonic() + 500.0)
    pool._store.drop_all()
    pool._store.append(own)
    pool._store.append(cooling)

    assert pool._restore_permits((own,)) is True

    assert pool.tokens == (cooling,)


def test_restoring_an_unchanged_pool_reports_no_change():
    pool = ResourcePlace("Barista", capacity=2)
    assert pool._restore_permits(pool.tokens) is False


@pytest.mark.parametrize("logical", [True, False], ids=["logical", "wall"])
def test_a_permit_moved_between_pools_comes_back_when_the_firing_fails(logical):
    jam = _Jam()

    def acquire(tokens: list[Token]) -> list[Token]:
        return tokens

    subnet = PetriNet(
        places=[
            Place("Orders"),
            Place("Waiting"),
            Place("Ready"),
            ResourcePlace("Oven", capacity=1),
            ResourcePlace("Held", capacity=0),
        ],
        transitions=[
            Transition(
                "Acquire",
                [InputArc("Orders"), InputArc("Oven")],
                [OutputArc("Waiting"), OutputArc("Held")],
                action=acquire,
                auto_return_resources=False,
            ),
            Transition(
                "Release",
                [InputArc("Waiting"), InputArc("Held")],
                [OutputArc("Ready"), OutputArc("Oven")],
                action=jam,
                max_retries=0,
                auto_return_resources=False,
            ),
        ],
    )
    kitchen = SubstitutionTransition(
        "Kitchen",
        [InputArc("Queue")],
        [OutputArc("Served")],
        action=lambda tokens: tokens,
        subnet=subnet,
        port_socket_map={"Orders": "Queue", "Ready": "Served"},
        max_retries=0,
    )
    parent = PetriNet(places=[Place("Queue"), Place("Served")], transitions=[kitchen])
    parent.deposit("Queue", _order(1))

    _drive(parent, logical)
    assert len(subnet.marking["Oven"]) == 1
    assert subnet.marking["Held"] == ()

    jam.on = False
    parent.deposit("Queue", _order(2))
    _drive(parent, logical)

    assert _served(parent) == [2]
    assert len(subnet.marking["Oven"]) == 1


def _copying_kitchen(copies: int, **arc) -> tuple[PetriNet, PetriNet, list[BaseException]]:
    def brew(tokens: list[Token]) -> list[Token]:
        return [tokens[0].evolve() for _ in range(copies)]

    subnet = PetriNet(
        places=[Place("Orders"), Place("Ready")],
        transitions=[Transition("Brew", [InputArc("Orders")], [OutputArc("Ready", count=copies)], action=brew)],
    )
    kitchen = SubstitutionTransition(
        "Kitchen",
        [InputArc("Queue")],
        [OutputArc("Served", **arc)],
        action=lambda tokens: tokens,
        subnet=subnet,
        port_socket_map={"Orders": "Queue", "Ready": "Served"},
        max_retries=0,
    )
    parent = PetriNet(places=[Place("Queue"), Place("Served")], transitions=[kitchen])
    errors: list[BaseException] = []
    parent.on_error = lambda _name, error, _token: errors.append(error)
    return parent, subnet, errors


@pytest.mark.parametrize("logical", [True, False], ids=["logical", "wall"])
def test_subnet_outputs_beyond_the_parent_demand_fail_the_firing(logical):
    parent, subnet, errors = _copying_kitchen(2)
    parent.deposit("Queue", _order(1))

    _drive(parent, logical)

    assert parent.marking["Served"] == ()
    assert any("surplus" in str(error) for error in errors), errors
    assert subnet.marking["Ready"] == ()
    assert subnet.marking["Orders"] == ()


def test_a_subnet_that_meets_the_parent_demand_exactly_commits():
    parent, _subnet, errors = _copying_kitchen(2, count=2)
    parent.deposit("Queue", _order(1))

    parent.drive_to_quiescence()

    assert _served(parent) == [1, 1]
    assert errors == []


def test_a_plain_transition_may_still_return_more_than_its_arcs_take():
    def spill(tokens: list[Token]) -> list[Token]:
        return [tokens[0], tokens[0]]

    net = PetriNet(
        places=[Place("Queue"), Place("Served")],
        transitions=[Transition("Pour", [InputArc("Queue")], [OutputArc("Served")], action=spill)],
    )
    net.deposit("Queue", _order(1))

    net.drive_to_quiescence()

    assert _served(net) == [1]


def _coloured_kitchen(port: frozenset | set | None) -> SubstitutionTransition:
    subnet = PetriNet(places=[Place("Orders", color_set=port), Place("Ready")])
    return SubstitutionTransition(
        "Kitchen",
        [InputArc("Queue")],
        [OutputArc("Served")],
        action=lambda tokens: tokens,
        subnet=subnet,
        port_socket_map={"Orders": "Queue", "Ready": "Served"},
    )


def test_add_transition_rejects_a_port_and_socket_with_different_colour_sets():
    parent = PetriNet(places=[Place("Queue", color_set={"latte", "mocha"}), Place("Served")])

    with pytest.raises(ValueError, match=r"Orders.*Queue.*colour set"):
        parent.add_transition(_coloured_kitchen({"latte"}))

    assert "Kitchen" not in parent.transitions


def test_add_place_rejects_a_socket_whose_colour_set_differs_from_its_port():
    parent = PetriNet()
    parent.add_transition(_coloured_kitchen({"latte"}))

    with pytest.raises(ValueError, match=r"Orders.*Queue.*colour set"):
        parent.add_place(Place("Queue", color_set={"mocha"}))

    assert "Queue" not in parent.places


def test_validate_rejects_a_colour_set_changed_after_wrapping():
    queue = Place("Queue", color_set={"latte"})
    parent = PetriNet(places=[queue, Place("Served")])
    parent.add_transition(_coloured_kitchen({"latte"}))
    queue.color_set = {"mocha"}

    with pytest.raises(ValueError, match="colour set"):
        parent.validate()


@pytest.mark.parametrize(
    ("port", "socket"),
    [({"latte"}, {"latte"}), (None, {"latte"}), ({"latte"}, None), (None, None)],
    ids=["same", "untyped-port", "untyped-socket", "both-untyped"],
)
def test_matching_or_undeclared_colour_sets_are_accepted(port, socket):
    parent = PetriNet(places=[Place("Queue", color_set=socket), Place("Served")])
    parent.add_transition(_coloured_kitchen(port))
    parent.validate()


def _parked() -> Token:
    return Token(payload={"order": 1}, available_at=math.inf)


def _parked_net() -> PetriNet:
    return PetriNet(
        places=[Place("Queue"), Place("Served")],
        transitions=[Transition("Pour", [InputArc("Queue")], [OutputArc("Served")], action=lambda tokens: tokens)],
    )


def test_a_parked_token_leaves_a_net_quiescent():
    net = _parked_net()
    net.deposit("Queue", _parked())
    assert net.is_quiescent()


def test_a_token_cooling_for_a_finite_time_still_counts_as_pending():
    net = _parked_net()
    net.deposit("Queue", Token(payload={"order": 1}, available_at=1e300))
    assert not net.is_quiescent()


def test_a_parked_token_does_not_mask_real_work():
    net = _parked_net()
    net.deposit("Queue", _parked())
    net.deposit("Queue", _order(2))
    assert not net.is_quiescent()
    net.drive_to_quiescence()
    assert _served(net) == [2]
    assert net.is_quiescent()


def test_a_consume_all_arc_is_not_kept_busy_by_a_parked_token():
    net = PetriNet(
        places=[Place("Queue"), Place("Served")],
        transitions=[
            Transition(
                "Pour", [InputArc("Queue", consume_all=True)], [OutputArc("Served")], action=lambda tokens: tokens[:1]
            )
        ],
    )
    net.deposit("Queue", _parked())
    assert net.is_quiescent()


def test_a_parent_run_with_a_parked_token_returns_without_its_deadline():
    net = _parked_net()
    net.deposit("Queue", _parked())

    started = time.monotonic()
    net.run(deadline=started + 5.0)

    assert time.monotonic() - started < 2.0
    assert len(net.marking["Queue"]) == 1
