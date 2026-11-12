# ADR 0013 — Let outside logic choose a transition's binding

- **Status:** Proposed
- **Date:** 2026-11-10
- **Relates to:** [0001](0001-combinatorial-binding-search.md) (the bounded product search and
  `BindingPolicy`), [0003](0003-inline-execution-and-string-removal.md) (callables that run under
  the engine lock), [0006](0006-incremental-enablement.md) (volatile transitions),
  [0010](0010-equi-join-index.md) (the place join index reused here);
  https://github.com/philgresh/cpnx/issues/80

## Context

Nets used as discrete-event simulators often model a dispatcher: a transition that pairs one
waiting job with one free server. Which pair fires is a policy decision, and the policy is often
what the simulation studies. `FIRST`, `RANDOM` and `PRIORITY` choose among guard-satisfying
bindings of a bounded product search. #80 showed three ways that fails a dispatcher whose
eligibility is membership (`job.skill in server.skills`):

1. No index applies, so each probe walks jobs × servers until the guard passes.
2. `RANDOM` is uniform only over the first `binding_search_limit` candidates in product order.
   With 2 000 jobs and 100 servers, the first pick over 200 seeds was always among jobs 0–9.
3. `PRIORITY` needs one total key per binding, and a policy that looks at state outside the
   binding does not fit it. An action cannot choose either: its tokens are already consumed.

## Decision

`Transition` gains `binding_selector: Callable[[BindingView], choice | None]`. When it is set,
`_resolve_binding` and `_binding_exists` ask the selector instead of searching.

- **Input.** A `BindingView`: the transition's arcs, the model time, `available(i)` (the tokens
  arc `i` could take, read lazily in place order), `has_available(i, n)`, `groups(i, key)` and
  `group(i, key, value)` (arc `i`'s tokens grouped by `key(token)`, as sized, indexable
  `TokenGroup` views), and `random`. The view reads the places under the lock and copies
  nothing.
- **Output.** `None` (nothing fires), or one choice per input arc, as a sequence in `inputs`
  order or a mapping from arc index. A choice is a `Token` for a `count=1` arc or a sequence of
  exactly `count` tokens.
- **Validation.** The engine checks the shape, that no token is chosen twice, that every token
  is stored and available in its arc's place (`Place.holds_available`, O(tokens chosen)), and
  that the guard holds. It then consumes by identity, as every binding is. A failed check or a
  raising selector is buffered and reported through `on_error` (off the lock, edge-triggered per
  transition: the same failure reports once, then again when it changes or after the transition
  fires), and the transition reads as not enabled.
- **Enablement.** The selector is the probe: `_binding_exists` asks it too. A transition with a
  selector is volatile (ADR 0006), so every reconcile asks it again. Before asking, the engine
  checks each arc's count and settle window, so an arc that cannot be met never reaches the
  selector.
- **Determinism.** `view.random` is a `random.Random` seeded with a string built from the net's
  `seed`, the transition's name and how many of its selector bindings have been consumed. So
  the stream is a function of the firing, not of how many times the engine asks between
  firings, it does not depend on `PYTHONHASHSEED`, and it never touches the net's scheduling or
  retry streams. An unseeded net draws a base from `SystemRandom` at construction. The selector
  must be a pure function of the view, as a guard must be (ADR 0003); `verify_callable_purity`
  and the side-effect linter check it at assignment.
- **Indexed lookups.** `groups(i, key)` with a certified `key` registers a place join index
  (ADR 0010) for that key on first use and reads it at O(keys) afterwards. At most four key
  functions per arc keep an index; a fifth evicts the oldest, so a selector that builds a new
  lambda on every call pays a rebuild rather than leaking indexes. The certification of each key
  is cached weakly, so those lambdas are not kept alive either. A transition's indexes are
  released when its arc moves to another place or its selector is cleared. An uncertified key,
  or a place type other than `Place`, is keyed on each call.
- **Scheduler.** A selector transition ignores its binding policy, so a `RANDOM` or `PRIORITY`
  policy on it (or as the net default) does not turn off the incremental scheduler.

### Answers to #80's open questions

- **Lock.** The selector runs under the engine lock, like guards and `binding_priority_key`.
  A snapshot would cost a copy of every input place per probe, which is what the view exists to
  avoid. The contract: cheap, pure, and never calling back into the net. The lock is not
  re-entrant, so for the length of the call the engine swaps in a guard that raises
  `RuntimeError` on the selector's own thread (reported like any raising selector) instead of
  deadlocking. A failure is reported once, and again only when it changes or after the
  transition fires, because a selector transition is re-asked on every enabling check.
- **`join_key`, `key`, `filter`, `consume_all`, `binding_priority_key`.** Mutually exclusive
  with a selector, rejected with `ValueError` at assignment and reported if an arc is changed
  later. `groups` covers what a selector would want from `join_key`, and the selector can apply
  any filter itself. Combinations can be added later without breaking anyone.
- **Net-level default.** Not added. A selector is written against one transition's arcs.
- **`RANDOM` truncation.** Documented on `BindingPolicy`, pointing to selectors for "random
  among all eligible". Fixing it inside `RANDOM` is a separate change.

## Consequences

- `benchmarks/bench_dispatch_selector.py` runs #80's dispatcher. With a selector that picks
  uniformly among all eligible pairs from the jobs grouped by skill, cost per firing changes ×0.96
  then ×1.01 as the backlog doubles from 1 000 to 4 000 jobs, and the first pick over 200 seeds
  spans indices up to 1 978 (191 distinct) where `RANDOM`'s stay within 0–9. Over the same
  range `RANDOM`'s cost per firing roughly doubles, ending about 2.2× above the selector's on a
  shared Linux container. The selector is still costlier than `FIRST`, because it scores all
  100 servers and is asked about five times per firing (each reconcile, plus the
  timing-ignoring quiescence probe).
- Nets without a selector take the same paths: one `is not None` check per resolution.
- Follow-up: cache a selector's answer per marking change of its input places, which would cut
  the repeated asks; and let `available(i)` honour an arc's `key` ordering.
