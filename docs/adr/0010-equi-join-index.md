# ADR 0010 — Opt-in equi-join between input arcs (`InputArc.join_key`)

- **Status:** Accepted
- **Date:** 2026-11-08
- **Relates to:** [0001](0001-combinatorial-binding-search.md) (the binding search and its
  candidate order), [0004](0004-arc-selection-key-filter.md) (per-token arc callables and their
  certification), [0006](0006-incremental-enablement.md) (volatile transitions);
  https://github.com/philgresh/cpnx/issues/64, follow-up to
  https://github.com/philgresh/cpnx/issues/59

## Context

A guarded transition finds its binding by walking the product of its input arcs' candidates
in lexicographic order (ADR 0001), testing the guard on each. #59 cause C made that walk lazy,
so a join whose match sits near the head costs what it inspects. A join with **no** match still
inspects every candidate up to `binding_search_limit`. The engine has to probe such a
transition on every clock advance, and when both sides of the join grow with the workload the
drive goes quadratic.

The common shape is an *equi-join*: "a job and the worker whose `wid` equals the job's". The
engine cannot see that equality, because the guard is an opaque function of the whole
candidate list. Recognising `a.payload[k] == b.payload[k]` inside a guard would mean
decompiling the guard, and it would silently change behaviour for nets that never asked for
it. An index needs a per-token value it can compute once, at deposit, the same reason ADR
0004 split `expression` into a per-token `key` and `filter`.

## Decision

### 1. `InputArc.join_key` declares the join

`InputArc` gains a keyword-only `join_key: Callable[[Token], object] | None`. Every input arc
of a transition that sets one belongs to the transition's single join group: a candidate
binding is considered only if all of those arcs' tokens have **equal** join keys (by `==` and
`hash`, as a dict does). The guard is still evaluated on each such candidate, so the equality
may be dropped from it.

Purity verification, linting, string rejection and the `_join_key_inline_safe` flag work
exactly as for `key` and `filter`. A transition with a `join_key` arc is volatile under
ADR 0006, like one with a `key` or `filter`.

A join arc must have `count == 1` and no `key`, `filter` or `consume_all`. Each of those
changes which tokens the arc offers, and the index models none of them yet. The combination
raises `ValueError` at construction and on any reassignment that would create it, and a failed
reassignment leaves the arc unchanged. Conditions on one side belong in the guard.

### 2. The candidates and their order

The join search's candidates are exactly the product search's candidates whose join keys
agree, **in the product's order**. So for a net that adds `join_key` to a guard that already
tests the same equality:

- `FIRST` picks the same binding;
- `RANDOM` sees the same satisfying bindings in the same order, so the seeded stream is
  unchanged;
- `PRIORITY` scans the same set and picks the same minimum.

`tests/test_join_key.py` checks this for all three policies, on timed and untimed places.
The one deliberate difference is the search limit. `binding_search_limit` counts examined
candidates, and the join search examines only agreeing ones, so a join can find a binding that
the guard-only search would have given up on. Join arcs are also not truncated to
`binding_search_limit + 1` options as other arcs are, since their groups hold only matching
tokens. Arcs without a `join_key` on the same transition keep their usual options and cap.

A transition with a `join_key` arc is never head-only, even with no guard. The heads of its
places need not share a key.

### 3. A persistent join index per place

For a **certified** `join_key` on a plain [`Place`][cpnx.Place], the place keeps a
`places._JoinIndex`: a dict from key to that key's tokens, each group sorted by
`(tier, available_at, seq)`. Tier 0 is a ready token and tier 1 a timed one, so this is
exactly the store's read order. Availability at `t` is "ready, or `available_at <= t`", so
the available tokens of a group are always a **prefix** of it, found with one bisection. Unlike
the key index of ADR 0004, the join index covers timed tokens, which a discrete-event net is
full of. It is maintained on every append and removal, with eager deletion through a
`seq -> key` map, so the cost is O(group size) per change and O(1) per looked-up key. The
engine registers the index lazily and re-registers it if `join_key` is reassigned.

A resolution reads the join arcs' places smallest first. The first returns its groups for
every available key; each later one is asked only for the keys every earlier one had. The
groups come back as lazy views (`places._JoinGroup`), so a deep group costs only what is read
from it. Each surviving key contributes the product of its groups with the other arcs'
options, read through `_lazy_product`, and `heapq.merge` interleaves the per-key products
back into product order by each candidate's per-arc read positions. Keys are disjoint, so no
two candidates tie.

The existence probe only asks whether *some* candidate satisfies. When all candidates fit
within `binding_search_limit` it chains the per-key products and skips the merge, because the
order cannot change the answer. With more candidates than that, the limit decides which are
examined, so the probe keeps the firing path's order and the two always agree.

### 4. Falling back

The index is an optimisation, never the authority, as in ADR 0004. When it cannot serve,
the engine reads the arc's whole available pool and groups it itself, with integer read
positions in place of the index's tuples. The candidates and their order are the same. That
happens when:

- `join_key` is not certified. Keying on the deposit path cannot wait on the timeout-bounded
  executor, so an uncertified key is never indexed. It is dispatched per token per resolution;
- the place is a subclass (`ResourcePlace`, `PacedResourcePlace`, `ThresholdPlace`, a sink),
  whose availability rules differ from the store's;
- the index disabled itself.

A `join_key` that raises or returns an unhashable value disables that place's index (a
deposit never raises because of it). The fallback then meets the same failure, makes the arc
unsatisfiable, and reports it once through `on_error`, as a raising `key` or `filter` does
(issue #29). A disabled index re-enables once its place is empty, since it then covers
everything again.

## Consequences

- A join with no satisfying binding costs O(keys in the smallest join place) lookups instead
  of a walk over the product. In `benchmarks/bench_guard_join.py` a miss over 8 workers takes
  about 7 µs at every depth from 1 000 to 16 000 jobs, where the guard takes 7 to 113 ms.
- A join that does match costs a few times more than the lazy guard search when the match is
  the very first candidate (about 37 µs against 11 µs per resolution in the same benchmark),
  because it merges one product per key. The drive there is within 10 to 20% of the guard.
- The cost is still proportional to the number of distinct keys available in the smallest
  join place. In #59's `shop` net the number of workers grows with the number of jobs, so its
  µs/step still grows by about 1.2 to 1.4× per doubling (588 µs/step at 16 000 jobs, against
  1 622 with the guard). Keeping the intersection of keys up to date incrementally, across
  places and a moving clock, would remove that and is left for later.
- Nets that do not set `join_key` take exactly the paths they took before. The only change on
  those paths is one `any(...)` over a transition's input arcs in `_is_head_only` and the
  resolution entry points.
- One join group per transition. Two independent equalities (say `wid` between arcs 0 and 1
  and `site` between arcs 1 and 2) still need one of them in the guard. A composite key
  (`(wid, site)`) covers the case where every join arc shares both.
