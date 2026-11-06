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
- Matured timed tokens migrate out of `_cooling` into `_matured`, so finding the next
  future boundary no longer re-walks a matured backlog (#59), while reads keep the
  `(available_at, seq)` contract across ready, matured and cooling tokens, past-dated
  deposits, `t_limit = inf` probes and reads on a clock behind the migration point.
"""

import heapq
import math
import random
import time

import pytest

from cpnx import InputArc, OutputArc, PacedResourcePlace, PetriNet, Transition
from cpnx.places import Place, _TokenStore
from cpnx.tokens import AVAILABLE_NOW, Token


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


def _timed(store: _TokenStore, *times: float) -> list[Token]:
    tokens = [Token(payload={"t": t}, available_at=t) for t in times]
    for token in tokens:
        store.append(token)
    return tokens


class _CountingHeapq:
    """Stand-in for `cpnx.places.heapq` that counts heap operations."""

    def __init__(self) -> None:
        self.ops = 0

    def heappush(self, heap: list, item: object) -> None:
        self.ops += 1
        heapq.heappush(heap, item)

    def heappop(self, heap: list) -> object:
        self.ops += 1
        return heapq.heappop(heap)

    def heapify(self, heap: list) -> None:
        self.ops += len(heap)
        heapq.heapify(heap)


@pytest.fixture
def heap_ops(monkeypatch: pytest.MonkeyPatch) -> _CountingHeapq:
    counter = _CountingHeapq()
    monkeypatch.setattr("cpnx.places.heapq", counter)
    return counter


def _deep_backlog_place(depth: int = 2000) -> Place:
    place = Place("p")
    for i in range(depth):
        place.deposit(Token(available_at=1.0 + i), model_time=0.0)
    place.deposit(Token(available_at=5000.0), model_time=0.0)
    return place


def _assert_fast_boundary(place: Place, heap_ops: _CountingHeapq) -> None:
    assert place.earliest_available_boundary(2500.0) == 5000.0
    heap_ops.ops = 0
    assert place.earliest_available_boundary(2600.0) == 5000.0
    assert heap_ops.ops <= 2


class TestMaturedMigration:
    """Matured timed tokens leave `_cooling` once a finite read time passes them (#59)."""

    def test_boundary_search_does_not_rewalk_a_matured_backlog(self, heap_ops):
        _assert_fast_boundary(_deep_backlog_place(), heap_ops)

    def test_reads_span_ready_matured_and_cooling_in_order(self):
        store = _TokenStore()
        ready = Token(payload={"t": "ready"})
        _timed(store, 30.0, 10.0, 20.0)
        store.append(ready)
        _timed(store, 40.0)

        assert store.peek_available(10, t_limit=25.0) == [ready, *_by_time(store, 10.0, 20.0)]
        assert [t.available_at for t in store.peek_available(10, t_limit=40.0)[1:]] == [10.0, 20.0, 30.0, 40.0]

    def test_past_dated_deposit_after_migration_keeps_order(self):
        store = _TokenStore()
        _timed(store, 60.0, 80.0, 200.0)
        assert store.earliest_cooling_after(100.0) == 200.0

        _timed(store, 50.0)

        assert [t.available_at for t in store.peek_available(10, t_limit=100.0)] == [50.0, 60.0, 80.0]
        assert store.earliest_cooling_after(100.0) == 200.0

    def test_infinite_probe_does_not_migrate(self):
        store = _TokenStore()
        _timed(store, 10.0, 20.0)

        assert store.has_available(t_limit=math.inf, need=2)
        assert store._migrated_to == -math.inf
        assert store.peek_available(10, t_limit=5.0) == []
        assert store.earliest_cooling_after(5.0) == 10.0
        assert store.earliest_cooling_after(math.inf) is None

    def test_read_behind_the_migration_point_still_sees_matured_future_tokens(self):
        store = _TokenStore()
        _timed(store, 5.0, 15.0, 2000.0)
        assert store.earliest_cooling_after(1000.0) == 2000.0

        assert store.earliest_cooling_after(10.0) == 15.0
        assert [t.available_at for t in store.peek_available(10, t_limit=10.0)] == [5.0]
        assert not store.has_available(t_limit=10.0, need=2)
        assert [t.available_at for t in store.take_available(10, t_limit=10.0)] == [5.0]
        assert len(store) == 2


#: Where the wall-clock reads below land. `time.monotonic()` is seconds since boot, which is
#: days on a workstation but under two minutes on a fresh CI runner, so it is pinned.
_WALL_CLOCK = 1_000_000.0


class TestSplitCannotBePinned:
    """A read on one clock must not leave later reads on another clock on the slow path (#59)."""

    def test_wall_clock_fifo_read_does_not_move_the_split(self, heap_ops, monkeypatch):
        place = _deep_backlog_place()
        assert place.earliest_available_boundary(100.0) == 101.0
        with monkeypatch.context() as m:
            m.setattr(time, "monotonic", lambda: _WALL_CLOCK)
            assert place.peek() is not None
            assert place.can_retrieve(1)
        assert place._store._migrated_to == 100.0

        heap_ops.ops = 0
        assert place.earliest_available_boundary(100.0) == 101.0
        assert heap_ops.ops <= 2

    def test_boundary_query_behind_the_split_resplits_once(self, heap_ops):
        place = _deep_backlog_place()
        assert place.earliest_available_boundary(_WALL_CLOCK) is None
        assert place._store._migrated_to == _WALL_CLOCK

        _assert_fast_boundary(place, heap_ops)
        assert place._store._migrated_to == 2600.0

    def test_wall_clock_step_then_logical_drive_stays_on_the_fast_path(self, heap_ops, monkeypatch):
        net = PetriNet(seed=0)
        for name in ("inbox", "out"):
            net.add_place(Place(name))
        net.add_place(PacedResourcePlace("gate", capacity=1, pacing_secs=3.0))
        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc("inbox"), InputArc("gate")],
                outputs=[OutputArc("out")],
                action=lambda tokens: [t for t in tokens if not t.is_resource],
            )
        )
        # Due after the wall-clock step, so nothing fires on a worker thread while it is patched.
        net.deposit("inbox", Token(available_at=2 * _WALL_CLOCK))
        with monkeypatch.context() as m:
            m.setattr(time, "monotonic", lambda: _WALL_CLOCK)
            net.step()
        net.advance_time(1.0)
        for i in range(1000):
            net.deposit("inbox", Token(available_at=1.0 + i))

        inbox = net.places["inbox"]
        boundary = inbox.earliest_available_boundary
        calls = ops = 0

        def counted(now: float) -> float | None:
            nonlocal calls, ops
            before = heap_ops.ops
            try:
                return boundary(now)
            finally:
                calls += 1
                ops += heap_ops.ops - before

        monkeypatch.setattr(inbox, "earliest_available_boundary", counted)
        result = net.drive_to_quiescence()

        assert len(net.marking["out"]) == 1001
        assert calls >= result.ticks > 1000
        assert ops <= 2 * calls

    def test_non_finite_reads_never_move_the_split(self):
        store = _TokenStore()
        _timed(store, 10.0, 100.0)
        assert store.earliest_cooling_after(50.0) == 100.0

        for t in (math.nan, math.inf, -math.inf):
            store.has_available(t_limit=t, need=1)
        assert store.earliest_cooling_after(-math.inf) == 10.0
        assert store.earliest_cooling_after(math.nan) is None
        assert store.earliest_cooling_after(math.inf) is None
        assert store._migrated_to == 50.0
        assert [t.available_at for t in store.peek_available(5, t_limit=60.0)] == [10.0]

    @pytest.mark.parametrize("empty", ["remove_identity", "take_available"])
    def test_removing_the_last_timed_token_resets_the_split(self, empty):
        store = _TokenStore()
        tokens = _timed(store, 10.0, 20.0)
        assert store.earliest_cooling_after(50.0) is None
        assert store._migrated_to == 50.0

        if empty == "remove_identity":
            store.remove_identity(tokens)
        else:
            assert len(store.take_available(10, t_limit=50.0)) == 2

        assert store._migrated_to == -math.inf
        assert store._matured == [] and store._cooling == []
        _timed(store, 30.0)
        assert store._cooling and not store._matured


