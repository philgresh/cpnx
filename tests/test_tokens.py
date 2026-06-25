from petriq.tokens import Token


class TestToken:
    def test_default_fields(self):
        t = Token()
        assert isinstance(t.id, str)
        assert len(t.id) == 8
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

    def test_payload_is_mutable(self):
        t = Token(payload={"count": 0})
        t.payload["count"] += 1
        assert t.payload["count"] == 1

    def test_two_tokens_not_equal(self):
        t1 = Token()
        t2 = Token()
        assert t1 != t2

    def test_same_token_equals_itself(self):
        t = Token()
        assert t == t
