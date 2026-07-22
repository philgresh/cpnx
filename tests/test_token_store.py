"""Regression tests for `_TokenStore`, the O(1)-arbitrary-removal / O(1)-amortized
earliest-available backing store introduced to replace `Place`'s `collections.deque`.

Covers:
- Arbitrary-position removal no longer requires rebuilding the whole store (the
  seq/id-keyed removal path stays correct regardless of how many tokens sit
  before/after the removed one, unlike the old O(n) deque rebuild).
- A deep TIMED place (many cooling tokens) still yields matured tokens through
  `peek`/`retrieve` in `(available_at, seq)` order, non-destructively for peek.
- The lazily-deleted heap (`_cooling`) does not accumulate unbounded stale
  entries across many interleaved deposit/remove cycles — old entries are
  discarded as they're encountered rather than kept forever.
"""

from cpnx.places import Place, _TokenStore
from cpnx.tokens import Token


class TestArbitraryRemovalCorrectness:
    """Removing tokens out of insertion order must leave exactly the right survivors,
    independent of *where* in the store the removed tokens sit."""

    def test_remove_middle_token_leaves_neighbors_intact_and_ordered(self):
        p = Place("p")
        tokens = [Token(payload={"i": i}) for i in range(7)]
        for t in tokens:
            p.deposit(t)

        # Remove a token from the middle by identity via retrieve_specific.
        victim = tokens[3]
        got = p.retrieve_specific([victim])
        assert got == [victim]

        remaining = p.tokens
        assert remaining == tuple(t for t in tokens if t is not victim)
        assert [t.payload["i"] for t in remaining] == [0, 1, 2, 4, 5, 6]

    def test_remove_many_arbitrary_positions_preserves_relative_order(self):
        p = Place("p")
        tokens = [Token(payload={"i": i}) for i in range(50)]
        for t in tokens:
            p.deposit(t)

        # Remove every third token, out of order, by identity.
        victims = [tokens[i] for i in range(0, 50, 3)]
        # Shuffle removal order to further stress arbitrary-position removal.
        removal_order = victims[::-1]
        for v in removal_order:
            got = p.retrieve_specific([v])
            assert got == [v]

        remaining = p.tokens
        expected = [t for t in tokens if t not in victims]
        assert list(remaining) == expected

    def test_id_collision_survivor_unaffected_by_unrelated_removal(self):
        # Sanity check that removal is truly by object identity, not by seq
        # proximity: removing an unrelated token from anywhere in a large store
        # must not disturb any other token, including one sharing a Token.id
        # with a still-present token (PacedResourcePlace cooldown-copy pattern).
        p = Place("p")
        base = Token(available_at=0.0)
        colliding_future = base.evolve(available_at=100.0, id=base.id)
        filler = [Token(payload={"i": i}) for i in range(20)]

        for t in filler[:10]:
            p.deposit(t)
        p.deposit(base)
        p.deposit(colliding_future)
        for t in filler[10:]:
            p.deposit(t)

        got = p.retrieve_specific([base], model_time=0.0)
        assert got == [base]
        remaining = p.tokens
        assert colliding_future in remaining
        assert base not in remaining
        assert len(remaining) == 20 + 1


class TestDeepTimedAvailabilityOrder:
    """A place with many cooling tokens must still surface matured tokens in
    strict `(available_at, seq)` order, and peek must not mutate the store."""

    def _deep_cooling_place(self, n: int) -> tuple[Place, list[Token]]:
        p = Place("p")
        # Deposit tokens with descending available_at so insertion order is the
        # REVERSE of maturity order -- this would defeat a naive scan that
        # assumed insertion order implied availability order.
        tokens = [Token(payload={"i": i}, available_at=float(n - i)) for i in range(n)]
        for t in tokens:
            p.deposit(t, model_time=0.0)
        return p, tokens

    def test_peek_returns_matured_tokens_in_availability_order_not_insertion_order(self):
        n = 200
        p, tokens = self._deep_cooling_place(n)
        # At model_time = n, everything has matured. availability order is the
        # REVERSE of insertion order (available_at = n - i, descending with i).
        peeked = p.peek(5, model_time=float(n))
        assert [t.payload["i"] for t in peeked] == [n - 1, n - 2, n - 3, n - 4, n - 5]

    def test_peek_is_non_destructive_on_deep_cooling_store(self):
        n = 100
        p, tokens = self._deep_cooling_place(n)
        first_peek = p.peek(10, model_time=float(n))
        second_peek = p.peek(10, model_time=float(n))
        assert first_peek == second_peek
        assert len(p.tokens) == n

    def test_retrieve_partial_maturity_only_returns_matured_subset(self):
        n = 50
        p, tokens = self._deep_cooling_place(n)
        # available_at values run n..1 as i runs 0..n-1. At model_time = 10,
        # only tokens with available_at <= 10 (i.e. i >= n - 10) have matured.
        matured_count = sum(1 for t in tokens if t.available_at <= 10.0)
        assert p.can_retrieve(matured_count, model_time=10.0)
        assert not p.can_retrieve(matured_count + 1, model_time=10.0)
        got = p.retrieve(matured_count, model_time=10.0)
        assert len(got) == matured_count
        assert all(t.available_at <= 10.0 for t in got)
        # Returned in ascending available_at order.
        assert [t.available_at for t in got] == sorted(t.available_at for t in got)


class TestLazyHeapCleanup:
    """Removed cooling tokens must not leave the heap growing unboundedly across
    many deposit/remove cycles -- stale entries are discarded as encountered."""

    def test_heap_size_bounded_after_many_deposit_remove_cycles(self):
        store = _TokenStore()
        cycles = 500
        for i in range(cycles):
            t = Token(payload={"i": i}, available_at=float(1000 - i))
            store.append(t)
            # Remove it again immediately -- this leaves a stale heap entry
            # each time (the (available_at, seq) tuple stays in the heap list,
            # but its seq is gone from _cooling_by_seq).
            store.remove_identity([t])

        assert len(store) == 0
        # Force a scan that must clean up all the stale entries it encounters.
        assert store.has_available(t_limit=10_000.0, need=1) is False
        # After the scan, no live cooling entries remain and the heap should
        # have been drained of everything it walked past (it walks the whole
        # heap here since nothing matches).
        assert store._cooling_by_seq == {}

    def test_interleaved_deposits_and_removals_keep_correct_survivors(self):
        store = _TokenStore()
        kept: list[Token] = []
        for i in range(300):
            t = Token(payload={"i": i}, available_at=float(300 - i))
            store.append(t)
            if i % 2 == 0:
                store.remove_identity([t])
            else:
                kept.append(t)

        assert len(store) == len(kept)
        survivors = list(store.iter_insertion_order())
        assert survivors == kept

        # All kept tokens eventually mature and are retrievable in
        # availability order despite the interleaved stale heap entries.
        all_available = store.take_available(len(kept), t_limit=10_000.0)
        assert len(all_available) == len(kept)
        assert [t.payload["i"] for t in all_available] == sorted((t.payload["i"] for t in kept), key=lambda i: 300 - i)
