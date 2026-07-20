# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Transient-failure retries now honour the logical clock.** When a transition's action failed and its data token was rolled back for retry, the token's `available_at` was computed as `time.monotonic() + retry_delay` — the wall clock — while every other availability check (`Place.retrieve`/`can_retrieve`, `PacedResourcePlace` pacing, input-arc settle windows) compares against the net's `model_time`. On a net driven by `advance_time`, the rolled-back token therefore received a deadline of seconds-since-boot (order 10^6–10^7) against a logical clock typically near zero, so it could **never** become retrievable again: the retry was stranded in its source place indefinitely, and `run()` could return quiescent with that work still pending. The rollback path now applies `retry_delay` against the same clock as pacing and settle, using the established `model_time if model_time is not None else time.monotonic()` fallback. Nets that never call `advance_time` are unaffected — the fallback reproduces the previous value exactly.

---

## [0.3.2] — 2026-07-11

### Added

- **`BindingPolicy.RANDOM`** — Enumerates the guard-satisfying bindings and selects one **uniformly at random**. Reproducible when the net is constructed with a `seed` (and `max_workers=1`); otherwise it varies run to run. Unlike `FIRST`, a guard-free `RANDOM` transition still selects among *all* eligible token groups (there is no guard-free fast path), so it always enumerates. Phase 2 of the plan in [`docs/adr/0001-combinatorial-binding-search.md`](docs/adr/0001-combinatorial-binding-search.md).
- **`BindingPolicy.PRIORITY`** — Enumerates the guard-satisfying bindings and selects the one **minimizing** `Transition.binding_priority_key`. Deterministic; ties fall to insertion order.
- **`Transition.binding_priority_key`** — Optional pure `Callable[[list[Token]], object]` mapping a candidate binding (its flat token list) to a comparable sort key for `PRIORITY`. `None` (default) means oldest-first — the minimum `Token.created_at` across the binding's **data** tokens (resource permits are excluded, so an ancient permit can't tie every candidate and collapse the choice to insertion order). Must be callable or `None` — a non-callable (e.g. a string expression, which is not supported) raises `TypeError` at assignment. A candidate whose key raises or is incomparable with the running best is skipped; if every candidate is skipped the first satisfying binding (insertion order) is used, so the firing path never disagrees with the enabling probe — and `on_error` fires (once per pass, off the lock) so a wholly-broken key is not silent. The key runs **inline under the engine lock with no timeout** (unlike callable guards, which use the expression pool), so it must be trivially cheap.

### Notes (search-limit clarification)

- `RANDOM`/`PRIORITY` over the limit only "select from a truncated prefix and fire" **when that prefix contains a satisfying binding**. A guarded `RANDOM`/`PRIORITY` transition whose only satisfying binding lies beyond the first `binding_search_limit` candidates finds nothing in the prefix and is disabled for that check — it can stall and let `run()` return quiescent exactly like `FIRST`. Raise `binding_search_limit` if this matters.
- **`PetriNet(seed=...)`** — Optional integer seed for the net's internal `random.Random`. When set it makes the run reproducible, driving **both** the scheduler's tie-break among equal-priority enabled transitions **and** `RANDOM` binding selection. Pair with `max_workers=1` for strict replay.

### Changed

- The scheduler's tie-break among equal-priority enabled transitions now draws from the net's `random.Random(seed)` instance instead of the global `random` module. Behavior is unchanged when unseeded, but the global `random.seed()` no longer influences cpnx scheduling — seed the net instead.

### Notes

- **Reproducibility is probe-independent.** Enabling/quiescence checks (`is_dead`, `is_quiescent`) use an existence-only probe that never draws the RNG, so a timing-dependent number of `run()` poll iterations cannot perturb a seeded `RANDOM` run.
- **Cost.** `RANDOM`/`PRIORITY` cannot short-circuit — they scan the whole (bounded) candidate set to sample or rank — so they are typically several times costlier than `FIRST` on the firing path. `binding_search_limit` still bounds the work; if the candidate space exceeds it, selection is over the first `limit` candidates (a **truncated prefix**) and `on_binding_search_exhausted` fires even though a binding is returned.
- The default policy remains `BindingPolicy.LEGACY`; `LEGACY`/`FIRST` behavior is unchanged from 0.3.1.

---

## [0.3.1] — 2026-07-10

### Added