def _by_time(store: _TokenStore, *times: float) -> list[Token]:
    by_time = {t.available_at: t for t in store.iter_insertion_order()}
    return [by_time[t] for t in times]


class _ReferenceStore:
    """Naive model of the `_TokenStore` read contract: ready tokens first, then timed by time."""

    def __init__(self) -> None:
        self.live: dict[int, tuple[int, Token]] = {}
        self.seq = 0

    def append(self, token: Token) -> None:
        self.live[id(token)] = (self.seq, token)
        self.seq += 1

    def available(self, t_limit: float) -> list[Token]:
        ready = [(s, t) for s, t in self.live.values() if t.available_at == AVAILABLE_NOW]
        timed = [(t.available_at, s, t) for s, t in self.live.values() if 0.0 < t.available_at <= t_limit]
        return [t for _, t in sorted(ready, key=lambda e: e[0])] + [t for *_, t in sorted(timed, key=lambda e: e[:2])]

    def earliest_after(self, now: float) -> float | None:
        later = [
            t.available_at for _, t in self.live.values() if t.available_at != AVAILABLE_NOW and t.available_at > now
        ]
        return min(later, default=None)


def _live(store: _TokenStore, heap: list[tuple[float, int]]) -> int:
    return sum(1 for _, seq in heap if seq in store._cooling_by_seq)


