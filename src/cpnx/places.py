"""Place types for the cpnx Petri net executor.

All place classes are thread-safe. Choose the right type for your use case:

- [`Place`][cpnx.Place]              — unbounded FIFO queue for data / work tokens
- [`ResourcePlace`][cpnx.ResourcePlace]      — bounded permit pool (GPU slots, DB connections)
- [`PacedResourcePlace`][cpnx.PacedResourcePlace] — permit pool with per-token cooldown (API rate limits)
- [`ThresholdPlace`][cpnx.ThresholdPlace]     — accumulates tokens until a batch threshold is met

**CPN alignment:** In Coloured Petri Net theory a place has a *colour set* —
the type of tokens it accepts. `color_set` exposes this directly.
[`ResourcePlace`][cpnx.ResourcePlace] and [`PacedResourcePlace`][cpnx.PacedResourcePlace] are Python
shorthands for a place whose colour set is ``{"resource"}`` with a pre-filled initial marking.
"""

import bisect
import heapq
import itertools
import math
import operator
import threading
import time
from collections import Counter, OrderedDict, deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any, ClassVar

from cpnx.tokens import AVAILABLE_NOW, Token

_seq_key = operator.itemgetter(0)
"""Sort key for `(seq, token)` pairs: the seq alone, so tokens are never compared."""

#: Rebuild a disabled key-index at most once per this many deposits, so a key that raises
#: intermittently (a "flapping" key) cannot trigger a full O(ready) heap rebuild on every
#: recovery. The re-arm path reads this live, so tests may monkeypatch it.
_KEY_INDEX_REARM_MIN_DEPOSITS = 64


class _KeyIndex:
    """A persistent ``(key, seq)`` min-heap over one [`_TokenStore`][]'s ready tokens.

    The same shape as the store's cooling heap, keyed by ``key_fn(token)`` instead of
    ``available_at``: entries are pushed once at deposit (so each key is computed exactly
    once, not re-derived per firing) and deleted **lazily** — an entry is stale once its
    ``seq`` is gone from the store's ready set, and is discarded permanently the first time
    it surfaces. ``seq`` is monotonic and never reused, so a stale entry can never be
    mistaken for a live one.

    Ordering is ``(key, seq)``: ascending key, insertion order within a tie — exactly what
    PR 1's stable ascending sort produced, which is what lets the index be a pure
    optimisation rather than a semantic change.

    **Self-disabling.** Any failure to key or order a token (`key_fn` raises; two keys are
    mutually incomparable, which surfaces from the heap comparison) disables the index
    until it is re-armed (`rearm`). The engine then falls back to the per-firing
    filter+sort, which reproduces the documented behaviour for those cases — including
    making the arc unsatisfiable — until the engine re-arms the index once selection
    succeeds again for the arc (see issue #34). A broken key must not turn a `deposit()`
    into an exception, and must not be able to corrupt the ordering of a partially-built
    heap, so the heap is dropped outright.

    When `key_fn` raised, `_bad_seq` remembers that token, and `rearm` tries no rebuild
    while it is still ready, since the rebuild would only re-key the whole ready set and
    fail on it again (the same gate as `_JoinIndex`). An incomparable key records nothing:
    which of the two tokens is at fault depends on heap order, so that case keeps only the
    rate limit.
    """

    __slots__ = ("key_fn", "_heap", "disabled", "_last_build", "_bad_seq")

    def __init__(self, key_fn: Callable[[Token], object]) -> None:
        self.key_fn = key_fn
        self._heap: list[tuple[object, int]] = []
        self.disabled = False
        self._last_build: int = 0
        self._bad_seq: int | None = None

    def clear(self) -> None:
        """Discard every entry, for a store that has just dropped all of its tokens."""
        self._heap.clear()
        self._bad_seq = None

    def add(self, seq: int, token: Token) -> bool:
        """Index *token* under *seq*. Returns ``False`` (and disables) if it cannot be keyed."""
        if self.disabled:
            return False
        try:
            key = self.key_fn(token)
        except Exception:
            self.disable()
            self._bad_seq = seq
            return False
        try:
            heapq.heappush(self._heap, (key, seq))
        except Exception:
            self.disable()
            return False
        return True

    def disable(self) -> None:
        """Stop serving this index and release its heap. Can be undone by `rearm`."""
        self.disabled = True
        self._heap = []

    def rearm(self, ready: "OrderedDict[int, Token]", deposit_clock: int) -> bool:
        """Re-enable a disabled index and repopulate it from *ready*; return whether it is
        now serving.

        A no-op returning ``True`` when the index is already enabled. Rebuilding walks the
        whole ready set (re-keying every token), so it is rate-limited to at most once per
        ``_KEY_INDEX_REARM_MIN_DEPOSITS`` deposits (``deposit_clock`` is the store's monotonic
        deposit counter): a key that raises intermittently cannot rebuild the heap on every
        recovery. When the limit has not yet elapsed this returns ``False`` and a later
        success retries. While the token whose key raised is still in *ready* this returns
        ``False`` at once, without re-keying anything. A back-fill that hits a token it still
        cannot key re-disables the index and returns ``False``.
        """
        if not self.disabled:
            return True
        if self._bad_seq is not None and self._bad_seq in ready:
            return False
        if deposit_clock - self._last_build < _KEY_INDEX_REARM_MIN_DEPOSITS:
            return False
        self._last_build = deposit_clock
        self.disabled = False
        self._heap = []
        for seq, token in ready.items():
            if not self.add(seq, token):
                return False  # still un-keyable — add() re-disabled the index
        return True

    def peek_ordered(
        self,
        ready: "OrderedDict[int, Token]",
        k: int,
        predicate: Callable[[Token], bool] | None = None,
    ) -> list[Token] | None:
        """Return up to *k* live ready tokens in ascending key order, non-destructively.

        ``None`` means "index unusable — fall back". *predicate* is applied at pop, so a
        token it rejects is skipped but **stays indexed** (it may satisfy a different arc,
        or the same arc later); scanning therefore continues past it rather than stopping,
        and a highly selective predicate degrades to a full ordered walk rather than
        silently returning short.

        Entries popped during the scan are pushed back in a ``finally``, so an exception
        out of *predicate* leaves the heap intact for the fallback path to re-derive.
        """
        if self.disabled:
            return None
        if k <= 0:
            return []
        popped: list[tuple[object, int]] = []
        found: list[Token] = []
        try:
            while self._heap and len(found) < k:
                entry = heapq.heappop(self._heap)
                token = ready.get(entry[1])
                if token is None:
                    continue  # stale: the token left the ready set — drop the entry for good
                popped.append(entry)
                if predicate is None or predicate(token):
                    found.append(token)
        finally:
            for entry in popped:
                heapq.heappush(self._heap, entry)
        return found


_JoinEntry = tuple[int, float, int]
"""A join-index entry: ``(tier, available_at, seq)``, with tier 0 for a ready token and 1 for a timed one."""

_JOIN_GENERATIONS = itertools.count(1)
"""Source of `_JoinIndex.generation` values, shared so that no two indexes ever hand out the same one."""


_HeadReport = tuple[int, dict[object, float] | None, dict[object, float | None]]
"""What `_JoinIndex.head_changes` returns: ``(generation, every_head, changes)``."""


def _join_head_time(group: list[_JoinEntry], depth: int = 1) -> float | None:
    """When a join group's first *depth* tokens are all available: ``-inf`` if they are ready now.

    That is when its *depth*-th entry becomes available, since entries are in read order.
    ``None`` when the group holds fewer than *depth* tokens.
    """
    if len(group) < depth:
        return None
    head = group[depth - 1]
    return head[1] if head[0] else -math.inf


