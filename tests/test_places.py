import pytest

from petriq.places import Place, ResourcePlace
from petriq.tokens import Token


class TestPlace:
    def test_empty_place_cannot_retrieve(self):
        p = Place("p")
        assert not p.can_retrieve(1)

    def test_fifo_ordering(self):
        p = Place("p")
        t1, t2, t3 = Token(), Token(), Token()
        for t in (t1, t2, t3):
            p.deposit(t)
        assert p.retrieve(1) == [t1]
        assert p.retrieve(1) == [t2]
        assert p.retrieve(1) == [t3]

    def test_retrieve_multiple(self):
        p = Place("p")
        tokens = [Token() for _ in range(5)]
        for t in tokens:
            p.deposit(t)
        got = p.retrieve(3)
        assert got == tokens[:3]
        assert len(p.tokens) == 2

    def test_retrieve_raises_when_insufficient(self):
        p = Place("p")
        p.deposit(Token())
        with pytest.raises(ValueError):
            p.retrieve(2)

    def test_retrieve_all(self):
        p = Place("p")
        tokens = [Token() for _ in range(4)]
        for t in tokens:
            p.deposit(t)
        got = p.retrieve_all()
        assert got == tokens
        assert len(p.tokens) == 0

    def test_retrieve_all_empty(self):
        p = Place("p")
        assert p.retrieve_all() == []

    def test_peek_does_not_consume(self):
        p = Place("p")
        t = Token()
        p.deposit(t)
        peeked = p.peek(1)
        assert peeked == [t]
        assert len(p.tokens) == 1

    def test_can_retrieve_exact_count(self):
        p = Place("p")
        for _ in range(3):
            p.deposit(Token())
        assert p.can_retrieve(3)
        assert not p.can_retrieve(4)

    def test_tokens_property_returns_copy(self):
        p = Place("p")
        t = Token()
        p.deposit(t)
        snapshot = p.tokens
        snapshot.clear()
        assert len(p.tokens) == 1

    def test_last_deposit_time_updated(self):
        p = Place("p")
        assert p.last_deposit_time == 0.0
        p.deposit(Token())
        assert p.last_deposit_time > 0.0

    def test_payload_preserved(self):
        p = Place("p")
        t = Token(payload={"key": "value", "num": 42})
        p.deposit(t)
        got = p.retrieve(1)[0]
        assert got.payload == {"key": "value", "num": 42}

    def test_retrieve_all_then_empty_again(self):
        p = Place("p")
        for _ in range(3):
            p.deposit(Token())
        p.retrieve_all()
        assert p.retrieve_all() == []


class TestResourcePlace:
    def test_prefilled_with_resource_tokens(self):
        rp = ResourcePlace("r", capacity=4)
        assert len(rp.tokens) == 4
        assert all(t.is_resource for t in rp.tokens)

    def test_capacity_zero(self):
        rp = ResourcePlace("r", capacity=0)
        assert len(rp.tokens) == 0
        assert not rp.can_retrieve(1)

    def test_retrieve_and_return(self):
        rp = ResourcePlace("r", capacity=2)
        taken = rp.retrieve(2)
        assert len(rp.tokens) == 0
        assert not rp.can_retrieve(1)
        for t in taken:
            rp.deposit(t)
        assert len(rp.tokens) == 2

    def test_retrieve_more_than_capacity_raises(self):
        rp = ResourcePlace("r", capacity=2)
        with pytest.raises(ValueError):
            rp.retrieve(3)

    def test_resource_tokens_have_is_resource_flag(self):
        rp = ResourcePlace("r", capacity=3)
        tokens = rp.retrieve(3)
        assert all(t.is_resource for t in tokens)

    def test_can_retrieve_partial(self):
        rp = ResourcePlace("r", capacity=5)
        assert rp.can_retrieve(5)
        assert not rp.can_retrieve(6)
