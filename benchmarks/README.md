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
| `bench_station_costs.py` | macro | µs/order against queue depth for each opt-in station's selection shape (certified vs. uncertified `key`, filter-only, timed×key), plus the arc-ordering and `binding_search_limit` search-budget sweeps. Answers the "Unmeasured combinations" audit below. |
| `bench_subnet.py` | macro | Per-firing `SubstitutionTransition` overhead (wrapped vs. inlined, swept over subnet size) and the wall-clock-friction leak — a subnet's cooldowns are paid in real time even under the logical driver. |
| `profile_cafe.py` | dev tool | `cProfile` a large cafe run and rank engine functions by cumulative / own time. Not a committed benchmark — a pointer to what to optimise. |

## The fixture: `cafe/`

The ☕ Concurrency Cafe topology lives in the `cafe/` package, split so that every place,
transition, guard, key, filter, and action is an individually documented symbol — each one
carrying both its *cafe role* and the *net feature* it exists to exercise. It renders as a
page on the docs site; start at `cafe/__init__.py` for the tour.

| Module | Holds |
| --- | --- |
| `cafe.places` / `cafe.transitions` | The core topology, one factory per entity |
| `cafe.inscriptions` | Guards and binding keys — pure, run under the engine lock |
| `cafe.actions` | Transition actions — side effects allowed, run off the lock |
| `cafe.stations` | Opt-in stations, one self-contained module each |
| `cafe.net` | `build_cafe`, which only chooses among the above |

`concurrency_cafe.py` remains as the runnable demo and a back-compatible
`from concurrency_cafe import build_cafe` shim.

### Opt-in stations

Every station is default-off and structure-preserving when off, so `build_cafe()` with no
flags is exactly the base topology and the numbers below stay comparable. Each exists
because there is an engine cost path the base net never touches:

| Flag | Station | Exercises |
| --- | --- | --- |
| `cold_brew` | 🧊 Cold-brew tower | A deep **timed** place |
| `cold_brew_key` | ↳ with a keyed arc | The timed×key residual ([#25]) |
| `batch_triage` | 📋 Rush-hour triage | A certified `InputArc.key` at depth |
| `decaf` | ☕ Decaf-only barista | `filter` **without** `key` — never indexed |
| `knock_box` | 🥁 Knock box | `consume_all` re-scanned behind a false guard |
| `specials_board` | 🧾 Specials board | An **uncertified** `key` (A/B vs `batch_triage`) |
| `eighty_six` | 🚫 86 board | Certified `key` + **uncertified** `filter` |
| `cupping` | 🥄 Cupping table | `count > 1` and the candidate space |
| `pastry_case` | 🥐 Pastry case | The only `SubstitutionTransition` |

Two knobs on `build_cafe` are not stations but are newly sweepable:
`binding_search_limit` (previously fixed at the engine default) and
`resource_arcs_first`, which reorders `T_Weigh_And_Grind`'s input arcs into the order
`BindingPolicy`'s documentation recommends.

**None of these are measured yet.** They are fixtures — the experiments they enable are
listed in "Unmeasured combinations" below.

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

### Deep-place drain: from O(N²) to linear

Sweeping an order of magnitude further — 20 000 orders, all deposited up front so a
place gets genuinely deep — exposed that draining a deep place was **O(N²)**: every
enabling check and every logical-clock advance scanned the whole marking. Three
independent O(depth) terms were involved, all now removed:

1. `Place.can_retrieve` counted *all* available tokens to answer "≥ count?" → early-exit.
2. `_arc_available` materialised the whole pool via `peek(len(place))` → bounded to the
   binding-search limit for single-token FIFO arcs.
3. The `_TokenStore` (a seq-keyed `OrderedDict` for ready tokens + a lazy min-heap for
   cooling ones) gives O(1) arbitrary removal and O(1) earliest-available, and the
   engine's clock advance (`_earliest_cooldown_boundary`) now reads that heap per place
   instead of scanning `place.tokens` on every tick.

Guard-free FIFO µs/step, same machine, before vs after (read the **shape** — flat is the
goal):

| orders | main (before) | after |
| ---: | ---: | ---: |
| 500 | 290.9 | 259.2 |
| 2 000 | 410.7 | 329.2 |
| 20 000 | 1 189.1 | **366.5** |

Per-10×-orders growth fell from **2.9× → 1.11×** (≈ flat: the drain is now linear), a
**3.2× speed-up at 20 000 orders**.

Two opt-in regimes (`build_cafe(cold_brew=True)` / `build_cafe(batch_triage=True)`, both
default off) exercise the shapes the base net never did, and show where the win lands:

| regime (20 000 orders) | before | after | note |
| --- | ---: | ---: | --- |
| cold-brew (deep **timed** place) | 1 048.8 µs/order | **228.2** | 4.6× — the cooling min-heap |
| batch-triage (deep **key**-sorted arc) | 3 677.5 µs/order | 2 440.7 | still ≈ O(N²) at the time; see below |

The batch-triage arc was the one retrieval shape that stayed non-linear: its per-firing
cost was a `filter`-then-`key`-sort over the whole eligible pool (`engine._order_available`),
re-run from scratch every firing. **That is now fixed.** Because the per-token
`InputArc.key` (see [ADR 0004](../docs/adr/0004-arc-selection-key-filter.md)) exposes a
value to index *on* — which the old opaque `list[Token] -> list[Token]` arc expression never
did — a **certified** key is now served from a persistent `(key, seq)` min-heap maintained
on the place across firings, so a firing reads only the leading tokens it can actually
consume instead of sorting the marking:

| orders | PR 1 (per-firing sort) | PR 2 (key-index) |
| ---: | ---: | ---: |
| 500 | 128.4 µs/order | **71.2** |
| 2 000 | 299.4 | **76.8** |
| 20 000 | 2 440.7 | **142.8** |

Per-order cost now grows **2.0× across a 40× increase in depth** (it was 19×), i.e. the
drain went from ≈ O(N²) to ≈ O(N log N) — a **17× speed-up at 20 000 orders**. Every
retrieval shape in this net is now at worst log-linear.

Two residuals remain, both deliberate and both falling back to the PR 1 cost rather than
misbehaving:

- **an uncertified `key`/`filter` is never indexed** — keying happens on the deposit path,
  which cannot host an unbounded callable — so it keeps the per-firing sort;
- **timed×key**: a keyed place that also holds cooling tokens cannot be served from the
  index (the index covers ready tokens only, and a cooling token never migrates into the
  ready set), so it keeps the per-firing sort too. No promotion pipeline was built, since
  no such place exists in the corpus.

Tracked in [#25](https://github.com/philgresh/cpnx/issues/25).

#### A/B against published v0.3.2

The table above compares PR 1 to PR 2. The comparison users actually feel is against the
last **published** release, so this one installs `cpnx==0.3.2` from PyPI and drains an
identical workload through each engine's own API — `InputArc(expression=lambda tokens:
sorted(tokens, key=k))` on 0.3.2, `InputArc(key=k)` today. Runs are interleaved
(A/B/C, 3 repeats, one sitting) so thermal drift hits every variant equally, and every run
is checked to consume tokens in the **same order** (identical SHA of the consumption
sequence) — otherwise the timings would not be comparable.

The middle row runs today's engine with the index switched off, which separates this work
from everything else since 0.3.2 (#24 callables-only, #26 the linearized store, #28 the API
split).

This was a **one-off migration-era measurement, not a committed harness** — there is no
script here to re-run. Pinning a comparison against one specific published version would
bit-rot the moment the next release lands, and the half of the experiment that mattered
(the isolated 0.3.2 environment, the interleaving, the order-hash check) is setup rather
than code. The method above is described in enough detail to rebuild it if a future change
warrants another cross-release comparison.

| µs/order (median of 3) | 500 | 2 000 | 20 000 |
| --- | ---: | ---: | ---: |
| v0.3.2 (published) | 76.2 | 218.7 | 2 273.1 |
| today, key-index **off** | 39.9 | 136.6 | 1 604.6 |
| today, key-index **on** | **14.3** | **20.6** | **103.9** |
| **speed-up vs 0.3.2** | **5.3×** | **10.6×** | **21.9×** |

Growth across a 40× increase in depth: **29.8× → 7.3×**.

This A/B is also what caught a regression the PR-1-vs-PR-2 comparison could not see. With
the index off, the split had briefly made a deep certified-key drain *slower than 0.3.2*
(3 053 vs 2 273 µs/order at 20 000): PR 1 replaced one C-level `sorted(tokens, key=k)` with
N interpreted per-token dispatches plus a decorate-sort. Since a certified callable is
called directly anyway, `_order_available` now hands it straight to `sorted(key=...)`,
restoring the C-driven loop — that is the "index off" row above, now 1.42× *faster* than
0.3.2 rather than 0.71× slower. It matters because it is the path the timed×key residual
takes.


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

## API-combination audit: what each selection shape costs

An audit of which API-element combinations affect performance, and which the corpus
covered, turned up the gaps below. Fixtures exist for every one (see "Opt-in stations"),
and `bench_station_costs.py` now measures the selection-shape and search-budget ones. The
routing decision that drives nearly all of it is `engine._materialize_pool`, which picks
between three costs per enabling check:

| Route | Condition | Cost |
| --- | --- | --- |
| 1 — bounded FIFO peek | `key is None and filter is None and not consume_all and count == 1` | O(limit) |
| 2 — key index | certified `key`, `filter` absent or certified, not `consume_all`, no cooling tokens | O(cap log cap) |
| 3 — full peek + per-firing sort | **everything else** | O(N) peek + O(N) dispatches + O(N log N) sort |

Route 3 runs **per `step()`, per transition** — including steps where the guard then
rejects — so on a deep place it is the O(N²) drain the key-index work removed.

### Results

µs/order draining one deep single-arc place, min-of-two, one machine
(Apple M4 Pro / CPython 3.14.3). Read the **growth** column (last÷first over an 8× depth
increase — ~1× is linear) and the **ratio to the certified-key control**, not the absolute
µs. Run-to-run spread was 2.5–5.4% on this pass, so treat anything under ~10% as unresolved.

| regime | arc shape | route | 250 | 2000 | growth | ×control@2000 |
| --- | --- | :-: | ---: | ---: | ---: | ---: |
| `batch_triage` | certified `key` | 2 | 75.6 | 82.0 | **1.1×** | 1× (control) |
| `decaf` | certified `filter`, no `key` | 3 | 94.1 | 178.7 | 1.9× | **2.1×** |
| `cold_brew` | no selection, timed | 1 | 149.9 | 409.2 | 2.7× | 4.4× |
| `cold_brew_key` | certified `key`, timed | 3 | 180.7 | 597.6 | 3.3× | 7.4× |
| `specials_board` | **uncertified** `key` | 3 | 1411.7 | 11039.4 | **7.8×** | **134.8×** |
| `eighty_six` | certified `key` + uncertified `filter` | 3 | 1439.3 | 11036.4 | 7.7× | 133.9× |

The headline: **an uncertified selection callable is the dominant resource-suck in the
API, by two orders of magnitude.** Certifying the `batch_triage` key keeps the drain flat
(1.1× over 8× depth); an *identical ordering* read off a mutable dict (`specials_board`)
grows 7.8× and lands **135× slower** at depth 2000 — and rising, since it is genuinely
O(N²) in N with a ~10 µs thread-hop constant per token. `eighty_six` confirms the
asymmetry it was built to show: one uncertified `filter` disqualifies the arc from indexing
*even though the key certifies*, landing right on top of the uncertified-key curve.

The three route-3 cases that stay cheap (`decaf`, both `cold_brew` arms) do so only because
their callables are certified/absent: route 3 costs an O(N) scan but no thread hop, so they
grow ~2–3× rather than ~8×. `cold_brew_key` (the timed×key residual, [#25]) is the most
expensive *certified* regime — the timed marking blocks the index, so a fully-certified key
still pays the per-firing sort — but at 7.4× it is a rounding error next to the uncertified
cliff.

### Search budget

Both search-budget hypotheses **confirmed**, on the pipeline (`T_Weigh_And_Grind`'s
three-arc product). "Deep-mobile rank" places one mobile-pickup ticket at the bottom of the
line and reports where `binding_priority_key` actually gets it ground — 0 means the
preference reached the bottom, ≈N means the budget ran out and it fell to FIFO.

- **Arc ordering ([#18]).** Listing the permit arcs *before* the deep data arc
  (`resource_arcs_first=True`) holds the deep-mobile rank at **0 through 800 orders**, while
  the cafe's historical data-first order lets it slip to 45 at 400 and 325 at 800 — the
  `limit / (scales × grinders)` cutoff the transition's own comment predicts. The one-line
  reorder dissolves the symptom, at a ~10% µs/order cost from the wider effective search.
  **This is the cheapest available fix for #18** and the reorder is semantics-preserving.
- **`binding_search_limit`.** A clean cost-vs-reach trade: at 800 orders, `limit=100` runs
  at 610 µs/order but strands the mobile ticket at rank 535; `limit=10000` reaches it
  (rank 0) at 2879 µs/order — 4.7× the cost for full-depth preference. The default 1000
  sits in between (rank 325), i.e. already past the cutoff for this depth.

### Still unmeasured (fixtures exist, numbers do not)

- **`consume_all`** (`knock_box`) — forces route 3, and pools are gathered *before* the
  guard, so a rarely-firing drain is re-scanned in full every step. Not on the
  `bench_station_costs.py` sweep because its cost depends on lull frequency, not depth.
- **`count > 1` at depth** (`cupping`) — the token pool stays bounded but `C(pool, count)`
  candidate combinations do not; the search-limit prefix can disable a transition while a
  valid binding sits deeper. Needs a candidate-space sweep, not a depth sweep.
- **Concurrency against uncertified selection.** A guard holds the lock per *candidate
  binding*; an uncertified `key`/`filter` holds it per *token*, unbounded — a far worse
  contention shape than any `bench_cafe_concurrency.py` regime currently exercises.
  `specials_board`/`eighty_six` are the regimes to run through it.

## `SubstitutionTransition` cost (`bench_subnet.py`)

A subnet fires like any pooled action, but its "action" is *drive a whole nested net to
quiescence*. `bench_subnet.py` measures what that abstraction costs, wrapping a chain of K
instant transitions in a subnet vs. inlining the identical K transitions (Apple M4 Pro /
CPython 3.14.3, min-of-three, 1000 tokens):

| subnet depth K | flat µs/tok | wrapped µs/tok | overhead µs/firing |
| ---: | ---: | ---: | ---: |
| 1 | 22.0 | 78.0 | **56.0** |
| 3 | 78.3 | 152.9 | 74.5 |
| 10 | 349.6 | 454.8 | 105.2 |
| 30 | 1817.0 | 2241.6 | 424.6 |

Two facts (production, wall-clock `run()`):

- **A fixed ~50-56 µs per firing** (read at K=1, where the shared per-step cost is smallest) —
  the deposit-into-ports, drive-the-subnet, retrieve machinery. That is the flat tax for
  wrapping, paid once per firing regardless of workload depth.
- **On top of that, a subnet re-runs its own drive/quiescence loop per internal step**, which is
  heavier than an inlined step — so the overhead *grows* with K (56→425 µs). A subnet does not
  make a large internal workflow cheaper; it multiplies its per-step cost. (The flat baseline
  is itself super-linear in K — per-step enablement scan grows with transition count — and both
  sides pay that; the *delta* is the subnet-specific part.)

**Driver-mode inheritance: a subnet's cooldowns are free to simulate.** A subnet inherits the
parent's clock *regime*. Under the logical driver (`drive_to_quiescence`) it is driven logically
too, so its internal cooldowns are jumped for free — exactly like the parent's; under the wall
driver (`run()`, production) they are waited out in real time. The same 0.05 s cooldown, 8 tokens:

| cooldown location | driver | wall time |
| --- | --- | ---: |
| in the parent | logical | 0.6 ms (jumped) |
| inside a subnet | logical | 1.0 ms (jumped — inherited) |
| inside a subnet | wall | 388 ms (waited — production) |

Logical driving is **~380× faster to simulate** the friction subnet, while the wall driver
still waits the real cooldown when you actually run the net. Tradeoff: for a *friction-free*
subnet the logical driver's clock machinery is ~20% heavier per firing than `run()`'s — a
simulation-only cost, negligible against the friction win, and production (the table above) is
unchanged.

**How this was reached — a fixed silent bug.** `drive_to_quiescence` used to *strand* tokens in
a subnet: driving a wrapped net on the logical clock left tokens stuck in the subnet's input port
(measured: 14 of 20), returned `is_quiescent() == True`, and reported a fast, wrong result. The
cause was clock *value* coupling — the parent pushed its logical time onto the subnet via
`advance_time`, but a subnet fires once per binding, so the second firing at the same instant
moved the subnet clock backward-or-equal, which `advance_time` rejects; the error rolled the
firing back and stranded the already-deposited copy. It had never surfaced because `pastry_case`
is not in the throughput sweep, so no logical-clock run had ever met a subnet. The fix isolates
the subnet's clock *value* (the parent's time never crosses the boundary) while inheriting its
*regime* (logical vs. wall) — `src/cpnx/engine.py`, regression tests in `tests/test_subnet.py`.

[#18]: https://github.com/philgresh/cpnx/issues/18
[#25]: https://github.com/philgresh/cpnx/issues/25

## Scope

These are intentionally lightweight sanity checks for spotting large
regressions or validating an optimization during development. They are **not** a
historical performance record — committed numbers go stale and aren't
comparable across machines. If we ever want regression tracking over time, that
belongs in CI with a dedicated tool (e.g. [asv](https://asv.readthedocs.io/) or
[CodSpeed](https://codspeed.io/)) that stores results per-commit per-runner,
rather than hand-maintained tables here.