- **`BindingPolicy`** — A new public enum (exported from `cpnx`) controlling how a transition resolves which input tokens bind it. `BindingPolicy.LEGACY` (the default) preserves the historical behavior: only the leading `count` tokens of each input place (FIFO / `InputArc.expression` ordering) are tested, with the guard evaluated once — subject to head-of-line (HoL) blocking. `BindingPolicy.FIRST` performs a **deterministic-complete binding search**, walking input-token combinations in stable insertion order and selecting the first combination whose guard holds; this fixes HoL blocking while remaining reproducible, and reduces to `LEGACY` (no search) when the transition has no guard.
- **`Transition.binding_policy`** — Optional per-transition policy (`BindingPolicy | None`, default `None`). `None` inherits the owning net's default, so binding search is **opt-in per transition or net-wide**.
- **`PetriNet(binding_policy=..., binding_search_limit=...)`** — The net-wide default policy (`binding_policy`, default `BindingPolicy.LEGACY`) and the cap on combinations tried per enabling check under `FIRST` (`binding_search_limit`, default `1000`).
- **`PetriNet.on_binding_search_exhausted`** — A new optional callback (`Callable[[str], None] | None`) fired **outside** the engine lock (so it may call back into the net) with the transition name when a `FIRST` search reaches `binding_search_limit` without finding a satisfying binding; the transition is treated as disabled for that check, never a silent hang. Exhaustions are de-duplicated *within* a single enabling pass before dispatch; a busy `run()` loop still fires it once per `step()`/`is_quiescent()` iteration, so keep the callback cheap and debounce on your side if needed. `binding_search_limit` must be `>= 1` (validated in the constructor).

### Notes

- The default remains **`BindingPolicy.LEGACY`**, so existing nets keep their historical enabling/consumption behavior; HoL blocking is only fixed when `FIRST` is explicitly selected. Two deliberate, engine-wide refinements to the resolve/consume path apply even under `LEGACY`, so behavior is not strictly byte-for-byte for two edge cases: (1) an input arc's selection **expression is now evaluated once per firing** (at resolution) rather than a second time at consumption — nets relying on a stateful/nondeterministic `InputArc.expression` observe the single evaluation; (2) token **consumption now goes exclusively through `Place.retrieve_specific`** (by token id) for every arc, so third-party `Place` subclasses that override `retrieve`/`retrieve_all` but not `retrieve_specific` no longer see their overrides invoked during firing.
- Under `FIRST`, `binding_search_limit` bounds the search's time **and** memory (each arc's candidate stream is truncated to `binding_search_limit + 1` groups before the Cartesian product is formed), and with a callable guard also bounds the lock-hold time to roughly `binding_search_limit × expr_timeout_secs`. Because an exhausted search counts as "disabled", a net whose only satisfiable binding lies past the limit can reach quiescence with that work still pending, signalled only via `on_binding_search_exhausted`.
- Resource (`ResourcePlace`/`PacedResourcePlace`) permit arcs contribute `C(capacity, count)` interchangeable combinations to the search; list resource arcs before data arcs in `Transition.inputs` (and/or raise the limit) so the data dimension varies first. See `BindingPolicy` for details.
- This is the **opt-in realization of the combinatorial binding** that the 0.3.0 **Notes** section had said was intentionally not implemented. The search walks the `itertools.product` of each arc's `count`-sized combinations and short-circuits on the first satisfying binding; each dimension is truncated to `binding_search_limit + 1` groups first, so both work and memory stay bounded by the limit rather than by `C(N, count)`. It is Phase 1 of the plan in [`docs/adr/0001-combinatorial-binding-search.md`](docs/adr/0001-combinatorial-binding-search.md).
- Planned follow-ups: **Phase 2** (`RANDOM`/`PRIORITY` selection policies) and **Phase 3** (flip the default to complete binding search at a major version bump).

---

## [0.3.0] — 2026-06-30

### Added

- **`SandboxEvaluator.compile_expression`** — Validates a string expression via the static AST security walk and returns a compiled `eval`-mode code object. Results are cached by source text, so an identical expression is parsed and compiled at most once.
- **`SandboxEvaluator.evaluate_compiled`** — Evaluates a pre-compiled code object against a context dictionary, skipping the parse/compile step.
- **`SandboxEvaluator.maybe_compile`** — Compiles a value if it is a string expression, otherwise returns `None` (for callables/`None`), centralizing the string-vs-callable rule.
- **Benchmarks** — `benchmarks/bench_enablement.py` (native stdlib, no dependencies) plus a `benchmarks/README.md` documenting methodology for pre/post performance comparison.

### Changed

- **Compile-once string expressions** — Guard and arc string expressions are now parsed, security-walked, and compiled once at `Transition`/`InputArc`/`OutputArc` construction time and reused on every enablement check, instead of re-parsing/re-compiling on every `SandboxEvaluator.evaluate()` call. The engine's `_is_transition_enabled` hot path reuses the compiled object via `evaluate_compiled`. End-to-end enablement checks on the string-guard path improved roughly 7–8×; the isolated compile path is ~215× faster. `SandboxEvaluator.evaluate()` is preserved as a thin, cached wrapper — the public API and `PermissionError` semantics are unchanged.
- **Eager validation of string expressions** — A malformed or forbidden **string** guard/arc expression now raises `PermissionError` at `Transition`/`InputArc`/`OutputArc` construction (and on reassignment) rather than being silently treated as a disabled transition at run time. Compiled objects stay in sync with the live `guard`/`expression`, so post-construction reassignment recompiles rather than evaluating a stale predicate.

### Notes

- Lazy `itertools.product` binding enumeration (a commonly suggested optimization) was intentionally **not** implemented: the engine resolves a single deterministic binding per transition (no Cartesian product across input places), so there is no combinatorial explosion to short-circuit.

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