class _JoinIndex:
    """A persistent ``join_key -> tokens`` index over one [`_TokenStore`][]'s tokens (#64).

    Each group is a list of `_JoinEntry` sorted by ``(tier, available_at, seq)``. That is the
    store's read order: ready tokens first in insertion order, then timed tokens by
    ``(available_at, seq)``. A token is available at *t_limit* when it is ready or its
    ``available_at`` is at or before *t_limit*, so the available entries of a group are always
    a prefix of it, found by one bisection.

    Unlike `_KeyIndex` this covers timed tokens too, and deletes eagerly: `_key_of` remembers
    each token's key by seq, so a removal finds its group directly. The cost is O(group size)
    per insert or removal, which is what an equi-join over many distinct keys wants.

    **Self-disabling.** A key that raises or is unhashable disables the index, dropping its
    groups, and the engine falls back to computing the keys itself (which then reports the
    failure). A disabled index re-enables at once when the store empties, and otherwise through
    `rearm`, which rebuilds it from every stored token. `_bad_seq` remembers the token that
    disabled it, and no rebuild is tried while that token is still stored, so a long-lived bad
    token costs one O(1) check per lookup rather than a re-keying of the whole place. Once it
    has left, rebuilds run at most once per `_KEY_INDEX_REARM_MIN_DEPOSITS` deposits, as
    `_KeyIndex.rearm` does, so a place that never drains gets its index back within that many
    deposits.

    **Head changes.** The engine keeps, per join transition, the keys that every join place
    holds (`cpnx.engine._JoinMatch`, ADR 0011). It needs to hear when a group's *head* changes,
    because a group's head decides when the key becomes available. The head is the group's
    first `depth` entries: a self-join, whose `depth` join arcs read one place and take one
    token each (issue #75), can use a key only once that many of its tokens are available, so
    its index reports a key only from then on, and its `_JoinMatch` lists only keys that bind.
    `head_changes` hands it the keys whose head changed since its last call, which `add` and
    `discard` collect once a reader has asked. `generation` changes whenever the groups are
    replaced wholesale (disabled, cleared or rebuilt), and a reader that sees a new one gets
    every head instead. Each index has exactly one reader; see `head_changes`.
    """

    __slots__ = (
        "key_fn",
        "depth",
        "_groups",
        "_key_of",
        "disabled",
        "_last_build",
        "_bad_seq",
        "_dirty",
        "generation",
    )

    def __init__(self, key_fn: Callable[[Token], object], depth: int = 1) -> None:
        self.key_fn = key_fn
        self.depth = depth
        self._groups: dict[object, list[_JoinEntry]] = {}
        self._key_of: dict[int, object] = {}
        self.disabled = False
        self._last_build = 0
        self._bad_seq: int | None = None
        self._dirty: set[object] | None = None
        self.generation = next(_JOIN_GENERATIONS)

    def rearm(self, tokens: Iterable[tuple[int, Token]], deposit_clock: int, is_stored: Callable[[int], bool]) -> bool:
        """Re-enable a disabled index by rebuilding it from *tokens* (seq, token); return whether it serves.

        A no-op returning ``True`` when the index is enabled. While the token that disabled it
        is still stored (*is_stored* answers by seq) a rebuild would only fail again, so this
        returns ``False`` at once. A rebuild re-keys every token, so it also runs at most once
        per `_KEY_INDEX_REARM_MIN_DEPOSITS` deposits (*deposit_clock* is the store's monotonic
        deposit counter). A rebuild that meets another token it cannot key disables the index
        again, remembering that token, and returns ``False``.
        """
        if not self.disabled:
            return True
        if self._bad_seq is not None and is_stored(self._bad_seq):
            return False
        if deposit_clock - self._last_build < _KEY_INDEX_REARM_MIN_DEPOSITS:
            return False
        self._last_build = deposit_clock
        self.disabled = False
        self.generation = next(_JOIN_GENERATIONS)
        for seq, token in tokens:
            self.add(seq, token)
            if self.disabled:
                return False
        return True

    def add(self, seq: int, token: Token) -> None:
        """Index *token* under *seq*, disabling the index if it cannot be keyed."""
        if self.disabled:
            return
        try:
            key = self.key_fn(token)
            group = self._groups.setdefault(key, [])
        except Exception:
            self.disable()
            self._bad_seq = seq
            return
        entry = (0, 0.0, seq) if token.available_at == AVAILABLE_NOW else (1, token.available_at, seq)
        if not group or group[-1] < entry:
            group.append(entry)
            at = len(group) - 1
        else:
            at = bisect.bisect_left(group, entry)
            group.insert(at, entry)
        self._key_of[seq] = key
        if at < self.depth and self._dirty is not None:
            self._dirty.add(key)

    def discard(self, seq: int, token: Token) -> None:
        """Remove *token*'s entry, if it has one."""
        if self.disabled or seq not in self._key_of:
            return
        key = self._key_of.pop(seq)
        group = self._groups[key]
        entry = (0, 0.0, seq) if token.available_at == AVAILABLE_NOW else (1, token.available_at, seq)
        at = bisect.bisect_left(group, entry)
        del group[at]
        if not group:
            del self._groups[key]
        if at < self.depth and self._dirty is not None:
            self._dirty.add(key)

    def clear(self) -> None:
        """Discard every entry and re-enable, for a store that has just become empty."""
        self._groups = {}
        self._key_of = {}
        self.disabled = False
        self._bad_seq = None
        self._dirty = None
        self.generation = next(_JOIN_GENERATIONS)

    def disable(self) -> None:
        """Stop serving this index and release its entries until the store next empties."""
        self.disabled = True
        self._groups = {}
        self._key_of = {}
        self._dirty = None
        self.generation = next(_JOIN_GENERATIONS)

    def head_changes(self, generation: int | None) -> "_HeadReport":
        """Report the group heads that changed since a reader last saw *generation*.

        Returns ``(generation, every_head, changes)``. When *generation* is this index's
        current one, *every_head* is ``None`` and *changes* maps each key whose group head
        changed since the previous call to its new `_join_head_time`, or to ``None`` if the key
        has fewer than `depth` tokens left. Otherwise (including a first call with ``None``)
        *every_head* maps every key with at least `depth` tokens to its head time and *changes*
        is empty. Either way change tracking restarts
        from here, so each change is reported once.

        **One reader per index.** A call drains the changes, so a second reader would miss
        them. The engine registers one index per (transition, input position) for that reason,
        even when two transitions share an `InputArc` object. Once a reader has asked, changes
        are collected until the index is cleared or disabled, whether or not it asks again.
        That set is bounded by the index's distinct keys.
        """
        groups, depth = self._groups, self.depth
        if generation != self.generation:
            self._dirty = set()
            every = {key: _join_head_time(group, depth) for key, group in groups.items() if len(group) >= depth}
            return self.generation, every, {}  # type: ignore[return-value]  # filtered to depth
        dirty = self._dirty
        if not dirty:
            return self.generation, None, {}
        self._dirty = set()
        changes: dict[object, float | None] = {}
        for key in dirty:
            group = groups.get(key)
            changes[key] = _join_head_time(group, depth) if group else None
        return self.generation, None, changes

    def available_groups(
        self,
        keys: Iterable[object] | None,
        t_limit: float,
        ready: "OrderedDict[int, Token]",
        timed: dict[int, Token],
    ) -> "dict[object, _JoinGroup]":
        """Map each of *keys* (or every key, for `None`) to a view of its tokens available at *t_limit*.

        A key with no available token is left out. *ready* and *timed* are the store's token
        maps by seq. Because a group's available entries are a prefix of it, its view is cut
        with one bisection, or none when its last entry is available. Nothing is copied, so
        this costs O(keys looked up), however many tokens share a key.
        """
        groups = self._groups
        result: dict[object, _JoinGroup] = {}
        cut = (1, t_limit, math.inf)
        for key in groups if keys is None else keys:
            group = groups.get(key)
            if not group:
                continue
            head = group[0]
            if head[0] and head[1] > t_limit:
                continue
            last = group[-1]
            end = bisect.bisect_right(group, cut) if last[0] and last[1] > t_limit else len(group)
            result[key] = _JoinGroup(group, end, cut, ready, timed)
        return result


class _JoinGroup:
    """The first `end` entries of one `_JoinIndex` group, read lazily as `(entry, token)` pairs.

    A view, not a copy: the engine reads it within one resolution under its lock, while the
    marking is fixed, so a join over a deep group pays only for the entries it reaches (the
    same argument as the engine's `_PeekStream`).

    Something may still change the place directly while the view is read, without the engine
    lock. A token that has left is then skipped. An insert or removal also shifts the live
    list under the read, so the reader keeps the entries strictly increasing and no later than
    *cut*: an entry shifted back into view is not yielded twice, and one shifted in from past
    the cut is not yielded at all. As with any stale read, a chosen token that is gone by
    consume time makes `retrieve_specific` raise and the firing roll back.
    """

    __slots__ = ("_entries", "_end", "_cut", "_ready", "_timed")

    def __init__(
        self,
        entries: list[_JoinEntry],
        end: int,
        cut: tuple[int, float, float],
        ready: "OrderedDict[int, Token]",
        timed: dict[int, Token],
    ) -> None:
        self._entries = entries
        self._end = end
        self._cut = cut
        self._ready = ready
        self._timed = timed

    def __len__(self) -> int:
        return self._end

    def __iter__(self) -> "Iterator[tuple[_JoinEntry, Token]]":
        ready, timed, cut = self._ready, self._timed, self._cut
        last: _JoinEntry | tuple[()] = ()
        for entry in itertools.islice(self._entries, self._end):
            if entry <= last:
                continue
            if entry > cut:
                return
            last = entry
            token = (timed if entry[0] else ready).get(entry[2])
            if token is not None:
                yield entry, token


def _first_repeated(tokens: list[Token]) -> Token:
    """Return the first token of *tokens* that also appears earlier in it, by identity."""
    seen: set[int] = set()
    for token in tokens:
        if id(token) in seen:
            return token
        seen.add(id(token))
    raise ValueError("no token is repeated")


