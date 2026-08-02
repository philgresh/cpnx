# ADR 0007 — Best-effort side-effect linting & dynamic invariant fuzzing

- **Status:** Accepted
- **Date:** 2026-08-01
- **Supersedes / relates to:** [0002](0002-guard-type-checking-scope.md) (guard
  type checking), [0003](0003-inline-execution-and-string-removal.md) (inline
  execution & string removal), and the certification module (`cpnx.certification`).

## Context

`cpnx` intentionally admits **arbitrary Python callables** in transition guards,
arc `key`/`filter` selectors, and output-arc conditions, to maximise developer
expressiveness. High-level Petri nets as standardised in **ISO/IEC 15909-1:2019**
(*Systems and software engineering — High-level Petri nets — Part 1: Concepts,
definitions and graphical notation*) define transition enabling via data-dependent
inscriptions over token colours; that core is decidable for many properties
(boundedness, reachability, liveness). By allowing Turing-complete inscriptions,
`cpnx` deliberately forfeits that static decidability — the Halting Problem
guarantees no analyzer can, in general, decide whether such a guard terminates or
whether the net converges.

We do not chase an impossible silver-bullet decidability detector. Instead we
layer **three** confidence mechanisms of decreasing strictness, and add a dynamic
counterpart:

| Layer | Module | Contract | Severity |
|---|---|---|---|
| Whitelist inline-safety proof | `cpnx.certification` | Proves a callable is closed-world & terminating; decides inline vs. sandboxed execution | Never raises |
| Blocklist purity gate | `cpnx.sandbox.verify_callable_purity` | Rejects unambiguous escapes (`open`/`eval`/`exec`/imports/`global`/`sleep`/`system`/`popen`/`urlopen`) | **Raises** at construction |
| **Best-effort trouble-spot linter** | **`cpnx.linting`** (this ADR) | Flags network / database / clock / randomness use in guards/keys/filters | **Warns** (opt-in strict raise) |
| Dynamic invariant fuzzing | `tests/test_invariants_fuzz.py`, `tests/test_state_machine.py` | Empirically verifies convergence & safety on randomized markings | test-time |

## Decision

### 1. Add `cpnx.linting` as an *advisory* static AST linter

`lint_callable(func)` walks the callable's AST (reusing the source-recovery and
name-resolution helpers from `cpnx.certification`) and returns `LintFinding`s in
three categories:

- **network** — `requests`, `httpx`, `urllib`, `http`, `socket`, `smtplib`, …
- **database** — `sqlite3`, `psycopg2`/`psycopg`, `sqlalchemy`, `pymysql`,
  `asyncpg`, `pymongo`, `redis`, …
- **nondeterminism** — `random` (whole-surface), plus the *specific*
  non-deterministic members of the mixed-surface stdlib packages `time`
  (`time`/`monotonic`/`perf_counter`/…), `datetime` (`now`/`utcnow`/`today`),
  `uuid` (`uuid1`/`uuid4`), and `secrets` (`token_hex`/`randbelow`/…), and a small
  by-name table of distinctive clock/entropy attributes (`.now`, `.monotonic`,
  `.urandom`, …) for local-alias receivers.

Detection is by **resolving the called symbol to the package that defines it**
(so `import requests as r; r.get(...)` and `from requests import get; get(...)`
resolve identically). Whole-surface-effect packages (the network/database drivers,
`random`) flag outright; mixed-surface packages flag only their listed members, so
deterministic API — `datetime.timedelta`, `uuid.UUID("…")`, `time.strftime(…)` — is
**not** flagged (which matters especially under strict mode, where a false positive
would break construction of a valid net). Ambiguous names that collide with
token-payload/dict methods (`.get`, `.execute`, `.connect`, `.time`) are **excluded**
from the by-name table — they rely on package/member resolution, which does not
misfire on a plain dict or user object.

### 2. Severity: **warn by default**, strict opt-in

Findings are emitted as `CpnxLintWarning` (a dedicated category, independently
filterable) at construction time, alongside the existing hard purity gate. A
process-wide strict switch (`cpnx.set_strict(True)` or `CPNX_LINT_STRICT=1`)
promotes findings to `SideEffectLintError` for CI gating.

Why advisory and not a hard gate: sound static side-effect detection is
**impossible** in a language as dynamic as Python — `getattr`, dynamic dispatch,
monkeypatching, and late binding all route around any AST rule. This is precisely
the motivation for *dynamic* linting (Eghbali, Burk & Pradel, "DyLin: A Dynamic
Linter for Python," *Proc. ACM Softw. Eng.* 2, FSE 2025, Art. 92,
doi:10.1145/3729395). A clock read or occasional DB lookup in a guard is legal
Python and sometimes intentional; hard-rejecting it would break legitimate nets
and overstate what static analysis can prove. So we **flag the legible trouble
spots and warn**, and pair that with dynamic validation for everything static
analysis cannot see.

### 3. Dynamic invariant fuzzing as the empirical counterpart

`tests/test_invariants_fuzz.py` uses **Hypothesis** to fuzz hostile initial
markings/payloads of fixed workflow nets and assert: no orphaned tokens outside
sinks, formal quiescence within a step budget linear in token count, and that
deadlock occurs only in a by-design negative-control net. This is framed **not**
as a replacement for exhaustive state-space model checking (which hits the
combinatorial/Turing wall on data-dependent nets) but as rigorous randomized
fuzzing that scales to production-shaped workloads.

## Consequences

- **Positive:** users get early, actionable signal on the decidability-corrosive
  patterns (I/O, clocks, randomness in enabling logic) without a behavioural
  change; strict mode enables CI enforcement; the honest severity avoids false
  rejections and matches the documented limits of static analysis in Python.
- **Negative / limitations:** best-effort by construction — only the callable's
  **own body** is scanned (not transitively-called helpers), and fully dynamic
  dispatch is invisible. These gaps are exactly what the dynamic fuzzing layer is
  there to catch. Detection tables are a maintenance surface as the ecosystem's
  driver libraries evolve.
- **Neutral:** `cpnx.linting` shares its risk vocabulary conceptually with
  `cpnx.sandbox`'s blocklist but keeps a separate, softer contract; the two are
  deliberately not merged.
