# ADR 0011 — Keep a join's matching keys up to date across its places

- **Status:** Accepted — shipped in 0.8.0 via [#74](https://github.com/philgresh/cpnx/pull/74)
- **Date:** 2026-11-09
- **Relates to:** [0010](0010-equi-join-index.md) (the join index this builds on, and whose
  last open consequence this closes), [0006](0006-incremental-enablement.md) (the scheduler
  that probes each join transition after every change to its places);
  https://github.com/philgresh/cpnx/issues/64, https://github.com/philgresh/cpnx/issues/59,
  https://github.com/philgresh/cpnx/issues/75, https://github.com/philgresh/cpnx/issues/78
- **Amended:** §3 now opens keys by a lower bound on their candidates instead of merging every key's
  product, and §4a keeps Hall's condition per key in the places' reports instead of checking
  it per probe.

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

Each `_JoinIndex` notes the keys whose group head changed once a reader has asked for them.
The head is the group's first `depth` entries (§4a; one entry for the usual join), so that is
an `add` or `discard` at a position below `depth`. A deposit behind the head
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
(transition, place, `join_key` function), with ids from a module-wide counter, rather than per
`InputArc` object. Two transitions that share an `InputArc` object get two indexes on its place,
each with one reader. The join arcs of one transition that read a place with one function (a
self-join) share its index, which is the one the match reads for that function (§4a): every
registered index has a reader, and a self-join of `k` arcs keys each deposit once, not `k`
times. An arc whose `join_key` is reassigned moves to the index of its new function, and an
index no arc uses any more is unregistered. Once a reader has asked, the index keeps collecting
changes until it is cleared or disabled, even if the match is dropped (a switch to the wall
clock, or a fallback) and nothing asks again. That set holds at most one entry per distinct key
deposited since the last report.

### 3. Using it

An existence probe on a transition with no guard asks the match for **one** key: the first by
when it binds. Any candidate satisfies such a transition, so one key that binds answers the
probe. That relies on ADR 0010 §1: a join arc takes exactly one token and has no `key` or
`filter`, so every token of an available group is a valid choice for its arc.
`_join_candidates` asserts it, so lifting that restriction must revisit this. The
timing-ignoring probe in the `shop` net drops from O(matched keys) to O(1).

Every other search (firing, and a probe with a guard) walks the candidates in product order up
to `binding_search_limit`, and so needs the keys in that order. Merging one product per matched
key with `heapq.merge` reads every key's groups first: O(matched keys) per firing even when
the first candidate satisfies. So the match also keeps the keys sorted by **a lower bound on their
candidates** (`_JoinMatch.ordered_keys`), and the search opens them one at a time.

Every candidate of a key takes the same options on the arcs before the first join arc as any
other key's could (those arcs have no `join_key`, so their lists do not depend on it), and takes
a token of its own group at the first join arc. So the least positions of the arcs before it,
followed by the first join arc's first entry, a `(tier, available_at, seq)` tuple, bound every
candidate of the key from below. One token has one key under one function, so those entries
differ between keys, and they order the keys. Each place's report carries the entry with the
key's time (`Place.join_head_changes` returns `(when, first)`).

The bound is usually the key's first candidate. It is not when the search keeps the first join
arc off that token: an earlier arc without a `join_key` on its place took it, or the residual
Hall check of §4a drops it because a later join arc on the place needs it.
`_merge_from_bounds` therefore treats it as a bound. It holds a heap of the keys opened so far
and opens the next key while that key's bound comes before the smallest candidate open. Every
candidate of a key not yet opened comes at or after its bound, so this yields exactly
`heapq.merge`'s order, including the counted dead ends a key's search yields, which are padded
with the least positions of the arcs after them. While the bounds are tight it opens at most
one key per candidate yielded, plus one: `FIRST` with no guard opens two keys, and a search
that examines `n` candidates opens at most `n + 2` (it pulls one past the limit to learn it is
exhausted). A loose bound only opens a key early.

The keys that bind by `t` and their order by bound are two different orders, so the
match keeps the second as a sorted list per frontier (`_FirstOrder`): one for the clock, whose
frontier follows the probes' time, and one for the timing-ignoring horizon. A key enters the
clock's list when the clock reaches its time, found as a range of the `(when, uid)` order, so a
run whose clock moves forward pays once per key. A probe at an earlier time drops the keys in
between. Each key change updates both lists, at O(log keys) comparisons.

The candidates and their order are exactly ADR 0010's, which `tests/test_join_key.py` checks
under all three policies, and
`test_the_key_match_searches_exactly_as_the_fallback_does` checks against the fallback's full
merge on timed self-joins probed back and forth in time.

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
compared by identity). A place whose join arcs share one function reports a key, and the time
it becomes available, once the `arcs`-th token of its group is in it.

