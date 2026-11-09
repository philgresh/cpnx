# ADR 0011 — Keep a join's matching keys up to date across its places

- **Status:** Accepted
- **Date:** 2026-11-09
- **Relates to:** [0010](0010-equi-join-index.md) (the join index this builds on, and whose
  last open consequence this closes), [0006](0006-incremental-enablement.md) (the scheduler
  that probes each join transition after every change to its places);
  https://github.com/philgresh/cpnx/issues/64, https://github.com/philgresh/cpnx/issues/59

## Context

ADR 0010 gave each place a join index (`places._JoinIndex`) from join key to that key's
tokens. A resolution read the smallest join place's keys, every one of them, and asked the
other places for each. So a probe cost O(keys in the smallest join place), however few keys
every place held.

That is the remaining cost in #59's `shop` net (`benchmarks/bench_shop_join.py`). Workers
scale with jobs (one per 130), and `resume` joins a job in `rejoin` with the idle worker of
the same `wid`. `idle` is the smaller place, holding about 90 keys at 16 000 jobs, while a
timed probe of `resume` matches none of them 95% of the time. The scheduler probes `resume`
after every change to `idle` or `rejoin`, and twice per change while timed tokens are cooling:
once at the current time and once ignoring timing. The timing-ignoring probe found about 68
matched keys and built a group view and a product for every one, only to ask whether any
candidate exists. Measured at 16 000 jobs, 200 000 such reads built 26 million group views.
µs/step grew 1.2 to 1.4× per doubling of the job count (567 µs/step at 16 000).

## Decision

### 1. A per-transition match over the places' heads

The engine keeps, per join transition, a `_JoinMatch`: the keys that **every** join place
holds, each with the time it becomes available in all of them.

A key's group in one place becomes available when its head does, because a group's available
tokens are a prefix of it (ADR 0010 §3). So a key is available everywhere at
`when(key) = max over places of head time`, with `-inf` for a ready head. The match keeps
`(when, uid)` for every held key in a sorted list, so the keys available at `t` are a prefix,
found with one bisection. `uid` stands in for the key, which need not be orderable.

### 2. Fed by head changes, not by reading the places

Each `_JoinIndex` notes the keys whose group head changed (`add` inserting at position 0,
`discard` removing position 0) once a reader has asked for them. A deposit behind the head
costs nothing extra. `Place.join_head_changes` hands the match those keys and their new head
times, under the place's lock, and the match keeps a mirror of each place's heads and
recomputes `when` for each key that changed. A probe therefore pays for what changed since
the last probe, plus one bisection, plus the keys it returns.

Each index carries a `generation`, taken from one global counter, that changes whenever its
groups are replaced wholesale: disabled, cleared (the store emptied, or `drop_all` for a
subnet restore), rebuilt by `rearm`, or replaced by a new index for a reassigned `join_key`.
A reader that sees a new generation gets every head and rebuilds from its mirrors.

**One reader per index.** A report drains the changes it hands over, so two readers of one
index would each miss what the other drained. The engine therefore registers one index per
(transition, input position), with ids from a module-wide counter, rather than per `InputArc`
object. Two transitions that share an `InputArc` object get two indexes on its place, each with
one reader. Once a reader has asked, the index keeps collecting changes until it is cleared or
disabled, even if the match is dropped (a switch to the wall clock, a fallback, or changed join
arcs) and nothing asks again. That set is bounded by the index's distinct keys.

### 3. Using it

`_join_candidates` asks the match for the available keys and then reads only those keys'
groups from the places, as before. The rest is unchanged: the per-key products, the merge into
product order, and the search limit. The candidates and their order are therefore exactly
ADR 0010's, which `tests/test_join_key.py` checks under all three policies.