class _TokenStore:
    """Backing store for [`Place`][cpnx.Place] giving O(1) arbitrary removal and O(1)
    (amortized) earliest-available checks, in place of an O(n)-per-firing deque rebuild.

    Tokens whose ``available_at == AVAILABLE_NOW`` (always available — the overwhelming
    common case) live in `_ready`, an `OrderedDict` keyed by a monotonic insertion
    sequence number so iteration order is insertion order. Timed tokens live in two
    `heapq` min-heaps of ``(available_at, seq)`` split at `_migrated_to`: `_matured`
    holds entries at or before it and `_cooling` holds entries after it. Only the boundary
    query, `earliest_cooling_after`, moves the split: it migrates entries from `_cooling` to
    `_matured` as its finite time moves past them, so a matured backlog is never re-walked to
    find the next future boundary, and it re-splits the heaps (`_resplit`) when it arrives
    behind the split, which is what keeps a query on one clock from pinning the split for
    another. FIFO reads only walk `_matured` then `_cooling` and never move it. Removing the
    last timed token resets both heaps and the split to ``-inf``. Both heaps are
    lazily deleted: an entry is stale once its ``seq`` is no longer in `_cooling_by_seq`
    (the authoritative membership for every timed token, matured or not). `_seq_of` maps
    ``id(token)`` to its seq for O(1) removal by object identity.

    **Ordering contract:** every read yields available tokens in ``(available_at, seq)``
    order. Because `AVAILABLE_NOW` (`0.0`) sorts below any real timestamp, a store
    holding only ready tokens yields pure insertion order — identical to the FIFO
    deque semantics this replaces. Reads walk `_ready`, then `_matured`, then `_cooling`;
    the `_migrated_to` split keeps every `_matured` entry below every `_cooling` entry, so
    the concatenation is still in order.

    Not internally synchronized: callers must hold the owning place's lock.
    """

    def __init__(self) -> None:
        self._ready: OrderedDict[int, Token] = OrderedDict()
        self._cooling: list[tuple[float, int]] = []
        self._matured: list[tuple[float, int]] = []
        self._migrated_to: float = -math.inf
        self._cooling_by_seq: dict[int, Token] = {}
        self._seq_of: dict[int, int] = {}
        self._next_seq: int = 0
        self._key_indexes: dict[int, _KeyIndex] = {}
        self._join_indexes: dict[int, _JoinIndex] = {}

    def register_key_index(self, index_id: int, key_fn: Callable[[Token], object]) -> None:
        """Maintain a persistent ``(key, seq)`` min-heap over the ready set for *key_fn*.

        Idempotent per *index_id*: re-registering the same id with the same function is a
        no-op, and with a *different* function rebuilds from scratch (an arc's ``key`` may
        be reassigned after construction). Registration back-fills from the tokens already
        present, so an index can be added to a non-empty place.
        """
        existing = self._key_indexes.get(index_id)
        if existing is not None and existing.key_fn is key_fn:
            return
        index = _KeyIndex(key_fn)
        self._key_indexes[index_id] = index
        for seq, token in self._ready.items():
            if not index.add(seq, token):
                break  # keying failed — the index disabled itself; leave it to fall back

    def rearm_key_index(self, index_id: int) -> bool:
        """Re-enable *index_id*'s disabled index, repopulating from the current ready set.

        Returns whether it is serving afterwards. ``False`` if there is no such index, the
        per-deposit rebuild limit has not elapsed, or the ready set still holds an un-keyable
        token. The deposit clock is `_next_seq`, which advances on every `append`.
        """
        index = self._key_indexes.get(index_id)
        if index is None:
            return False
        return index.rearm(self._ready, self._next_seq)

    def key_index(self, index_id: int) -> "_KeyIndex | None":
        """Return the registered index for *index_id*, or ``None`` if there is none."""
        return self._key_indexes.get(index_id)

    def register_join_index(self, index_id: int, key_fn: Callable[[Token], object], depth: int = 1) -> None:
        """Maintain a persistent `_JoinIndex` for *key_fn* over every stored token.

        Idempotent per *index_id*, function and *depth* (see `_JoinIndex`), rebuilt when either
        changes, and back-filled from the tokens already present, like `register_key_index`.
        """
        existing = self._join_indexes.get(index_id)
        if existing is not None and existing.key_fn is key_fn and existing.depth == depth:
            return
        index = _JoinIndex(key_fn, depth)
        self._join_indexes[index_id] = index
        for seq, token in [*self._ready.items(), *self._cooling_by_seq.items()]:
            index.add(seq, token)
            if index.disabled:
                break

    def unregister_join_index(self, index_id: int) -> None:
        """Stop maintaining the join index registered under *index_id*, if there is one."""
        self._join_indexes.pop(index_id, None)

    def join_index(self, index_id: int) -> "_JoinIndex | None":
        """Return the serving join index for *index_id*, or ``None`` if absent or still disabled.

        A disabled index is offered a rate-limited `_JoinIndex.rearm` first, rebuilt in store
        read order, so a keying failure is not permanent while the store stays non-empty.
        """
        index = self._join_indexes.get(index_id)
        if index is None:
            return None
        if index.disabled and not index.rearm(self._join_rebuild_order(), self._next_seq, self._holds_seq):
            return None
        return index

    def _holds_seq(self, seq: int) -> bool:
        """Return whether the token at *seq* is still stored."""
        return seq in self._ready or seq in self._cooling_by_seq

    def _join_rebuild_order(self) -> Iterable[tuple[int, Token]]:
        """Every stored `(seq, token)`, ready then timed, for rebuilding a join index."""
        return itertools.chain(self._ready.items(), self._cooling_by_seq.items())

    def join_groups(
        self, index_id: int, keys: Iterable[object] | None, t_limit: float
    ) -> "dict[object, _JoinGroup] | None":
        """Group the tokens available at *t_limit* by join key, or ``None`` if *index_id* is not serving.

        See `_JoinIndex.available_groups`.
        """
        index = self.join_index(index_id)
        if index is None:
            return None
        return index.available_groups(keys, t_limit, self._ready, self._cooling_by_seq)

    def join_head_changes(self, index_id: int, generation: int | None) -> "_HeadReport | None":
        """`_JoinIndex.head_changes` for *index_id*, or ``None`` if that index is not serving."""
        index = self.join_index(index_id)
        if index is None:
            return None
        return index.head_changes(generation)

    def append(self, token: Token) -> None:
        """Add *token*, routing it to the ready set or the cooling heap by `available_at`."""
        seq = self._next_seq
        self._next_seq += 1
        self._seq_of[id(token)] = seq
        if token.available_at == AVAILABLE_NOW:
            self._ready[seq] = token
            for index in self._key_indexes.values():
                index.add(seq, token)
        else:
            self._push_timed((token.available_at, seq))
            self._cooling_by_seq[seq] = token
        for join_index in self._join_indexes.values():
            join_index.add(seq, token)

    def __len__(self) -> int:
        return len(self._ready) + len(self._cooling_by_seq)

    def _push_timed(self, entry: tuple[float, int]) -> None:
        """Push a timed ``(available_at, seq)`` entry onto whichever heap its time belongs to.

        An entry at or before `_migrated_to` goes to `_matured` even when it is new, such as a
        past-dated deposit, because every `_cooling` entry must stay after `_migrated_to`.
        """
        heapq.heappush(self._matured if entry[0] <= self._migrated_to else self._cooling, entry)

    def _push_timed_all(self, entries: Iterable[tuple[float, int]]) -> None:
        """Restore entries popped by a non-destructive read; see `_push_timed`."""
        for entry in entries:
            self._push_timed(entry)

    def _migrate(self, t_limit: float) -> None:
        """Move `_cooling` entries at or before *t_limit* to `_matured`, if time moved forward.

        Each entry migrates at most once, so the cost is amortized O(log n) per token. A
        non-finite *t_limit* never migrates, so NaN or ``inf`` can never become the split; an
        ``inf`` split would file every future token as matured. Stale entries met on the way
        are discarded.
        """
        if not math.isfinite(t_limit) or t_limit <= self._migrated_to:
            return
        while self._cooling and self._cooling[0][0] <= t_limit:
            entry = heapq.heappop(self._cooling)
            if entry[1] in self._cooling_by_seq:
                heapq.heappush(self._matured, entry)
        self._migrated_to = t_limit

    def _resplit(self, now: float) -> None:
        """Move live `_matured` entries after *now* back to `_cooling` and lower the split to *now*.

        O(n) once, then forward reads from *now* are back on the fast path. Called only from
        `earliest_cooling_after`, whose pre-split cost was already O(n) per call, so a caller
        alternating between two clocks is never slower than that. Drops stale entries.
        """
        keep: list[tuple[float, int]] = []
        later: list[tuple[float, int]] = []
        for entry in self._matured:
            if entry[1] in self._cooling_by_seq:
                (later if entry[0] > now else keep).append(entry)
        heapq.heapify(keep)
        self._matured = keep
        if later:
            self._cooling.extend(later)
            heapq.heapify(self._cooling)
        self._migrated_to = now

    def _pop_matured_cooling(self, t_limit: float, limit: int) -> list[tuple[float, int]]:
        """Pop up to *limit* live timed ``(available_at, seq)`` entries due by *t_limit*.

        Walks `_matured` then `_cooling` in ascending order, discarding stale entries
        permanently along the way. Does NOT restore anything — callers that need a
        non-destructive read must hand the result to `_push_timed_all`.

        Does not migrate: a FIFO read on the wall clock would otherwise file every token
        dated before it as matured, and the next logical-time boundary query would re-split
        them all back.
        """
        matured: list[tuple[float, int]] = []
        for heap in (self._matured, self._cooling):
            while heap and len(matured) < limit:
                avail_at, seq = heap[0]
                if seq not in self._cooling_by_seq:
                    heapq.heappop(heap)
                    continue
                if avail_at > t_limit:
                    break
                matured.append(heapq.heappop(heap))
        return matured

    def has_available(self, t_limit: float, need: int) -> bool:
        """Return ``True`` as soon as *need* available tokens are known to exist.

        `_ready` tokens are always available, so if `need` is already covered by
        `len(_ready)` this is O(1). Otherwise pops just enough matured cooling
        entries off the heap to decide, then pushes them back (non-destructive).
        """
        if need <= 0:
            return True
        if need <= len(self._ready):
            return True
        shortfall = need - len(self._ready)
        matured = self._pop_matured_cooling(t_limit, shortfall)
        self._push_timed_all(matured)
        return len(matured) >= shortfall

    def count_available(self, t_limit: float) -> int:
        """Return the exact number of currently available tokens.

        Not O(1) — walks all matured cooling entries. Intended for diagnostic
        messages on the (rare) error/raise path, not the hot success path.
        """
        count = len(self._ready)
        matured = self._pop_matured_cooling(t_limit, len(self._cooling_by_seq))
        count += len(matured)
        self._push_timed_all(matured)
        return count

    def peek_available(self, k: int, t_limit: float) -> list[Token]:
        """Return up to *k* available tokens in `(available_at, seq)` order, non-destructively."""
        if k <= 0:
            return []
        result = list(itertools.islice(self._ready.values(), k))
        remaining = k - len(result)
        if remaining <= 0:
            return result
        matured = self._pop_matured_cooling(t_limit, remaining)
        result.extend(self._cooling_by_seq[seq] for _, seq in matured)
        self._push_timed_all(matured)
        return result

    def peek_by_key(
        self, index_id: int, k: int, predicate: Callable[[Token], bool] | None = None
    ) -> list[Token] | None:
        """Return up to *k* available tokens in ascending key order, or ``None`` to fall back.

        ``None`` is returned whenever the index cannot be trusted to represent the whole
        available pool:

        - no index is registered for *index_id*, or it disabled itself (see `_KeyIndex`);
        - **the place holds cooling tokens.** A key index covers `_ready` only, and a
          timed token never migrates into `_ready` even after it matures — it is served
          off the timed heaps — so any timed token in this place puts available
          tokens outside the index's view. Rather than return a silently incomplete
          ordering, defer to the caller's per-firing scan. This is the timed×key residual:
          a keyed place that also holds timed tokens keeps PR 1's cost.

        The check is dynamic, not decided at registration: a place that merely *might*
        receive a timed token still gets the fast path for as long as it does not hold one.
        """
        if self._cooling_by_seq:
            return None
        index = self._key_indexes.get(index_id)
        if index is None:
            return None
        return index.peek_ordered(self._ready, k, predicate)

    def take_available(self, k: int, t_limit: float) -> list[Token]:
        """Remove and return up to *k* available tokens in `(available_at, seq)` order."""
        if k <= 0:
            return []
        taken: list[Token] = []
        ready_pairs = list(itertools.islice(self._ready.items(), k))
        for seq, token in ready_pairs:
            del self._ready[seq]
            del self._seq_of[id(token)]
            taken.append(token)
            self._unjoin(seq, token)
        remaining = k - len(taken)
        if remaining > 0:
            matured = self._pop_matured_cooling(t_limit, remaining)
            for _, seq in matured:
                token = self._cooling_by_seq.pop(seq)
                del self._seq_of[id(token)]
                taken.append(token)
                self._unjoin(seq, token)
            self._reset_if_untimed()
        self._reenable_join_indexes_if_empty()
        return taken

    def _unjoin(self, seq: int, token: Token) -> None:
        """Remove a token that just left the store from every join index."""
        for join_index in self._join_indexes.values():
            join_index.discard(seq, token)

    def _reenable_join_indexes_if_empty(self) -> None:
        """Re-enable every disabled join index once the store holds no token (see `_JoinIndex`)."""
        if self._join_indexes and not self._ready and not self._cooling_by_seq:
            for join_index in self._join_indexes.values():
                if join_index.disabled:
                    join_index.clear()

    def drop(self, seq: int) -> None:
        """Remove the token at *seq* from whichever set holds it (heap entry left lazily stale)."""
        token = self._ready.pop(seq, None)
        if token is None:
            token = self._cooling_by_seq.pop(seq, None)
            if token is None:
                return
            self._reset_if_untimed()
        self._unjoin(seq, token)
        self._reenable_join_indexes_if_empty()

    def _reset_if_untimed(self) -> None:
        """Empty both timed heaps and reset the split once the store holds no timed token."""
        if not self._cooling_by_seq:
            self._cooling.clear()
            self._matured.clear()
            self._migrated_to = -math.inf

    def remove_identity(self, tokens: Iterable[Token]) -> None:
        """Remove each of *tokens* by object identity. Callers must validate presence first."""
        for token in tokens:
            seq = self._seq_of.pop(id(token), None)
            if seq is not None:
                self.drop(seq)

    def drop_all(self) -> None:
        """Remove every stored token, keeping key-index registrations but not their entries."""
        for seq in [*self._ready, *self._cooling_by_seq]:
            self.drop(seq)
        self._seq_of.clear()
        for index in self._key_indexes.values():
            index.clear()
        for join_index in self._join_indexes.values():
            join_index.clear()

    def missing_identities(self, ids: set[int]) -> set[int]:
        """Return the subset of `id(token)` values in *ids* that are not currently stored.

        Expected O(len(ids)), independent of how many tokens are stored: `set.difference` with
        an exact `dict` argument walks the set and does one average-O(1) lookup per requested id,
        in C. `set - dict.keys()` would instead walk every stored key, making each
        `retrieve_specific` cost O(place size).
        """
        return ids.difference(self._seq_of)

    def earliest_cooling_after(self, now: float) -> float | None:
        """Smallest timed ``available_at`` strictly greater than *now*, or ``None``.

        O(1) when nothing is timed (the untimed common case — both heaps are empty). Otherwise
        matured entries are moved out of the way and the answer is the live head of `_cooling`:
        amortized O(log n) however deep the matured backlog is. A *now* behind `_migrated_to`
        (a read on another clock, such as the wall clock before a net switches to logical time)
        first re-splits the heaps at *now*, an O(n) step paid once per move backwards. A NaN or
        `-inf` *now* scans both heaps, as before the split existed.
        """
        if (not self._cooling and not self._matured) or now == math.inf:
            return None
        if not math.isfinite(now):
            candidates = [self._first_live_after(heap, now) for heap in (self._matured, self._cooling)]
            return min((c for c in candidates if c is not None), default=None)
        if now < self._migrated_to:
            self._resplit(now)
        else:
            self._migrate(now)
        return self._first_live_after(self._cooling, now)

    def _first_live_after(self, heap: list[tuple[float, int]], now: float) -> float | None:
        """Return the first live ``available_at`` after *now* in *heap*, leaving *heap* intact.

        Stale entries are discarded for good; live entries at or before *now* are buffered and
        pushed back.
        """
        buffered: list[tuple[float, int]] = []
        result: float | None = None
        while heap:
            avail_at, seq = heap[0]
            if seq not in self._cooling_by_seq:
                heapq.heappop(heap)
                continue
            if avail_at > now:
                result = avail_at
                break
            buffered.append(heapq.heappop(heap))
        for entry in buffered:
            heapq.heappush(heap, entry)
        return result

    def iter_insertion_order(self) -> Iterable[Token]:
        """Yield ALL tokens (ready + cooling) in insertion-sequence order.

        When nothing is cooling (the common case) `_ready` is already in seq order, so this
        is O(n) with no sort; the merge-sort is paid only when timed tokens are present.
        """
        if not self._cooling_by_seq:
            return list(self._ready.values())
        items = list(self._ready.items()) + list(self._cooling_by_seq.items())
        items.sort(key=_seq_key)
        return [token for _, token in items]