Where the functions differ, that count is only a necessary condition: a token whose key is the
same under two functions lists that key under each, though its only candidate hands it to both
arcs. The key binds there iff it passes **Hall's condition**: for every set of the place's
functions, the key's groups under them hold at least as many distinct tokens as there are arcs
using them. By Hall's theorem (P. Hall, 1935, on systems of distinct representatives) that is
exactly when each arc can take a token of its own. Arcs sharing a function share a group, so
checking sets of functions, with their full arc counts, is exact.

Whether a key passes depends only on that key's groups, so the place reports it like a head
([#78](https://github.com/philgresh/cpnx/issues/78)). The place's report reads one index per
function and gives, per key, the earliest time at which it passes (`places._hall_time`):

- A set of functions needs at most `D` tokens, `D` being every join arc on the place, so only
  each group's first `D` entries matter. The indexes there are registered with depth `D`, and
  hand over a key whenever one of its first `D` entries changes.
- The tokens available at a time are a prefix of each group, so the condition only gains as
  time passes. The report checks it at the latest of the single-function times first, then at
  each later entry time among those `D`, and returns the first that passes, or no time if
  none does.

That costs O(1) in the place's size per changed key: `c` functions read at most `c · D` entries
and check `2^c - c - 1` sets per candidate time. A deposit or removal changes one key per
function, so a probe's refresh rechecks that many keys. The match then lists only keys that
bind, as it does for one function, so the one-key probe of §3 serves mixed places too, and a
backlog of keys that fail Hall's condition costs nothing per probe once reported. A key that
fails it has no candidate that does not reuse a token, so leaving it out changes no binding,
and keeps such a backlog from using up `binding_search_limit`. Changing the depth (an arc moved
onto or off the place) replaces the index.

The fallback (§4), which reads every key, applies the same filters to the groups it reads:
`_with_enough_tokens` for each function's count and `_has_distinct_tokens` for Hall's
condition.

The invariants this section relies on, and the tests that hold the engine to them:

- **Distinct tokens.** No binding hands one token to two arcs. The CPN enabling rule asks
  that what all of a transition's arcs take from a place fits within its marking (Jensen,
  Kristensen and Wells 2007, §2.2–2.3); with token identity that is a choice of distinct
  representatives.
- **Hall-exactness.** A join transition with no guard is enabled iff some key passes Hall's
  condition at every join place. `test_a_self_join_binds_exactly_when_hall_condition_holds`
  checks this against a brute-force search, and
  `test_the_key_match_searches_exactly_as_the_fallback_does` checks the incremental state
  against the fallback's per-probe check, with timed tokens.
- **Probe and firing agree.** `_binding_exists` and `_resolve_binding` give the same answer
  (ADR 0010 §3). The same property test checks it, and
  `test_the_guard_free_probe_agrees_with_firing_past_the_search_limit` checks it with more
  keys than `binding_search_limit`, where the probe's shortcut could differ from firing.
- **Filters drop only keys that cannot bind.** Depth and Hall filtering may only shrink the
  candidates to those that do not reuse a token, never change which binding fires (ADR 0010
  §4).

Every arc on a shared place takes tokens the earlier arcs left there, so no candidate reuses a
token: join arcs with any `join_key`, and arcs *without* a `join_key`, whose options are drawn
from the remaining tokens. A residual Hall check prunes either kind's choice when it would strand
a later join arc on the place, such as an arc without a `join_key` read first taking a key's
only token there. Pruning drops only choices no binding extends, so it never changes which
binding fires. Any other choice that leaves a later arc nothing counts toward
`binding_search_limit`, which keeps the search bounded.

The one-key probe must answer as firing does, and firing merges every
key's candidates and dead ends in product order and stops at the limit. So it applies only
when no dead end can repeat once per key ahead of the first candidate. Two kinds of arc
without a `join_key` on a shared place rule it out:

- One that reads a join place. Its choices depend on the key, and its `filter` may accept only
  the matched key's token, so another key would be needed.
- One that sits before a join arc, on a place where some arc has a `filter`. If it takes the
  token a filtered arc needs, that is a dead end, and in product order it comes once for every
  key before the arc moves to its next token. With more keys than the limit, firing runs out
  where one key alone would bind: `[c, a:k, b:k, c(filter)]`.

An arc after the last join arc keeps the shortcut, since its dead ends fall inside one choice
of the join arcs and the first key meets them once. So does one before a join arc on a place
with no `filter`: the arcs there all choose among the same tokens, so none runs out while
another has an option. When the shortcut does not apply, the probe asks for every key, in
firing's order.

That residual check, or an earlier arc on the first join arc's place, can keep the first join
arc off its group's first token, so a key's first candidate may start later than the entry the
match sorts it by (§3). The merge treats that entry as a lower bound, so it opens such a key
early, never late.

### 5. Concurrent changes

The match is fed under each place's lock in turn and read under the engine lock. A place
changed directly in between, without the engine lock, can leave the match listing a key that a
place has just lost, or missing one it has just gained. The groups are still read live from the
places, which drops a lost key, so the effect is a stale read, as with any other read of a
place that changes during resolution. The change is reported at the next probe.

The keys' sort order is a lower bound computed from the mirrors (§3), so it is only as current
as they are. A token inserted ahead of a key's first entry by such a change, between the refresh
and the moment the merge opens that key, can give the key a candidate below its listed bound,
and the merge can then yield candidates out of product order. Under the engine lock the mirrors
are current and every bound holds; this is the same stale read, not a weaker guarantee.

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
- The firing path and guarded probes open keys in order of their bounds (§3), so they cost
  O(candidates examined · log keys), not O(keys). On a `manager == id` chain
  (`benchmarks/bench_self_join_chain.py`)
  every column holds flat from 1 000 to 16 000 keys:

  | µs/call at 1k / 4k / 16k keys | `main` | #77 (Hall per probe) | this ADR |
  | --- | --- | --- | --- |
  | guard-free probe | 8.0 / 8.2 / 8.2 | 10.8 / 10.7 / 10.7 | 10.1 / 10.3 / 10.2 |
  | guarded probe | 473 / 8 022 / 36 661 | 1 008 / 10 293 / 45 823 | 13.8 / 14.2 / 14.1 |
  | `FIRST` firing, no guard | 1 863 / 7 695 / 34 690 | 2 455 / 9 929 / 43 307 | 13.8 / 14.1 / 14.3 |
  | `FIRST` firing, guarded | 1 899 / 7 610 / 34 831 | 2 495 / 9 964 / 43 396 | 14.1 / 14.2 / 14.1 |
  | guard-free probe past N self-managed keys | wrong answer | 814 / 3 062 / 12 201 | 10.1 / 9.9 / 10.1 |

  `main` answers the last row in 8 µs because it binds a self-managed token to both arcs, the
  bug #75 fixed. The guard-free probe pays about 2 µs over `main` for the place reports and the
  distinct-token rule.
- Each match keeps up to two sorted lists of its keys by first candidate (§3), one entry per key
  that binds. A probe at a time earlier than the clock's last one drops the keys in between
  from the clock's list, and a later probe adds them back.
- A place read by join arcs with several functions registers its indexes with depth `D`, every
  join arc on it, so each watches its groups' first `D` entries and reports a key when any of
  them changes.
- Nets without `join_key`, and join transitions the match does not serve, take the same paths
  as before.
