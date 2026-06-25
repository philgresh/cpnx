import pytest

from petriq.places import Place
from petriq.tokens import Token


def test_base_place_fifo():
    place = Place("p1")
    t1 = Token(payload={"val": 1})
    t2 = Token(payload={"val": 2})

    assert not place.can_retrieve(1)
    place.deposit(t1)
    assert place.can_retrieve(1)
    assert not place.can_retrieve(2)

    place.deposit(t2)
    assert place.can_retrieve(2)

    assert len(place.tokens) == 2
    assert place.peek(1)[0] == t1

    retrieved = place.retrieve(1)
    assert retrieved == [t1]
    assert len(place.tokens) == 1

    retrieved_all = place.retrieve_all()
    assert retrieved_all == [t2]
    assert len(place.tokens) == 0


def test_base_place_insufficient_tokens():
    place = Place("p1")
    with pytest.raises(ValueError, match="Not enough tokens to retrieve"):
        place.retrieve(1)