class Place:
    """An unbounded FIFO queue that holds tokens flowing through a Petri net.

    **CPN equivalent:** a place with an unrestricted colour set (accepts any
    colour) and no initial marking. Set ``color_set`` to restrict accepted
    colours; set ``initial_marking`` to pre-fill with tokens at construction.

    All operations are thread-safe via an internal `threading.Lock`.
    """

    _restorable: ClassVar[bool] = True
    """Whether `_restore_tokens` may replace this place's whole contents.

    A sink is never restored, and a pool rolls back by count through `_roll_back_to`.
    """

    def __init__(
        self,
        name: str,
        bound: int | None = None,
        color_set: set[str] | None = None,
        initial_marking: list[Token] | None = None,
        schema: type | Callable[[Any], bool] | None = None,
    ) -> None:
        """Create a new Place.

        Args:
            name: Unique identifier for this place within a [`PetriNet`][cpnx.PetriNet].
            bound: Optional k-bound (capacity constraint). The engine will not
                   fire a transition whose unguarded output arc targets this place
                   if doing so would exceed the bound. ``None`` (default) means
                   unbounded — this is standard CPN k-bounded place semantics.
            color_set: Set of accepted token colours. ``None`` (default) accepts
                       any colour. Pass e.g. ``{"data", "priority"}`` to enforce
                       typing at deposit time.
            initial_marking: Tokens to deposit at construction (CPN *I* function).
                             Deposited before any external code runs.
            schema: Optional schema or type constraint for token payloads. Can be a
                    type (checked via ``isinstance``) or a callable predicate
                    returning a bool. ``None`` (default) accepts any payload. Resource
                    permit tokens (``color="resource"``) are exempt, since permits carry
                    no payload — a payload schema never applies to them.

        Note:
            Token payloads are always coerced to an immutable [`FrozenDict`][cpnx.FrozenDict]
            (a ``Mapping``, not a ``dict`` subclass), so a type schema is only useful as
            ``dict``/``Mapping`` (always satisfied) or an ``ABC`` that ``FrozenDict`` registers
            under. To validate payload *contents*, pass a callable predicate.
        """
        self.name = name
        self.bound = bound
        self.color_set = color_set
        self.schema = schema
        self._store = _TokenStore()
        self._lock = threading.Lock()
        self.last_deposit_time: float = 0.0
        self.last_deposit_time_model: float = 0.0

        for token in initial_marking or []:
            if self.color_set is not None and token.color not in self.color_set:
                raise TypeError(
                    f"Place '{self.name}' has color_set {self.color_set!r} — "
                    f"cannot deposit token with color {token.color!r}."
                )
            self._enforce_schema(token)
            self._store.append(token)
            self.last_deposit_time = time.monotonic()

    def _schema_failure_reason(self, payload: Any) -> str | None:
        """Return ``None`` if *payload* conforms to `schema`, else a human-readable reason.

        The reason always contains the word "schema" (callers wrap it in a ``TypeError``) and,
        when a callable schema *raises*, includes that exception via ``repr`` — so a buggy or
        over-strict predicate is diagnosable rather than silently collapsed to a bare rejection.
        A raising predicate still counts as a rejection (the payload could not be validated).

        Args:
            payload: The token payload to check against `schema`.

        Returns:
            ``None`` when valid; otherwise a reason string describing the violation.
        """
        if self.schema is None:
            return None
        if isinstance(self.schema, type):
            if self.schema is dict and isinstance(payload, Mapping):
                return None
            if isinstance(payload, self.schema):
                return None
            return f"payload {payload!r} does not match schema {self.schema!r}."
        if callable(self.schema):
            try:
                ok = bool(self.schema(payload))
            except Exception as exc:
                return f"payload {payload!r} does not match schema {self.schema!r} (predicate raised {exc!r})."
            if ok:
                return None
            return f"payload {payload!r} does not match schema {self.schema!r}."
        return f"payload {payload!r} does not match schema {self.schema!r} (schema is neither a type nor callable)."

    def validate_schema(self, payload: Any) -> bool:
        """Validate whether *payload* conforms to this place's schema.

        Thin boolean wrapper over `_schema_failure_reason` (which carries the diagnostic detail).

        Args:
            payload: The token payload to check against `schema`.

        Returns:
            ``True`` if `schema` is ``None``, or if *payload* is an instance of `schema`
            (when `schema` is a type), or if ``schema(payload)`` evaluates to truthy
            (when `schema` is a callable). ``False`` otherwise, including when a callable
            schema raises an exception.
        """
        return self._schema_failure_reason(payload) is None

    def _enforce_schema(self, token: Token) -> None:
        """Raise ``TypeError`` if *token* violates this place's schema.

        Resource permit tokens (``color="resource"``) are exempt: a permit carries no payload,
        so `schema` — a *payload* constraint — never applies to it. Enforcing it would reject
        every auto-generated permit (empty payload) and, on the transition return path, silently
        route the permit to the error place instead of the pool. See `is_resource`.
        """
        if token.is_resource:
            return
        reason = self._schema_failure_reason(token.payload)
        if reason is not None:
            raise TypeError(f"Color Set / Schema Violation in Place '{self.name}': {reason}")

    def deposit(self, token: Token, model_time: float | None = None) -> None:
        """Append *token* to the tail of the FIFO queue, enforcing the place's colour set.

        Updates `last_deposit_time` (and `last_deposit_time_model` if *model_time* is given),
        then calls `_on_deposit`.

        Args:
            token: The token to deposit.
            model_time: Optional logical clock timestamp recorded alongside the deposit.
                        Does not affect wall-clock availability checks.

        Raises:
            TypeError: If `color_set` is set and *token*'s colour is not in it,
                       or if `schema` is set and *token*'s payload fails validation.
        """
        with self._lock:
            if self.color_set is not None and token.color not in self.color_set:
                raise TypeError(
                    f"Place '{self.name}' has color_set {self.color_set!r} — "
                    f"cannot deposit token with color {token.color!r}."
                )
            self._enforce_schema(token)
            self._store.append(token)
            self.last_deposit_time = time.monotonic()
            if model_time is not None:
                self.last_deposit_time_model = model_time
            self._on_deposit(token)

    def _on_deposit(self, token: Token) -> None:
        """Extension hook called immediately after a token is deposited (under the place lock).

        Override in subclasses to react to new arrivals. Default is a no-op.

        Args:
            token: The token that was just deposited.
        """

    def retrieve(self, count: int = 1, model_time: float | None = None) -> list[Token]:
        """Remove and return *count* tokens from the head of the queue, in FIFO order.

        A token is only eligible if its `available_at` timestamp is at or before the
        effective time (`model_time` if given, else `time.monotonic()`); tokens still
        in the future (e.g. cooling down) are skipped.

        Args:
            count: Number of tokens to retrieve. Must be >= 1.
            model_time: Optional logical clock timestamp used instead of wall-clock time
                        to determine which tokens are available.

        Returns:
            List of retrieved tokens in FIFO order.

        Raises:
            ValueError: If fewer than *count* tokens are available.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            if not self._store.has_available(t_limit, count):
                available_count = self._store.count_available(t_limit)
                raise ValueError(
                    f"Place '{self.name}': cannot retrieve {count} token(s) — only {available_count} available."
                )
            return self._store.take_available(count, t_limit)

    def retrieve_specific(self, tokens: list[Token], model_time: float | None = None) -> list[Token]:
        """Remove and return exactly the given *tokens*, matched by ``id`` rather than FIFO order.

        Used by the engine when an [`InputArc`][cpnx.InputArc] has a ``key``/``filter`` that
        selects a specific subset of tokens to consume rather than the head of the queue.

        Args:
            tokens: Tokens to remove, identified by their ``id``. Each must currently be
                    present in this place and available at the effective time.
            model_time: Optional logical clock timestamp used instead of wall-clock time
                        to check token availability.

        Returns:
            The removed tokens, in the same order as *tokens* (not necessarily FIFO order).

        Raises:
            ValueError: If any token in *tokens* is not yet available (its `available_at`
                        is after the effective time), is not found in this place, or appears
                        more than once in *tokens*.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            for t in tokens:
                if t.available_at > t_limit:
                    raise ValueError(
                        f"Place '{self.name}': token {t.id} is not yet available at {t_limit} "
                        f"(available_at={t.available_at})."
                    )
            ids = set(map(id, tokens))
            if len(ids) != len(tokens):
                repeated = _first_repeated(tokens).id
                raise ValueError(f"Place '{self.name}': token {repeated} passed more than once to retrieve_specific.")
            missing_identities = self._store.missing_identities(ids)
            if missing_identities:
                missing = {t.id for t in tokens if id(t) in missing_identities}
                raise ValueError(
                    f"Place '{self.name}': token id(s) {missing} not found — "
                    f"cannot retrieve_specific tokens that are not present."
                )
            self._store.remove_identity(tokens)
            return tokens

    def retrieve_all(self, model_time: float | None = None) -> list[Token]:
        """Remove and return every currently-available token, leaving not-yet-available tokens behind.

        Args:
            model_time: Optional logical clock timestamp used instead of wall-clock time
                        to determine which tokens are available.

        Returns:
            All available tokens in FIFO order; empty list if none are available.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            return self._store.take_available(len(self._store), t_limit)

    def _return_token(self, token: Token) -> None:
        """Put back a token this place gave up, unchanged and without re-applying any deposit rule."""
        with self._lock:
            self._store.append(token)

    def _restore_tokens(self, tokens: tuple[Token, ...]) -> bool:
        """Replace the contents with exactly *tokens*, bypassing colour, schema and pacing.

        Used to roll a subnet back after a failed substitution firing; *tokens* is an earlier
        `tokens` snapshot of this same place. Returns whether the contents changed.
        """
        if not self._restorable:
            raise TypeError(f"{type(self).__name__} '{self.name}' is never restored from a checkpoint")
        with self._lock:
            current = self._store.iter_insertion_order()
            if len(current) == len(tokens) and all(a is b for a, b in zip(current, tokens, strict=True)):
                return False
            self._store.drop_all()
            for token in tokens:
                self._store.append(token)
            return True

    def _roll_back_to(self, tokens: tuple[Token, ...]) -> bool:
        """Return this place to its `tokens` snapshot from a checkpoint; whether anything changed.

        Resource pools override this to restore only their permit count.
        """
        return self._restore_tokens(tokens)

    def peek(self, count: int = 1, model_time: float | None = None) -> list[Token]:
        """Return up to *count* available tokens from the head without removing them.

        Args:
            count: Maximum number of tokens to inspect.
            model_time: Optional logical clock timestamp used instead of wall-clock time
                        to determine which tokens are available.

        Returns:
            List of up to *count* tokens; may be shorter than requested if fewer
            are present or available. Does not modify the queue.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            if count <= 0:
                return []
            return self._store.peek_available(count, t_limit)

    def register_key_index(self, index_id: int, key_fn: Callable[[Token], object]) -> None:
        """Maintain a persistent ascending-key index over this place's ready tokens.

        Registered by the engine at net-build time for each [`InputArc`][cpnx.InputArc]
        whose `key` is *certified* (see [`cpnx.certification`]) — an uncertified key is not
        indexed, because keying happens on the deposit path where an unbounded callable
        cannot be allowed to run. *index_id* identifies the registering arc; re-registering
        the same id with a different function rebuilds the index, so reassigning `arc.key`
        after construction stays correct. A disabled index can also be re-armed in place,
        without changing the function, via [`rearm_key_index`][cpnx.Place.rearm_key_index]
        (issue #34).

        Registration back-fills from the tokens already present, so it is safe on a
        non-empty place. This is a pure optimisation: every read through
        [`peek_by_key`][cpnx.Place.peek_by_key] can decline, and the engine then computes
        the same answer the slow way.
        """
        with self._lock:
            self._store.register_key_index(index_id, key_fn)

    def rearm_key_index(self, index_id: int) -> bool:
        """Re-enable a disabled key-index for *index_id* and repopulate it from the ready set.

        Returns whether the index is serving afterwards. Called by the engine when selection
        succeeds again for an arc whose index disabled itself (issue #34): a keying failure is
        no longer permanent — once the offending token is gone the fast path is restored,
        rebuilt at most once per `_KEY_INDEX_REARM_MIN_DEPOSITS` deposits to bound the cost of a
        flapping key.
        """
        with self._lock:
            return self._store.rearm_key_index(index_id)

    def key_index_disabled(self, index_id: int) -> bool:
        """Whether *index_id*'s index exists and has disabled itself.

        Distinct from "cannot answer right now" (see
        [`peek_by_key`][cpnx.Place.peek_by_key], which also declines while the place holds
        cooling tokens): this is specifically the state a keying failure leaves behind, which
        is reversible via [`rearm_key_index`][cpnx.Place.rearm_key_index] (issue #34). The
        engine reads it when reporting a selection fault, so the report mentions the lost
        index only when one was actually lost — a raising `filter` never touches the index,
        and an uncertified key never has one.
        """
        with self._lock:
            index = self._store.key_index(index_id)
            return index is not None and index.disabled

    def peek_by_key(
        self, index_id: int, count: int, predicate: Callable[[Token], bool] | None = None
    ) -> list[Token] | None:
        """Return up to *count* available tokens in ascending key order, or ``None``.

        ``None`` means the index cannot answer and the caller must fall back to its own
        ordering — no index registered, the index disabled itself after a keying failure,
        or the place currently holds timed (cooling) tokens the index does not cover.

        *predicate* is applied at pop: a rejected token is skipped but stays indexed, and
        the scan continues past it, so a `filter` never causes a short read.
        """
        with self._lock:
            if count <= 0:
                return []
            return self._store.peek_by_key(index_id, count, predicate)

    def register_join_index(self, index_id: int, key_fn: Callable[[Token], object], depth: int = 1) -> None:
        """Maintain a persistent index from `key_fn(token)` to this place's tokens.

        Registered by the engine for each [`InputArc`][cpnx.InputArc] whose `join_key` is
        *certified* (see [`cpnx.certification`]), because keying runs on the deposit path.
        *index_id* identifies the registering arc. Re-registering the same id with a different
        function rebuilds the index, and registration back-fills from the tokens already
        present. Like the key index, this is a pure optimisation: [`peek_join`][cpnx.Place.peek_join]
        may decline, and the engine then computes the same groups itself.

        *depth* is how many join arcs of the registering transition read this place with
        *key_fn*, each taking a different token. [`join_head_changes`][cpnx.Place.join_head_changes]
        then reports a key only while it holds at least that many tokens. Changing it rebuilds
        the index.
        """
        with self._lock:
            self._store.register_join_index(index_id, key_fn, depth)

    def unregister_join_index(self, index_id: int) -> None:
        """Drop the join index registered under *index_id*, so deposits stop keying for it.

        The engine calls this when the registering arc no longer reads this place through that
        index: its `join_key` was set back to `None`, or the place was replaced. Unknown ids are
        ignored.
        """
        with self._lock:
            self._store.unregister_join_index(index_id)

    def peek_join(
        self, index_id: int, keys: Iterable[object] | None, model_time: float | None = None
    ) -> "dict[object, Iterable[tuple[tuple[int, float, int], Token]]] | None":
        """Return this place's available tokens grouped by join key, or ``None`` to fall back.

        Args:
            index_id: The id the join index was registered under.
            keys: The join keys to look up, or `None` for every key with an available token.
                A key with no available token is left out of the result.
            model_time: Optional logical clock timestamp used instead of wall-clock time to
                decide which tokens are available.

        Returns:
            Each key mapped to its available tokens in this place's read order, each paired
            with a sort position that orders them the same way. A group is a sized, lazily
            read view of the index, valid until the place next changes. `None` when no index
            is registered under *index_id* or it disabled itself after a keying failure.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            return self._store.join_groups(index_id, keys, t_limit)

    def join_head_changes(self, index_id: int, generation: int | None) -> "_HeadReport | None":
        """Report which join keys' earliest tokens changed, for the engine's cross-place key match.

        For the join index registered under *index_id*, returns ``(generation, every_head,
        changes)``. With the *generation* the previous call returned, *every_head* is ``None``
        and *changes* maps each key whose earliest tokens changed since then to the time they
        are all available (``-inf`` when they are ready now), or to ``None`` when the key has
        left. Otherwise *every_head* maps every key to that time and *changes* is empty. "Earliest
        tokens" means the first `depth` given at registration, one by default; a key with fewer
        is reported as left. ``None`` when the index is not registered or not serving, exactly
        as for [`peek_join`][cpnx.Place.peek_join].
        """
        with self._lock:
            return self._store.join_head_changes(index_id, generation)

    def can_retrieve(self, count: int = 1, model_time: float | None = None) -> bool:
        """Return ``True`` if at least *count* tokens are currently available for retrieval.

        Args:
            count: Number of tokens needed. Defaults to 1.
            model_time: Optional logical clock timestamp used instead of wall-clock time
                        to determine which tokens are available.

        Returns:
            ``True`` if at least *count* tokens have `available_at` at or before the
            effective time, ``False`` otherwise.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            return self._store.has_available(t_limit, count)

    def earliest_available_boundary(self, now: float) -> float | None:
        """Smallest future ``available_at`` (strictly greater than *now*) held here, or ``None``.

        Backs the engine's logical-clock advance. O(1) for an untimed place (nothing is
        cooling), and O(log n) amortized otherwise, however many matured tokens are waiting
        here to be consumed, so the clock advance does not scan the marking on every tick.
        A *now* earlier than the previous boundary query's (for example the engine's wall-clock
        query followed by a switch to a small logical time) pays one O(n) re-split, after which
        queries from that time on are back to O(log n). Reads such as `peek` and
        `can_retrieve` do not affect this, whichever clock they use.
        """
        with self._lock:
            return self._store.earliest_cooling_after(now)

    def can_deposit(self, count: int = 1) -> bool:
        """Return ``True`` if the place can accept *count* more tokens without exceeding its bound.

        Implements k-bounded place semantics: a place with ``bound=k`` blocks when
        depositing would push the token count above ``k``. Unbounded places
        (``bound=None``) always return ``True``. Ignores colour — use `can_accept`
        to check colour compatibility.

        Args:
            count: Number of tokens to be deposited. Defaults to 1.

        Returns:
            ``True`` if depositing *count* tokens would not exceed `bound`, ``False`` otherwise.
        """
        with self._lock:
            if self.bound is None:
                return True
            return len(self._store) + count <= self.bound

    def can_accept(self, token: Token) -> bool:
        """Return ``True`` if *token* is compatible with this place's colour set and schema.

        This is a non-mutating pre-flight check that does not modify the place's tokens
        and does not consider capacity — use `can_deposit` for bound checks.

        Args:
            token: The token to check for colour compatibility and schema validation.

        Returns:
            ``True`` if `color_set` is ``None`` or contains *token*'s colour and
            `validate_schema` returns ``True``, ``False`` otherwise. Resource permit
            tokens are exempt from the schema check (see `_enforce_schema`).
        """
        with self._lock:
            if self.color_set is not None and token.color not in self.color_set:
                return False
            if not token.is_resource and not self.validate_schema(token.payload):
                return False
            return True

    @property
    def tokens(self) -> tuple[Token, ...]:
        """Snapshot of all current tokens (including not-yet-available ones) as an immutable tuple.

        Does not filter by `available_at` and does not consume or remove any tokens.
        """
        with self._lock:
            return tuple(self._store.iter_insertion_order())

    def __len__(self) -> int:
        """Return the number of tokens currently in the place."""
        with self._lock:
            return len(self._store)

    def __bool__(self) -> bool:
        """A Place is always truthy, even when it contains no tokens."""
        return True


