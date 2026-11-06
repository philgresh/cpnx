# Performance

`cpnx` runs a net by firing transitions to quiescence, so the cost of the engine's
per-step scheduler — the work it does to decide *what fires next* — is the ceiling on
throughput. A net can grow along two independent axes, and naïvely each one makes that
per-step decision more expensive:

- **Breadth** — the number of *transitions* in the net. A wide fan-out net (many
  independent workers, a validator set, a large routing mesh) has a high transition count
  `T`.
- **Depth** — the number of *tokens* sitting in a single place. A backlog place, a
  barrier that accumulates votes, or a queue that drains in one sweep can hold a large
  marking `N`.

The engine is built so that neither axis makes the *per-step* decision scale with the size
of the net. Two structural results — each with its own decision record and benchmark — keep
the scheduler flat where a straightforward implementation would go quadratic.

!!! note "Reading the numbers below"

    Absolute microsecond figures are hardware- and interpreter-specific, so the benchmarks
    (and this page) report the **shape** — how cost grows as the net grows — not raw speed.
    "Flat" means the per-step cost stays roughly constant as the relevant axis doubles
    (growth factor ~1.0); "linear" means it doubles with the axis. Reproduce them yourself
    with the commands at the bottom.

## Breadth: the per-step scan is `O(K)`, not `O(T)`

Selecting the next transition to fire used to re-derive enablement from scratch every step:
the engine looped over **every** registered transition, resolved a fresh binding for each,
and then fired exactly one. A single step was `O(T)` in the transition count; a run that
fires `K` times paid `O(K · T)`. Because that scan runs under the engine's single global
lock, its cost is not just CPU — it is lock-hold time, and on a wide net every worker thread
waiting for its turn to fire paid for it. That is precisely what caps concurrency on a
wide fan-out topology: the scan, not the actual work.

The engine now schedules **incrementally**. A static reverse-routing table maps each place
to the transitions that read it, and a per-place *dirty set* tracks which places actually
changed since the last step. A firing only touches the places on its own arcs, so it can
only change the enablement of the transitions reading *those* places — and the scheduler
re-evaluates only that handful (`K`), never the whole net (`T`). Selecting the winner among
the ready transitions is `O(1)`.

The effect is a flat per-step cost as the net widens. Sweeping the transition count `T` from
50 to 1600 (a 32× range):

| Metric | Before | After |
| --- | --- | --- |
| `_select_transition_to_fire` probe (1 of `T` enabled) | linear in `T` | **flat** (~1.0× per doubling) |
| per-step drive cost (`K = T` firings) | linear in `T` → `O(T²)` total | **flat** → `O(T)` total |

The same result lifts `is_quiescent()` and `is_dead()` to `O(1)` for these nets, so a run
loop that polls quiescence frequently no longer pays a per-transition tax on every poll.

!!! info "When the fast path engages"

    Incremental scheduling turns on automatically for the common case: a net with **no
    clock-driven timing** (`PacedResourcePlace`, `settle_secs` arcs) and **no**
    `BindingPolicy.RANDOM`/`PRIORITY` transition. Nets using those features fall back to the
    original full scan, which is **unchanged and byte-identical** to before — you never
    trade correctness or determinism for the speedup; you simply get it or you don't. The
    fallback exists because timed re-enablement and RNG-drawing binding policies have
    subtle re-enablement and seeded-stream semantics that the fast path deliberately does
    not try to prove safe. See
    [ADR 0006](https://github.com/philgresh/cpnx/blob/main/docs/adr/0006-incremental-enablement.md)
    for the full boundary and the four re-enablement hazards it handles.

This is the win that matters for concurrency: on the BFT-style consensus workload (one
transaction fanning out to 100 validators behind an 80-vote barrier), collapsing the
per-step lock-hold time is what lets added workers actually translate into throughput
rather than queueing behind the scan.

## Depth: `consume_all` drains are `O(N)`, not `O(N²)`

The complementary axis is depth. A `consume_all` transition sweeps an entire place in one
firing — ideal for a barrier that clears every accumulated vote, or a queue that flushes in
a batch. The hazard is the *check*: deciding whether such a transition is enabled used to
copy and inspect the full pool on every probe, so a place that sat enabled-but-undrained
across many steps paid an `O(N)` peek each step, compounding to `O(N²)` to accumulate and
then drain a place of depth `N`.

The enablement check for a guard-free `consume_all` transition is now **count-only**: it
asks whether the pool is non-empty, an `O(1)` test, instead of materialising the pool. The
per-probe cost holds constant as depth grows, turning the accumulate-then-drain pattern from
`O(N²)` back into `O(N)`. See
[ADR 0005](https://github.com/philgresh/cpnx/blob/main/docs/adr/0005-consume-all-count-only-fast-path.md).

## Depth: timed backlogs drain like untimed ones

A net used as a discrete-event simulation builds a different kind of deep place: timed work
that has *matured* (its `available_at` has passed) but waits for a scarce resource. Two costs
used to scale with that backlog. Every clock advance asked each place for its next future
boundary, and the store walked past every matured token to answer, so each advance cost
`O(backlog)`. And every firing of a guard-free FIFO transition peeked
`binding_search_limit + 1` tokens to use one, which on a timed place is a heap pop and push
per token.

Matured tokens now move once into their own heap, so the boundary query skips them
([#60](https://github.com/philgresh/cpnx/pull/60)), and a head-only transition reads only the
tokens it will consume ([#61](https://github.com/philgresh/cpnx/pull/61)). A timed backlog
now costs about what the same work costs untimed. In the cafe's mobile-order rush at 8 000
orders the timed rail runs 1.0× its untimed control (5.1–5.3× with only the first fix), and
in `bench_timed_backlog.py` the timed drive at 4 000 arrivals runs 1.02–1.14× its control
(7.1–7.25×). Both causes are tracked in
[#59](https://github.com/philgresh/cpnx/issues/59).

## What this means for you

You do not have to opt into either optimization or structure your net around them — they are
properties of the engine. Practically:

- **Wide nets stay cheap per step.** Adding independent transitions does not slow down the
  steps that don't touch them.
- **Deep places drain cheaply.** A barrier or backlog that grows before it clears does not
  pay a quadratic tax to do so.
- **Concurrency is the lever it should be.** With the per-step scan off the critical
  section, `max_workers` governs throughput on wide nets instead of the scheduler.
- **You never lose correctness for speed.** Timed and `RANDOM`/`PRIORITY` nets transparently
  use the original, fully-tested scheduler.

## Reproduce it

The benchmarks are native stdlib — no dependencies, no runner — and importable from a
checkout without installing the package:

```bash
# Breadth: per-step cost as the transition count T grows (the O(T)→O(K) result).
python benchmarks/bench_transition_scan.py
```

```bash
# Depth: per-probe consume_all enablement cost as place depth N grows (O(N²)→O(N)).
python benchmarks/bench_consume_all_drain.py
```

```bash
# Depth: timed backlog — boundary query cost and a timed drive vs its untimed control.
python benchmarks/bench_timed_backlog.py
```

```bash
# Realistic wide fan-out: BFT (80% UNL) consensus throughput across a worker sweep.
python benchmarks/consensus/benchmark_consensus.py
```

For the full catalogue of benchmarks — including the ☕
[Concurrency Cafe](cafe.md) macro fixture and its per-station cost probes — see
[`benchmarks/README.md`](https://github.com/philgresh/cpnx/blob/main/benchmarks/README.md)
in the repository. The design rationale for each result lives in the
[architecture decision records](https://github.com/philgresh/cpnx/tree/main/docs/adr).