An existence probe on a transition with no guard asks the match for **one** key. Any candidate
satisfies such a transition, so one available key answers the probe. That relies on ADR 0010
§1: a join arc takes exactly one token and has no `key` or `filter`, so every token of an
available group is a valid choice for its arc. `_join_candidates` asserts it, so lifting that
restriction must revisit this. The timing-ignoring probe in the `shop` net drops from
O(matched keys) to O(1).

### 4. When it does not serve

The match is an optimisation over the index, which is itself an optimisation. It serves only
when every join arc is served by a place join index (certified `join_key`, plain `Place`,
index enabled) and the probe has a definite time. Otherwise `_join_candidates` reads the keys
itself as before. The engine drops the match when the cause is structural or a matter of clock:

- an uncertified `join_key`, or a join place that is a `Place` subclass;
- the wall clock (`model_time=None`), where the place, not the engine, picks the read time.

The match registers any index it needs that is not registered yet, so it serves from a
transition's first probe.

A disabled index (a key that raised or was unhashable) is different: it is expected to come
back, and `rearm` is rate-limited, so it can stay down for many probes. The match is kept.
`refresh` stops at the first place whose index is not serving, after folding in what the
earlier places reported, so a probe that falls back costs those places' drains and never a
rebuild. When the index re-enables, its new generation makes the match rebuild once.

A match is replaced when its transition's join arcs, their index ids or their places change,
which it checks on every probe.

### 4a. Self-joins ([#75](https://github.com/philgresh/cpnx/issues/75))

Two or more join arcs can read one place. Each takes a different token, so where they share a
`join_key` function, a key binds there only once the place holds as many of its tokens under
that function as there are such arcs. Arcs with different functions match a key through
different tokens, one each, so each counts only the arcs that share its function (functions
compared by identity). Each index is
registered with that number as its **depth**, and its "head" is its group's first `depth`
entries: it reports a key, and the time it becomes available, only once its `depth`-th token
is in the group. The match then lists only keys that bind, and the one-key probe of §3 still
holds. A key whose tokens all sit in a backlog of one-token keys costs nothing to probe past,
as for an ordinary join. Changing the depth (an arc moved onto or off the place) replaces the
index.

The one-key probe does not apply when a join place is also read by an arc *without* a
`join_key`. That arc's options are the place's tokens, and when its `filter` accepts only the
matched key's token, another key would be needed. Such a probe asks for every key.

### 5. Concurrent changes

The match is fed under each place's lock in turn and read under the engine lock. A place
changed directly in between, without the engine lock, can leave the match listing a key that a
place has just lost, or missing one it has just gained. The groups are still read live from the
places, which drops a lost key, so the effect is a stale read, as with any other read of a
place that changes during resolution. The change is reported at the next probe.

## Consequences

- In `benchmarks/bench_shop_join.py` the `join_key` arm runs at a flat µs/step from 1 000 to
  16 000 jobs, growing 0.95 to 1.09× per doubling where it grew 1.17 to 1.35×. At 16 000
  jobs it takes 308 µs/step against 567 before, and 1 355 for the equality guard.
- A probe pays a fixed cost to refresh the match: two lock acquisitions, a few tuple builds
  and the key list. A join whose keys all match, such as `bench_guard_join.py`'s `hit` with 8
  workers, pays that and gains nothing: its probe rises from about 35 to 42 µs. Its drive is
  unchanged within noise (160 to 185 µs/step before and after).
- Each join index keeps a set of changed keys, bounded by its distinct keys, from the first
  time a match reads it until it is cleared or disabled.
- An `InputArc` object shared by two transitions is now indexed twice on its place, once per
  transition, so each deposit there keys the token twice.
- Each match keeps a mirror of every join place's heads, one entry per distinct key per place,
  and a sorted list of the keys all places hold.
- The order of the keys handed to the per-key products changes (by availability, then first
  match). It cannot change a result. The firing path merges the products back into product
  order, and the existence probe chains them only when every candidate fits in the search
  limit, where order cannot change whether one satisfies.
- Nets without `join_key`, and join transitions the match does not serve, take the same paths
  as before.