class ResourcePlace(Place):
    """A [`Place`][cpnx.Place] pre-filled with *capacity* resource tokens, for modelling finite permits.

    **CPN equivalent:** ``Place(color_set={"resource"}, initial_marking=[Token(color="resource")] * capacity)``.
    This class is a Python shorthand — it sets the colour set and initial marking
    automatically and documents the resource-return invariant explicitly. It does not
    otherwise change [`Place`][cpnx.Place]'s behavior: all inherited methods (`deposit`,
    `retrieve`, etc.) work exactly as on the base class. One exception: across a
    [`SubstitutionTransition`][cpnx.SubstitutionTransition] boundary a port and its socket
    must both be `ResourcePlace`s or neither, so a plain `Place` written as above does not
    stand in for a pool on either side.

    Resource tokens (``color="resource"``) are consumed when a transition fires
    and must be returned via a matching output arc. This models finite resources
    such as GPU slots, database connections, or thread-pool permits.

    Example:
        ```python
        gpu_pool = ResourcePlace("gpu_slots", capacity=4)
        ```
    """

    _restorable: ClassVar[bool] = False

    def __init__(
        self,
        name: str,
        capacity: int,
        schema: type | Callable[[Any], bool] | None = None,
    ) -> None:
        """Create a ResourcePlace pre-filled with *capacity* resource tokens.

        Args:
            name: Unique identifier for this place within a [`PetriNet`][cpnx.PetriNet].
            capacity: Number of resource permits in the pool. ``0`` is valid
                      (creates an empty, permanently-blocking place).
            schema: Optional schema or type constraint for token payloads. Resource
                    permit tokens are exempt (they carry no payload). See [`Place`][cpnx.Place].
        """
        self.capacity = capacity
        super().__init__(
            name,
            color_set={"resource"},
            initial_marking=[Token(color="resource") for _ in range(capacity)],
            schema=schema,
        )

    def _roll_back_to(self, tokens: tuple[Token, ...]) -> bool:
        return self._restore_permits(tokens)

    def _restore_permits(self, tokens: tuple[Token, ...]) -> bool:
        """Bring the pool back to the number of permits in *tokens*, its checkpoint snapshot.

        Permits are interchangeable, and a settled firing has handed back every one it used, so
        only the count is rolled back. The permits here stay as they are: a surplus (a port's
        copy from the parent) gives up the soonest available, which keeps any cooldown running,
        and a shortfall (a permit left in another pool) is made up from the snapshot. Returns
        whether the contents changed.
        """
        with self._lock:
            current = list(self._store.iter_insertion_order())
            surplus = len(current) - len(tokens)
            if surplus == 0:
                return False
            if surplus > 0:
                dropped = {id(t) for t in sorted(current, key=lambda t: t.available_at)[:surplus]}
                kept = [t for t in current if id(t) not in dropped]
            else:
                present = Counter(id(t) for t in current)
                missing = []
                for token in tokens:
                    if present[id(token)] > 0:
                        present[id(token)] -= 1
                    else:
                        missing.append(token)
                kept = current + missing[:-surplus]
            self._store.drop_all()
            for token in kept:
                self._store.append(token)
            return True


