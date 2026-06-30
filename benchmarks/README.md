# Benchmarks

Micro-benchmarks for cpnx hot paths. Native stdlib only — no dependencies, no
test runner. Each script is standalone and importable from a checkout without
installing the package.

```bash
python benchmarks/bench_enablement.py
```

## Benchmarks

| Script | Measures |
| --- | --- |
| `bench_enablement.py` | Cost of one `_is_transition_enabled` check for a transition carrying a **string** guard and a **string** input-arc expression — the per-transition, per-`step()` hot path. |

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