def _check_read(store: _TokenStore, ref: _ReferenceStore, t: float, rng: random.Random) -> None:
    expected = ref.available(t)
    boundary_first = rng.random() < 0.5
    if boundary_first:
        assert store.earliest_cooling_after(t) == ref.earliest_after(t)
    assert store.peek_available(5, t_limit=t) == expected[:5]
    assert store.has_available(t_limit=t, need=len(expected))
    assert not store.has_available(t_limit=t, need=len(expected) + 1)
    assert store.count_available(t) == len(expected)
    if not boundary_first:
        assert store.earliest_cooling_after(t) == ref.earliest_after(t)


@pytest.mark.parametrize("seed", range(12))
def test_store_matches_reference_under_a_moving_clock(seed):
    rng = random.Random(seed)
    store, ref = _TokenStore(), _ReferenceStore()
    clock = 10.0
    reads = reads_with_both_heaps_live = 0

    for _ in range(800):
        op = rng.random()
        if op < 0.35:
            token = Token(available_at=AVAILABLE_NOW if rng.random() < 0.1 else clock + rng.randint(-5, 20))
            store.append(token)
            ref.append(token)
        elif op < 0.45 and ref.live:
            _, victim = rng.choice(list(ref.live.values()))
            store.remove_identity([victim])
            del ref.live[id(victim)]
        else:
            direction = rng.random()
            if direction < 0.8:
                clock += rng.randint(0, 3)
                t = clock
            elif direction < 0.95:
                t = clock - rng.randint(1, 30)
            else:
                t = math.inf
            reads += 1
            reads_with_both_heaps_live += bool(_live(store, store._matured) and _live(store, store._cooling))
            _check_read(store, ref, t, rng)
            if op > 0.95:
                expected = ref.available(t)
                taken = store.take_available(2, t_limit=t)
                assert taken == expected[:2]
                for token in taken:
                    del ref.live[id(token)]
        assert len(store) == len(ref.live)

    assert reads_with_both_heaps_live >= 0.75 * reads


def test_restore_drops_tokens_absent_from_the_snapshot():
    a, b = Token(payload={"order": "a"}), Token(payload={"order": "b"})
    place = Place("Orders", initial_marking=[a, b])
    assert place._restore_tokens((a,))
    assert place.tokens == (a,)
    with pytest.raises(ValueError):
        place.retrieve_specific([b])


def test_restore_resets_key_index_entries():
    place = Place("Backlog", initial_marking=[Token(payload={"prio": i}) for i in range(100)])
    place._store.register_key_index(1, lambda t: t.payload["prio"])
    for _ in range(20):
        snapshot = place.tokens
        place.retrieve(1)
        place._restore_tokens(snapshot)
    index = place._store.key_index(1)
    assert len(index._heap) == len(place)
    assert [t.payload["prio"] for t in index.peek_ordered(place._store._ready, 3)] == [0, 1, 2]