class PacedResourcePlace(ResourcePlace):
    """A [`ResourcePlace`][cpnx.ResourcePlace] where returned tokens must cool down before becoming reusable.

    **CPN equivalent:** a Timed CPN [`ResourcePlace`][cpnx.ResourcePlace] where returned tokens
    carry a timestamp that prevents re-use until ``pacing_secs`` have elapsed.
    This is a pragmatic extension — standard Timed CPNs put timestamps on tokens,
    not cooldown windows on places.

    Useful for enforcing API rate limits or minimum inter-request intervals.
    Tokens are available immediately at construction; after each return via
    [`deposit`][cpnx.PacedResourcePlace.deposit], they are unavailable for *pacing_secs* seconds.

    Example — 10 Serper requests per second:
        ```python
        serper = PacedResourcePlace("serper", capacity=10, pacing_secs=0.1)
        ```
    """

    def __init__(
        self,
        name: str,
        capacity: int,
        pacing_secs: float,
        schema: type | Callable[[Any], bool] | None = None,
    ) -> None:
        """Create a PacedResourcePlace.

        Args:
            name: Unique identifier for this place within a [`PetriNet`][cpnx.PetriNet].
            capacity: Number of resource permits in the pool.
            pacing_secs: Seconds a token must wait after being returned before
                         it becomes available again.
            schema: Optional schema or type constraint for token payloads. Resource
                    permit tokens are exempt (they carry no payload). See [`Place`][cpnx.Place].
        """
        self.pacing_secs = pacing_secs
        super().__init__(name, capacity, schema=schema)

    def deposit(self, token: Token, model_time: float | None = None) -> None:
        """Return a resource token to the pool, replacing its `available_at` to start a cooldown timer.

        Differs from [`Place.deposit`][cpnx.Place.deposit]: instead of appending *token* unchanged,
        this creates a copy of *token* with `available_at` set to the effective time plus
        `pacing_secs`, so the token cannot be retrieved again until the cooldown elapses.
        Does not validate `color_set` (unlike the base class).

        Args:
            token: The resource token being returned. Must have ``color="resource"``.
            model_time: Optional logical clock timestamp used instead of wall-clock time
                        as the cooldown's start reference, and recorded in
                        `last_deposit_time_model`.

        Raises:
            TypeError: If `schema` is set and *token*'s payload fails validation. Resource
                permit tokens (the usual case here) are exempt — see `_enforce_schema`.
        """
        with self._lock:
            self._enforce_schema(token)
            ref_time = model_time if model_time is not None else time.monotonic()
            # Create a new token with updated availability timestamp (stateless place cooldown)
            timed_token = token.evolve(available_at=ref_time + self.pacing_secs, id=token.id)
            self._store.append(timed_token)
            self.last_deposit_time = time.monotonic()
            if model_time is not None:
                self.last_deposit_time_model = model_time
            self._on_deposit(timed_token)

    def can_retrieve(self, count: int = 1, model_time: float | None = None) -> bool:
        """Return ``True`` if at least *count* tokens have completed their cooldown and are usable.

        Behaves identically to [`Place.can_retrieve`][cpnx.Place.can_retrieve]; documented
        separately here because "available" specifically means "cooldown has expired"
        for this class.

        Args:
            count: Number of cooled-down tokens needed. Defaults to 1.
            model_time: Optional logical clock timestamp used instead of wall-clock time
                        to determine which tokens have finished cooling down.

        Returns:
            ``True`` if at least *count* tokens are past their cooldown, ``False`` otherwise.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            return self._store.has_available(t_limit, count)

    def retrieve(self, count: int = 1, model_time: float | None = None) -> list[Token]:
        """Remove and return *count* tokens whose cooldown has expired, in expiry order.

        Behaves like [`Place.retrieve`][cpnx.Place.retrieve] but the error message reports
        how many tokens are still cooling down, which is specific to this class's semantics.

        Args:
            count: Number of cooled-down tokens to retrieve. Defaults to 1.
            model_time: Optional logical clock timestamp used instead of wall-clock time
                        to determine which tokens have finished cooling down.

        Returns:
            List of retrieved resource tokens in cooldown-expiry order.

        Raises:
            ValueError: If fewer than *count* tokens are past their cooldown, with
                        a message indicating how many are ready vs still cooling down.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            if not self._store.has_available(t_limit, count):
                available_count = self._store.count_available(t_limit)
                cooling = len(self._store) - available_count
                raise ValueError(
                    f"PacedResourcePlace '{self.name}': {available_count} token(s) ready, "
                    f"{count} requested — {cooling} token(s) still in cooldown "
                    f"(pacing_secs={self.pacing_secs})."
                )
            return self._store.take_available(count, t_limit)

    def peek(self, count: int = 1, model_time: float | None = None) -> list[Token]:
        """Return up to *count* cooled-down tokens without removing them.

        Behaves identically to [`Place.peek`][cpnx.Place.peek]; "available" specifically
        means "cooldown has expired" for this class.

        Args:
            count: Maximum number of tokens to inspect. Defaults to 1.
            model_time: Optional logical clock timestamp used instead of wall-clock time
                        to determine which tokens have finished cooling down.

        Returns:
            List of up to *count* cooled-down tokens; may be shorter than requested.
            Does not modify the pool.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            if count <= 0:
                return []
            return self._store.peek_available(count, t_limit)


class ThresholdPlace(Place):
    """A [`Place`][cpnx.Place] where tokens are only retrievable once the queue depth reaches *threshold*.

    **CPN equivalent:** a plain [`Place`][cpnx.Place] whose associated transition has a
    guard requiring ``|M(p)| >= threshold`` before firing. This class is a Python
    shorthand that encodes the threshold directly on the place rather than
    duplicating it in every downstream transition's guard.

    Useful for batch processing: tokens accumulate until enough are present,
    then they are released in groups matching the transition's ``arc.count``.
    `deposit` and `peek` are inherited unchanged from [`Place`][cpnx.Place].

    Example — convene a committee once 6 validated leads are ready:
        ```python
        validated = ThresholdPlace("validated_leads", threshold=6)
        ```
    """

    def __init__(
        self,
        name: str,
        threshold: int,
        schema: type | Callable[[Any], bool] | None = None,
    ) -> None:
        """Create a ThresholdPlace.

        Args:
            name: Unique identifier for this place within a [`PetriNet`][cpnx.PetriNet].
            threshold: Minimum queue depth required before any retrieval is
                       permitted. Must be >= 1.
            schema: Optional schema or type constraint for token payloads. Resource
                    permit tokens are exempt (they carry no payload). See [`Place`][cpnx.Place].
        """
        super().__init__(name, schema=schema)
        self.threshold = threshold

    def can_retrieve(self, count: int = 1, model_time: float | None = None) -> bool:
        """Return ``True`` only if the batch threshold is met AND at least *count* tokens are present.

        Differs from [`Place.can_retrieve`][cpnx.Place.can_retrieve]: adds a gating condition
        on top of the plain count check — the queue must have reached `threshold` regardless
        of *count*, and separately contain at least *count* available tokens (*count* may
        exceed `threshold`).

        Args:
            count: Number of tokens needed by the requesting transition arc. Defaults to 1.
            model_time: Optional logical clock timestamp used instead of wall-clock time
                        to determine which tokens are available.

        Returns:
            ``True`` if both the threshold and *count* conditions hold, ``False`` otherwise.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            return self._store.has_available(t_limit, max(self.threshold, count))

    def retrieve(self, count: int = 1, model_time: float | None = None) -> list[Token]:
        """Remove and return *count* tokens from the head of the queue, but only if the threshold is met.

        Differs from [`Place.retrieve`][cpnx.Place.retrieve]: first checks that the queue
        has reached `threshold` available tokens (raising if not) before applying the
        usual *count* check, gating retrieval behind the batch threshold.

        Args:
            count: Number of tokens to retrieve. Defaults to 1.
            model_time: Optional logical clock timestamp used instead of wall-clock time
                        to determine which tokens are available.

        Returns:
            List of retrieved tokens in FIFO order.

        Raises:
            ValueError: If the threshold is not yet met, with a message showing
                        current depth vs required threshold.
            ValueError: If the threshold is met but fewer than *count* tokens are
                        available.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            if self._store.has_available(t_limit, max(self.threshold, count)):
                return self._store.take_available(count, t_limit)
            available_count = self._store.count_available(t_limit)
            if available_count < self.threshold:
                raise ValueError(
                    f"ThresholdPlace '{self.name}': threshold of {self.threshold} not met "
                    f"({available_count} token(s) available — need {self.threshold - available_count} more)."
                )
            raise ValueError(
                f"ThresholdPlace '{self.name}': threshold met but only {available_count} "
                f"token(s) available, {count} requested."
            )

    def retrieve_all(self, model_time: float | None = None) -> list[Token]:
        """Remove and return every available token, but only if the threshold has been met.

        Differs from [`Place.retrieve_all`][cpnx.Place.retrieve_all]: raises instead of
        returning an empty list when fewer than `threshold` tokens are available.

        Args:
            model_time: Optional logical clock timestamp used instead of wall-clock time
                        to determine which tokens are available.

        Returns:
            All available tokens in FIFO order.

        Raises:
            ValueError: If the threshold is not yet met, with a message showing
                        current depth vs required threshold.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            if self._store.has_available(t_limit, self.threshold):
                return self._store.take_available(len(self._store), t_limit)
            available_count = self._store.count_available(t_limit)
            raise ValueError(
                f"ThresholdPlace '{self.name}': threshold of {self.threshold} not met "
                f"({available_count} token(s) available — need {self.threshold - available_count} more)."
            )


