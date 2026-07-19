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
