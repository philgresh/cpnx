"""Follow-ups to the subnet transaction of #38."""

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
