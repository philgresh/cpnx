# Benchmarks

Benchmarks for cpnx hot paths. Native stdlib only — no dependencies, no test
runner. Each script is standalone and importable from a checkout without
installing the package.

```bash
python benchmarks/bench_enablement.py
python benchmarks/bench_cafe_throughput.py
```

Two tiers:

- **Micro** — isolate a single hot function with `timeit` (`bench_enablement.py`).
- **Macro** — process a realistic workload end-to-end and measure throughput
  (`bench_cafe_throughput.py`, built on the ☕ Concurrency Cafe topology in
  `concurrency_cafe.py`). Use the macro tier to *find* where time goes and
  `profile_cafe.py` to *rank* the hotspots; use the micro tier to isolate and
  prove a fix.

## Benchmarks

| Script | Tier | Measures |
| --- | --- | --- |
| `bench_enablement.py` | micro | Cost of one `_is_transition_enabled` check (and `_resolve_binding` across binding policies) for a transition carrying a **string** guard and a **string** input-arc expression — the per-transition, per-`step()` hot path. |
| `bench_cafe_throughput.py` | macro | End-to-end throughput (`us/order`, `us/step`) of the cafe net processing N orders, swept over `N` and `max_workers`. Reveals how per-step engine cost scales with marking depth. |
| `profile_cafe.py` | dev tool | `cProfile` a large cafe run and rank engine functions by cumulative / own time. Not a committed benchmark — a pointer to what to optimise. |

Supporting module: `_driver.py` — the shared **logical-clock driver** (see below).

## Timing the engine, not `time.sleep`

The cafe has real friction — a grinder cooldown (`PacedResourcePlace`,
`pacing_secs`), settle windows, a two-token tray threshold. `PetriNet.run()`
waits those out on the **real** clock, so a naïve end-to-end timing would mostly
measure sleeping. Instead the macro benchmarks keep that friction but drive the
net on its **logical clock** (`_driver.drive_to_quiescence`): fire everything
enabled now, let in-flight actions settle, then jump `advance_time` straight to
the next availability boundary. Back-pressure is fully preserved (the grinder is
genuinely unavailable for 8 *logical* seconds) but the waiting is free, so the
measured wall time is engine CPU — the part we can optimise. Random channeling
failures are disabled in this mode (`channel_failure_rate=0.0`) for determinism.

## What the cafe throughput currently shows

The benchmark sweeps **two regimes**: `dose_tolerance_g=None` leaves
`T_Weigh_And_Grind` guard-free, and `dose_tolerance_g=1.0` puts a `[17, 19]`
dose guard in front of its `PRIORITY` candidate scan. Indicative numbers from
one machine (workers=1, so read the *shape* and the ratio, not the absolute µs):

| orders | steps (free / guarded) | µs/step guard-free | µs/step guarded | guarded ÷ free |
| ---: | ---: | ---: | ---: | ---: |
| 10 | 40 / 43 | 78.8 | 129.8 | 1.6× |
| 100 | 400 / 430 | 104.0 | 474.0 | 4.6× |
| 500 | 2000 / 2150 | 195.6 | 1791.4 | 9.2× |
| 2000 | 8000 / 8600 | 285.8 | 2526.1 | 8.8× |

Min of three sweeps on an otherwise-idle Apple M4 Pro / CPython 3.14.3 (min is
the right estimator here — measurement noise only ever *adds* time). Run-to-run
spread was ≤2% for every cell except guarded n=500 (5.9%) and guard-free n=10
(27%); the n=10 row is a 3 ms workload dominated by fixed setup cost, so treat
it as indicative only. Everything else is comfortably above the noise floor.

> **These figures supersede an earlier guard-free-only table.** The cafe had no
> `guard=` anywhere, so the old numbers measured the *cheapest possible* binding
> path and understated the cost for any guarded net. The guarded column is the
> honest one for guard-carrying workloads; nothing regressed.

- **Step count is exactly linear** — 4 steps per order guard-free, plus one
  `T_Rework_Dose` firing for the 30% of orders whose dose starts out of spec.
- **Per-step cost grows mildly and the growth decays** in *both* regimes:
  quadrupling 500 → 2000 raises guard-free µs/step ~1.5×. The `PRIORITY`
  candidate scan grows with ticket-line depth but is **capped by
  `binding_search_limit` (default 1000)**, so the workload is linear with a fat
  constant, not quadratic. The 2000-order row deliberately crosses that cap —
  and the guarded ratio flattening from 9.2× to 8.8× there is that cap working.
- **`max_workers` makes no difference** for these trivial actions — a single
  global engine lock plus per-firing thread-pool dispatch dominate.

### The guard cost is dispatch, not evaluation

ADR 0001 anticipated that guards would be the search's main expense ("guard is
evaluated once per candidate combination instead of once") and expected
AST-caching to mitigate it. Profiling says the mitigation targets the wrong
thing. In a 500-order guarded run, `_call_expr` (`engine.py:393`) accounts for
**8.26 s of 10.20 s** cumulative (81%) — but only 0.35 s of *own* time. It
submits every guard evaluation to a `ThreadPoolExecutor` and blocks on
`fut.result(timeout)`, so each of the ~334 K candidate checks pays a full
thread round-trip.

Measured in isolation on the same machine:

| | per call |
| --- | ---: |
| raw predicate | 0.090 µs |
| via `_call_expr` | 10.007 µs |
| **overhead** | **9.9 µs (112×)** |

So ~99% of guard cost is the timeout sandbox, not the user's predicate — and
because it is charged *per candidate binding* rather than per firing, it scales
with search depth. Caching the guard's AST cannot help; the thread hop happens
after any such cache. Both guard flavours route through it (string guards via
`_eval_expression`, callables directly), so neither avoids the cost. A fast path
that runs trivially-cheap guards inline when no timeout is configured looks like
a much larger lever than anything in the candidate-enumeration code — but it
trades away the I/O-in-a-guard protection `_call_expr` exists to provide, so it
is a design decision, not a mechanical optimisation. Not attempted here.

Hotspot ranking by own time, guarded (was: `_iter_candidate_bindings` first,
`_check_transition_guard` 8th at ~1.4% when guard-free):

1. `_call_expr` — 0.353 s own, **8.258 s cumulative**, 334 K calls
2. `_eval_expression` — 0.200 s own, 8.492 s cumulative
3. `_iter_satisfying_bindings` — 0.195 s own, 9.057 s cumulative
4. `_iter_candidate_bindings` — 0.193 s own, 0.204 s cumulative
5. `_reduce_min_key` — 0.141 s own, 9.640 s cumulative

## How to read the numbers

These report **microseconds per call** on the machine that ran them. The
absolute figure is hardware- and interpreter-specific, so don't treat it as a
fixed target. What's meaningful is the **relative** change between two revisions
on the *same* machine and Python version:

```bash
git checkout main          && python benchmarks/bench_enablement.py   # baseline
git checkout your-branch   && python benchmarks/bench_enablement.py   # candidate
```

Compare the two and report the ratio (e.g. "2× faster"), not the raw µs.

## Scope

These are intentionally lightweight sanity checks for spotting large
regressions or validating an optimization during development. They are **not** a
historical performance record — committed numbers go stale and aren't
comparable across machines. If we ever want regression tracking over time, that
belongs in CI with a dedicated tool (e.g. [asv](https://asv.readthedocs.io/) or
[CodSpeed](https://codspeed.io/)) that stores results per-commit per-runner,
rather than hand-maintained tables here.