class SinkPlace(Place):
    """A terminal place that counts and optionally samples tokens but never retains them for retrieval.

    Deposited tokens are absorbed: their colour and cumulative counts are recorded, and up
    to `keep_last` of the most recent tokens are kept in a ring buffer purely for inspection
    via `tokens`/`stats`/`drain_stats` — but they can never be consumed onward by a transition.
    Useful for streaming pipelines (e.g. logging sinks, dead-letter/error places) to avoid
    accumulating memory indefinitely while still exposing aggregate statistics.

    Note:
        A sink is a terminal ring buffer, not a retrievable queue, so it does NOT use
        `_TokenStore` internally — it keeps its own bounded
        `collections.deque(maxlen=keep_last)` and overrides every method that would
        otherwise touch the base class's store.

    Warning:
        Avoid setting a restrictive `color_set` if this place is used as an `error_place`.
        Dead-lettered tokens preserve their original colours, and depositing a rejected
        colour will raise a `TypeError` inside the locked transition failure branch,
        causing the token to be lost rather than successfully dead-lettered.
    """

    _restorable: ClassVar[bool] = False

    def __init__(
        self,
        name: str,
        *,
        keep_last: int = 0,
        color_set: set[str] | None = None,
        schema: type | Callable[[Any], bool] | None = None,
    ) -> None:
        """Create a new SinkPlace.

        Args:
            name: Unique identifier for this place within a [`PetriNet`][cpnx.PetriNet].
            keep_last: Number of most recent tokens to keep in a ring buffer for inspection.
                       Default is 0 (retain nothing beyond the aggregate counters).
            color_set: Set of accepted token colours. ``None`` (default) accepts any colour.
                       Do not use a restrictive color_set if used as an error_place.
            schema: Optional schema or type constraint for token payloads. Resource
                    permit tokens are exempt (they carry no payload). See [`Place`][cpnx.Place].
        """
        super().__init__(name, bound=None, color_set=color_set, schema=schema)
        self.keep_last = keep_last
        self._kept: deque[Token] = deque(maxlen=keep_last)
        self._absorbed = 0
        self._by_color: dict[str | None, int] = {}
        self._first_deposit_time: float | None = None

    def deposit(self, token: Token, model_time: float | None = None) -> None:
        """Absorb *token*: append it to the ring buffer and update cumulative counters and timestamps.

        Differs from [`Place.deposit`][cpnx.Place.deposit]: the internal deque has
        `maxlen=keep_last`, so once full, appending silently evicts the oldest kept
        token — this is a sampling buffer, not the full token history. Also increments
        the `_absorbed` count and the per-colour tally, and records `_first_deposit_time`
        on the very first deposit.

        Args:
            token: The token to absorb.
            model_time: Optional logical clock timestamp recorded in `last_deposit_time_model`.

        Raises:
            TypeError: If `color_set` is set and *token*'s colour is not in it,
                       or if `schema` is set and *token*'s payload fails validation.
        """
        with self._lock:
            if self.color_set is not None and token.color not in self.color_set:
                raise TypeError(
                    f"Place '{self.name}' has color_set {self.color_set!r} — "
                    f"cannot deposit token with color {token.color!r}."
                )
            self._enforce_schema(token)
            self._kept.append(token)
            now = time.monotonic()
            self.last_deposit_time = now
            if self._first_deposit_time is None:
                self._first_deposit_time = now
            if model_time is not None:
                self.last_deposit_time_model = model_time

            self._absorbed += 1
            self._by_color[token.color] = self._by_color.get(token.color, 0) + 1

            self._on_deposit(token)

    def can_retrieve(self, count: int = 1, model_time: float | None = None) -> bool:
        """Always return ``False``: a sink is a terminal place, so nothing is ever retrievable.

        Differs from [`Place.can_retrieve`][cpnx.Place.can_retrieve]: arriving tokens are
        absorbed for inspection/counting only and can never be consumed onward by a
        transition, so this unconditionally reports nothing is retrievable. *count* and
        *model_time* are accepted for interface compatibility but ignored.

        Args:
            count: Ignored.
            model_time: Ignored.

        Returns:
            ``False``, always.
        """
        return False

    def retrieve(self, count: int = 1, model_time: float | None = None) -> list[Token]:
        """Always raise: a sink is terminal, so tokens cannot be retrieved by any means.

        Differs from [`Place.retrieve`][cpnx.Place.retrieve]: never returns tokens, since
        absorbed tokens are only for inspection, not downstream consumption. *count* and
        *model_time* are accepted for interface compatibility but ignored.

        Args:
            count: Ignored.
            model_time: Ignored.

        Raises:
            ValueError: Always, with message "SinkPlace is terminal — tokens are absorbed,
                        not retrievable".
        """
        raise ValueError("SinkPlace is terminal — tokens are absorbed, not retrievable")

    def retrieve_specific(self, tokens: list[Token], model_time: float | None = None) -> list[Token]:
        """Always raise: a sink is terminal, so no tokens — specific or otherwise — can be retrieved.

        Differs from [`Place.retrieve_specific`][cpnx.Place.retrieve_specific]: never
        returns tokens, since absorbed tokens are only for inspection. *tokens* and
        *model_time* are accepted for interface compatibility but ignored.

        Args:
            tokens: Ignored.
            model_time: Ignored.

        Raises:
            ValueError: Always, with message "SinkPlace is terminal — tokens are absorbed,
                        not retrievable".
        """
        raise ValueError("SinkPlace is terminal — tokens are absorbed, not retrievable")

    def retrieve_all(self, model_time: float | None = None) -> list[Token]:
        """Always raise: a sink is terminal, so its absorbed tokens can never be drained via retrieval.

        Differs from [`Place.retrieve_all`][cpnx.Place.retrieve_all]: never returns tokens.
        Use `drain_stats` to reset the aggregate counters instead. *model_time* is accepted
        for interface compatibility but ignored.

        Args:
            model_time: Ignored.

        Raises:
            ValueError: Always, with message "SinkPlace is terminal — tokens are absorbed,
                        not retrievable".
        """
        raise ValueError("SinkPlace is terminal — tokens are absorbed, not retrievable")

    def peek(self, count: int = 1, model_time: float | None = None) -> list[Token]:
        """Always raise: use the `tokens` property to inspect a sink's ring buffer instead.

        Differs from [`Place.peek`][cpnx.Place.peek]: rather than returning a possibly-empty
        list, this raises, since a sink's kept tokens are only meant to be read via `tokens`
        or `stats`. *count* and *model_time* are accepted for interface compatibility but
        ignored.

        Args:
            count: Ignored.
            model_time: Ignored.

        Raises:
            ValueError: Always, with message "SinkPlace is terminal — tokens are absorbed,
                        not retrievable".
        """
        raise ValueError("SinkPlace is terminal — tokens are absorbed, not retrievable")

    def can_deposit(self, count: int = 1) -> bool:
        """Always return ``True``: a sink has unbounded capacity and absorbs every token offered.

        Differs from [`Place.can_deposit`][cpnx.Place.can_deposit]: ignores `bound`
        entirely (a `SinkPlace` is constructed with ``bound=None`` and never rejects
        on capacity grounds — only `color_set` can reject a deposit).

        Args:
            count: Ignored.

        Returns:
            ``True``, always.
        """
        return True

    @property
    def tokens(self) -> tuple[Token, ...]:
        """Snapshot of the ring buffer's currently-kept tokens (most recent `keep_last`), as a tuple.

        Differs from [`Place.tokens`][cpnx.Place.tokens]: reads the sink's own bounded
        `deque` ring buffer rather than a `_TokenStore` (a sink never routes deposits
        through the store).
        """
        with self._lock:
            return tuple(self._kept)

    def __len__(self) -> int:
        """Return the number of tokens currently kept in the ring buffer (at most `keep_last`).

        Differs from [`Place.__len__`][cpnx.Place.__len__]: reflects the ring buffer's
        size, not a `_TokenStore`'s — a sink's "length" is its sample size, not its
        (unbounded) absorbed count (see `stats()["absorbed"]` for that).
        """
        with self._lock:
            return len(self._kept)

    def stats(self) -> dict:
        """Return a snapshot of cumulative statistics of absorbed tokens, without resetting any counters.

        Example:
            ```python
            sink = SinkPlace("errors", keep_last=10)
            sink.deposit(Token(color="error"))
            sink.stats()
            # {"name": "errors", "absorbed": 1, "by_color": {"error": 1}, "kept": 1,
            #  "first_deposit_time": ..., "last_deposit_time": ...}
            ```

        Returns:
            Dictionary with keys ``name`` (str), ``absorbed`` (total tokens ever deposited,
            int), ``by_color`` (dict mapping colour to count), ``kept`` (number of tokens
            currently in the ring buffer), ``first_deposit_time`` (float or ``None`` if
            nothing has been deposited), and ``last_deposit_time`` (float, ``0.0`` if
            nothing has been deposited).
        """
        with self._lock:
            return {
                "name": self.name,
                "absorbed": self._absorbed,
                "by_color": dict(self._by_color),
                "kept": len(self._kept),
                "first_deposit_time": self._first_deposit_time,
                "last_deposit_time": self.last_deposit_time,
            }

    def drain_stats(self) -> dict:
        """Atomically return the current stats snapshot and reset the cumulative counters to zero.

        Differs from `stats`: after returning the snapshot, resets `_absorbed` to 0,
        `_by_color` to an empty dict, and `_first_deposit_time` to ``None`` (the ring buffer
        of kept tokens and `last_deposit_time` are left untouched). Useful for periodic
        reporting where each report should cover only the interval since the last drain.

        Returns:
            The stats dictionary as it was immediately before the reset — same shape as
            `stats` (``name``, ``absorbed``, ``by_color``, ``kept``, ``first_deposit_time``,
            ``last_deposit_time``).
        """
        with self._lock:
            snapshot = {
                "name": self.name,
                "absorbed": self._absorbed,
                "by_color": dict(self._by_color),
                "kept": len(self._kept),
                "first_deposit_time": self._first_deposit_time,
                "last_deposit_time": self.last_deposit_time,
            }
            self._absorbed = 0
            self._by_color = {}
            self._first_deposit_time = None
            return snapshot
