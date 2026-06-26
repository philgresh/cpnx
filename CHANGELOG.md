# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

---

## [0.2.0] — 2026-06-26

### Added

- **`SinkPlace`** — A new terminal place type that counts and observes tokens without retaining them, resolving potential memory leak issues in long-lived, high-throughput pipelines. Supports an optional ring buffer to retain the most recent N tokens for inspection/debugging.
- **`on_token_dead_lettered`** — A new lifecycle callback invoked when a data token is dead-lettered to the error place after exhausting retries or failing immediately.
- **Pull Request Template** — A standardized GitHub PR template including general and project-specific guidelines (concurrency, memory-safety, styling).

### Changed

- **Error Handling & Dead-lettering Redesign** — Restored and cleaned up the dead-lettering behavior. When a transition's action fails, the data token is dead-lettered to the `error_place` once `max_retries` are exhausted (or immediately if `max_retries=0`). Surplus resource tokens are returned to their source places on success. Added try-except wrapper to executor submit calls to prevent token leaks.
- **Window-First statistics in `SinkPlace`** — Calling `drain_stats()` on a `SinkPlace` now resets `_first_deposit_time` to `None` in addition to resetting counters, supporting window-first throughput calculations.

---

## [0.1.2] — 2026-06-26

### Added

- **`action_timeout_secs`** — New optional field on `Transition`. When set, the engine
  enforces a wall-clock deadline on the action callable. Timed-out actions trigger
  atomic rollback (all consumed tokens returned to source; data tokens with a one-second
  `available_at` delay to prevent livelock) and fire the `on_error` callback with a
  descriptive `RuntimeError`. Uses a dedicated secondary executor (`cpnx-action`) to
  avoid nested-future deadlock. The underlying OS thread is not killed; callers must
  apply native I/O timeouts inside their actions to prevent zombie thread accumulation.

### Fixed

- **Encapsulation** — `SubstitutionTransition` no longer writes a `_parent_transition`
  back-reference onto the child `PetriNet` instance. Double-mapping prevention is now
  tracked entirely on the parent side via a class-level `WeakSet`, keeping subnets
  fully agnostic of their parents.
- **Atomicity** — On transition failure, all consumed tokens (resource *and* data) are
  now returned to their original source places, preserving the formal Marking exactly.
  Previously, data tokens were routed to a dead-letter `error_place`, which altered
  the Marking in a way that could not be modelled as a clean rollback. Data tokens
  receive a one-second `available_at` delay on return to prevent livelock when an
  action raises persistently. The `on_error` callback continues to fire for
  observability.
- **Sandbox purity** — `verify_callable_purity` now inspects `FunctionDef` default
  argument values. Mutable literals (`list`, `dict`, `set`) as parameter defaults
  raise `PermissionError`, closing a loophole that permitted hidden persistent state
  between transition firings.
- **Sandbox deadlock** — `SandboxEvaluator.evaluate` now blocks all iteration
  constructs: `while`, `for`, and `async for` loops, as well as list, dict, and set
  comprehensions and generator expressions. These could previously hold the engine lock
  indefinitely; comprehensions were the critical gap as they are `ast.ListComp` nodes,
  not `ast.For`, and would have bypassed a loop-only check.

---

## [0.1.1] — 2025-06-01

### Added

- Python 3.13 and 3.14 to the CI test matrix.
- PyPI publication metadata (author, `AGENTS.md`, `.env` in `.gitignore`).

---

## [0.1.0] — initial release

### Added

- Core `PetriNet` executor with thread-pool-based transition firing.
- `Place`, `ResourcePlace`, `PacedResourcePlace`, `ThresholdPlace`.
- `Token` (immutable, `FrozenDict` payload) with `evolve()` for functional updates.
- `Transition` and `SubstitutionTransition` for hierarchical (nested) nets.
- `InputArc` and `OutputArc` with arc expressions and guard support.
- Timed tokens via `available_at` and a logical model clock (`advance_time()`).
- `SandboxEvaluator` for hermetic string expression evaluation.
- `verify_callable_purity` for AST-level callable safety checks.
- Nondeterministic conflict resolution using `random.choice`.
- Bipartite topology enforcement (no place-to-place or transition-to-transition arcs).
- Pre-flight deposit validation for atomic output commits.
- Configurable `expr_timeout_secs` to bound expression evaluation time.
- `on_token_deposited`, `on_transition_fired`, and `on_error` callbacks.
- Graphviz snapshot export via `visualization.py`.
