from petriq.tokens import Token


class TestToken:
    def test_default_fields(self):
        t = Token()
        assert isinstance(t.id, str)
        assert len(t.id) == 16
        assert t.payload == {}
        assert t.is_resource is False
        assert t.created_at > 0

    def test_unique_ids(self):
        ids = {Token().id for _ in range(1000)}
        assert len(ids) == 1000

    def test_payload_stored(self):
        t = Token(payload={"key": "value"})
        assert t.payload["key"] == "value"

    def test_resource_flag(self):
        t = Token(color="resource")
        assert t.is_resource is True

    def test_payload_is_immutable(self):
        t = Token(payload={"count": 0})
        import pytest

        with pytest.raises(TypeError):
            t.payload["count"] += 1

    def test_two_tokens_not_equal(self):
        t1 = Token()
        t2 = Token()
        assert t1 != t2

    def test_same_token_equals_itself(self):
        t = Token()
        assert t == t

    def test_frozen_dict_backdoor_blocked(self):
        from petriq.tokens import FrozenDict
        import pytest

        fd = FrozenDict({"x": 1})
        with pytest.raises(TypeError):
            fd._data["x"] = 2

        with pytest.raises(AttributeError):
            fd.new_attribute = "allowed?"

    def test_evolve_generates_new_id(self):
        t1 = Token()
        t2 = t1.evolve()
        assert t1.id != t2.id
        # Confirm we can override explicitly
        t3 = t1.evolve(id="explicit_id")
        assert t3.id == "explicit_id"
