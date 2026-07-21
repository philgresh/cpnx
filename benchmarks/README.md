# Benchmarks

Benchmarks for cpnx hot paths. Native stdlib only — no dependencies, no test
runner. Each script is standalone and importable from a checkout without
installing the package.

```bash
python benchmarks/bench_enablement.py
python benchmarks/bench_cafe_throughput.py
python benchmarks/bench_cafe_concurrency.py
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
| `bench_enablement.py` | micro | Cost of one `_is_transition_enabled` check (and `_resolve_binding` across binding policies), for a **string** guard and a **callable** guard computing the identical predicate — the per-transition, per-`step()` hot path, and the dispatch gap between the two flavours. |
| `bench_cafe_throughput.py` | macro | End-to-end throughput (`us/order`, `us/step`) of the cafe net processing N orders, swept over `N` and the binding regime. Reveals how per-step engine cost scales with marking depth. Single-worker by construction. |
| `bench_cafe_concurrency.py` | macro | Wall-clock **makespan** against `max_workers`, swept over queue depth and guard regime. The only script here that says anything about parallelism. |
| `profile_cafe.py` | dev tool | `cProfile` a large cafe run and rank engine functions by cumulative / own time. Not a committed benchmark — a pointer to what to optimise. |

Supporting module: `_driver.py` — two drivers, `drive_to_quiescence` (logical
clock, deliberately serialized, measures engine CPU) and `drive_saturating`
(wall clock, deliberately concurrent, measures makespan). They answer different
questions and are not interchangeable; see the module docstring.

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
| 10 | 40 / 43 | 101.8 | 158.4 | 1.6× |
| 100 | 400 / 430 | 133.5 | 878.0 | 6.6× |
| 500 | 2000 / 2150 | 270.9 | 3012.4 | 11.1× |
| 2000 | 8000 / 8600 | 383.6 | 3999.4 | 10.4× |

Min of three sweeps, one machine, one sitting (Apple M4 Pro / CPython 3.14.3);
min is the right estimator because measurement noise only ever *adds* time.
Run-to-run spread was 5–8% on this pass, higher than an earlier quieter run
managed, so treat differences under ~10% as unresolved. The n=10 row is a ~4 ms
workload dominated by fixed setup cost — indicative only.

**Step counts are now identical across every repeat**, which was not true of any
previously published table here. Two instrument defects had to be fixed first:

- The net was never seeded. Every cafe transition shares the default `priority`,
  so each `step()` breaks the tie with `_rng.choice`, and `build_cafe` left
  `PetriNet` on OS entropy — step counts wandered ~2% run to run (919/936/917/
  928/938 over five 200-order runs). Since µs/step divides by that count, every
  earlier figure compared runs that had done different amounts of work, and any
  effect under ~2% was unresolvable in principle.
- The logical driver stranded on settle-window boundaries. Adding `settle_secs`
  to the tray arc exercised a branch of `_driver.py` that no cafe arc had ever
  triggered, and it turned out `last_deposit + settle_secs` can round to exactly
  `now` in float64 at `time.monotonic()` magnitudes — the driver then saw
  nothing to wait for and stopped while the window was still unmet. Observed at
  100 orders guard-free: 6 tokens stranded on the tray, `is_quiescent()` False,
  97 of 100 orders served, and the benchmark printed its table anyway.

The clearest sign both are fixed: step counts are now exactly 4 per order
guard-free and 4.3 guarded, the linearity this document has always claimed while
the measured numbers quietly drifted above it.

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
- **`max_workers` is not swept here, and the old flat result meant nothing.**
  An earlier revision swept it against *this* driver, found no difference, and
  reported that as evidence that dispatch overhead dominates parallelism. That
  conclusion was unfounded: `drive_to_quiescence` awaits in-flight completion
  after every `step()` — deliberately, since it measures engine CPU and the
  seeded channeling regime reproduces only because of it — so at most one action
  is ever in flight and the pool size *cannot* matter. The benchmark never gave
  the pool anything to parallelise. Concurrency has its own driver and its own
  script now; see `bench_cafe_concurrency.py` and the section below.

### Third regime: channeling (retries and dead-letters)

Since [#20](https://github.com/philgresh/cpnx/pull/20) made `retry_delay`
model-clock-aware, the retry path is measurable on the logical clock — before
it, a rolled-back token got a wall-clock deadline it could never reach against a
logical clock near zero, so the run stranded. `channel_seed` makes it
reproducible. Within one run (workers=1, guarded, 15% channel rate):

| orders | steps | served | trashed | µs/step vs. guarded |
| ---: | ---: | ---: | ---: | ---: |
| 100 | 442 | 99 | 2 | 0.97× |
| 500 | 2214 | 493 | 13 | 1.01× |
| 2000 | 8874 | 1974 | 51 | 0.99× |

Same sweep as the table above — all three regimes now come from one run on one
machine, rather than being stitched together from two.

Dead-letter rate lands at 51/2000 = 2.6% against a 15% channel rate, close to
the expected 0.15² = 2.25% — `max_retries=1` means a shot must channel *twice*
to be binned.

Note the retry path makes µs/**step** slightly *cheaper* while making the run
strictly more expensive overall. Retries fire against the shallow
`P_Ground_Coffee` queue, not the deep ticket line, so they add cheap steps that
dilute the average. This is the clearest argument for reading us/step only
within a fixed regime, and never as a cross-regime efficiency score.

### Does `max_workers` help? Only until the guard search gets deep

`bench_cafe_concurrency.py` answers what the logical driver structurally cannot.
It drives the net on the **wall** clock without ever awaiting, so firings stack
up to `max_workers`, and it gives each station real work (`work_secs`) because
instant pure-Python actions are GIL-bound — a flat curve there would measure
CPython, not cpnx. Speedups are against that regime's own `workers=1`:

| orders | regime | w=2 | w=4 | w=8 |
| ---: | --- | ---: | ---: | ---: |
| 60 | guard-free | 2.00× | 3.93× | 6.93× |
| 300 | guard-free | 2.01× | 4.03× | 7.00× |
| 60 | guarded | 1.99× | 3.86× | 6.04× |
| 300 | guarded | 1.80× | 1.63× | 1.72× |

**cpnx parallelises well — until a guard is in the way of a deep candidate set.**
The guard-free rows are flat across depths, which is the control that says the
collapse is real and not a loaded machine. The guarded row at 300 orders is not
merely sublinear: it is *non-monotonic*. Going from 2 workers to 4 makes the run
slower. That is lock contention, not the GIL.

The mechanism is `step()`: it runs `_select_transition_to_fire()` — which
evaluates a guard per candidate binding — entirely inside `with self._lock`, and
commit and deposit take the same lock. Guard cost therefore scales with queue
depth while the interval each worker needs serving in (`work_secs / workers`)
does not, so there is a crossover, and past it extra workers only add contention.

Note how easily this is missed: at 60 orders the guarded arm scales 6.04× against
6.93×, which reads as "guards cost a little". Sweep the depth before concluding.

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

So ~99% of the *isolated* call is the timeout sandbox rather than the user's
predicate. **Do not read 112× as an end-to-end figure** — it is the ratio around
a bare predicate. Measured through the real engine path by `bench_enablement.py`,
fixed per-call work (arc-pool gathering, availability and settle checks) dilutes
it considerably, and the result depends on the binding policy:

| path | callable ÷ string |
| --- | ---: |
| `_is_transition_enabled`, LEGACY / FIRST | 2.0–2.6× |
| `_resolve_binding`, LEGACY / FIRST | 2.0–2.6× |
| `_resolve_binding`, RANDOM / PRIORITY | **15–16×** |

Both numbers are real; they measure different things. The useful conclusion is
that the cost concentrates in the policies that enumerate the *whole* candidate
set instead of short-circuiting at the first satisfying binding — which is where
the cafe's `PRIORITY` transition sits. The RANDOM gap reconciles arithmetically:
200 tokens × ~11 µs round trip accounts for essentially all of it.

**The two flavours do not cost the same, contrary to what this file used to say.**
`_eval_expression` dispatches a certified closed-world callable (per
`cpnx.certification.is_inline_safe`) *inline* — no executor, no timeout — and
sends only an uncertified callable through `_call_expr`. Certified guards never
pay the thread hop.

Caching the guard's AST cannot help either flavour: for the uncertified path the
thread hop happens after any such cache, and a certified callable is already
running inline with no hop to amortise. The fast path that runs certified
callables inline is a much larger lever than anything in the
candidate-enumeration code — and, unlike when this file last described it as a
future option, it is now implemented: it trades away the I/O-in-a-guard time
bound `_call_expr` exists to provide, but only for the callables the certifier
can prove closed-world; anything it cannot prove still pays the full
timeout-bounded round trip.

Hotspot ranking by own time, 500 orders guarded (was: `_iter_candidate_bindings`
first, `_check_transition_guard` 8th at ~1.4% when guard-free):

1. `_call_expr` — 0.621 s own, **14.290 s cumulative**, 571 K calls
2. `_eval_expression` — 0.353 s own, 14.700 s cumulative, 575 K calls
3. `_iter_satisfying_bindings` — 0.349 s own, 15.710 s cumulative
4. `_iter_candidate_bindings` — 0.347 s own, 0.365 s cumulative
5. `_check_transition_guard` — 0.167 s own, 14.866 s cumulative

`_call_expr` is 84% of the 17.06 s run by cumulative time but 3.6% by own time:
the work is the thread round-trip, not the predicate. It is charged **per
candidate binding** — 571 K calls against 2 900 `step()`s, i.e. ~197 guard
evaluations per step — so it scales with search depth, and every one of those
round-trips holds the global engine lock.

An earlier ranking here was profiled against a constant `weight_g: 18`, which the
dose guard accepts unconditionally, so `T_Rework_Dose` never fired and the guard
ran at acceptance probability 1. `profile_cafe.py` now deposits the same ~30%
out-of-spec mix as the throughput benchmark.

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

## Correction: the `count==1` fast path is ~1%, not 3.3%

Commit `03b43fb` ("perf: fast path for count==1 arcs") is documented as **~3.3%
faster** with "baseline noise ~0.4%". Re-measured here, min-of-five, on a seeded
net and a driver that no longer strands:

| regime | with fast path | without | delta |
| --- | ---: | ---: | ---: |
| guard-free, 2000 orders | 3167.3 ms | 3202.3 ms | **1.1%** |
| guarded, 500 orders | 8539.4 ms | 8546.6 ms | **0.08%** |

Neither the original headline nor its noise estimate holds. The 3.3% was measured
guard-free (the cheapest possible binding path, before guards existed in the
fixture) **and** on an unseeded net whose step counts were themselves varying
~2% — so the claimed effect sat inside an unquantified band larger than itself.

The change is behaviour-preserving: `tests/test_seeded_determinism.py` passes
with the fast path both present and reverted, which is a much stronger check than
the ad-hoc fingerprint used at the time. It is kept because it is nine lines and
directionally faster on the guard-free path. But treat ~1% guard-free / ~0%
guarded as the honest figure, not 3.3%.

## Scope

These are intentionally lightweight sanity checks for spotting large
regressions or validating an optimization during development. They are **not** a
historical performance record — committed numbers go stale and aren't
comparable across machines. If we ever want regression tracking over time, that
belongs in CI with a dedicated tool (e.g. [asv](https://asv.readthedocs.io/) or
[CodSpeed](https://codspeed.io/)) that stores results per-commit per-runner,
rather than hand-maintained tables here.
