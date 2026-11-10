# ADR 0012 — Run a logical-clock drive's firings on the driving thread

- **Status:** Accepted
- **Date:** 2026-11-10
- **Relates to:** [0003](0003-inline-execution-and-string-removal.md) (inline guards and arc
  expressions, the same trade of a thread handoff for a direct call),
  [0006](0006-incremental-enablement.md) (the scheduler whose per-firing cost is now flat);
  https://github.com/philgresh/cpnx/issues/79

## Context

`drive_to_quiescence` keeps at most one firing in flight: after each `step()` it waits in
`_await_inflight` until the action commits. Even so, every firing went through the thread pool.
`step()` submitted `_execute_transition` to the executor, and `_await_inflight` spun on
`_running_count` with `time.sleep(0)` until the worker committed.

With nothing to overlap, the handoff and the spin are pure overhead. Since #59's fixes the rest
of a firing's cost is flat in backlog depth, so this constant bounds how many events a
logical-clock run can process. #79 profiled a two-transition chain at 63 µs per firing, with
`_await_inflight` at about 59% of cumulative time (about 93 `sleep(0)` spins per firing).

The `max_spins` cap on that spin is also the drive's only guard against an action that never
returns. Several tests in `tests/test_subnet_failure.py` rely on it: a subnet's drive gives up
on a slow action by its spin bound, the substitution firing fails while the action is still
running, and the parent waits for it to settle before restoring the subnet.

## Decision

`drive_to_quiescence` takes `inline_actions: bool = False`. When it is set, the drive selects
and consumes a firing under the engine lock exactly as `step()` does, releases the lock, flushes
the deferred selection callbacks, and then calls `_execute_transition` on the driving thread.
That call commits or rolls back, runs the callbacks off the lock and decrements `_running_count`
before it returns, so the following `_await_inflight` finds nothing in flight.

- **Order is unchanged.** The drive already serialized firings, so selection, commits,
  rollbacks and draws from the scheduling and retry streams happen in the same order. Only the
  thread that runs the action changes. `tests/test_inline_drive.py` drives the same seeded nets
  both ways (RANDOM bindings, a paced pool, jittered retries that dead-letter, a scheduler
  tie-break, and a substitution transition whose subnet fails and is restored) and compares the
  firing log, `DriveResult`, the clock, the final marking and both RNG states.
- **Substitution firings run inline too.** The subnet's drive inherits the setting through
  `_drive_limits`, like `max_ticks` and `max_spins`. Inline, nothing of a failed firing is left
  running when its subnet drive returns, so the subnet settles at once and the wait in
  `_await_settled` is never reached. Firings of one transition are serialized by its
  `_firing_lock`, and a serialized drive never has two firings of one transition in progress, so
  the driving thread never waits on a firing it is itself running.
- **Failure paths are the same code.** `_execute_transition` already catches what an action
  raises, so rollback, `RetryPolicy` scheduling, dead-lettering, `on_error` and the
  settle-then-restore logic run unchanged for every `Exception`.
- **An interrupt is not a failed firing.** Inline, a `BaseException` that is not an `Exception`
  (`KeyboardInterrupt`, `SystemExit`) raised by the action is the caller stopping the drive.
  The firing's tokens go back unchanged, as for a firing an enclosing failure cuts short
  (`_return_interrupted`: no attempt counted, no retry delay, nothing dead-lettered, no
  callbacks), and the interrupt propagates out of the drive. For a substitution firing the
  subnet's own firing does the same, and the parent's firing then restores the subnet to its
  checkpoint before re-raising. On the pool the interrupt has nowhere to go, and handing the
  tokens back would re-fire the same action forever, so a worker still rolls the firing back
  as a failure. This holds for an interrupt raised inside the action body; one that lands
  while the engine is committing or rolling back can leave that firing half done.
- **A firing that never starts is undone.** Between consuming a firing and running it, the
  drive dispatches the callbacks deferred during selection (`on_error`,
  `on_binding_search_exhausted`). If one of them raises a `BaseException` they do not catch,
  the consumed firing would never run and would count as running forever. The drive returns
  its tokens as they were taken and drops the count before re-raising. On the pool the firing
  is already submitted by then, so this window did not exist.
- **A closed net refuses to fire.** An inline drive never submits to the pool, so after the
  net's `with` block exits it checks a flag set by `__exit__` and raises `RuntimeError` before
  consuming anything, as `step()` does when its submit fails.
- **`step()` and `run()` keep the thread pool.** They exist to overlap actions.

## Why opt-in

Inline execution gives up the hung-action guard. An action that blocks now blocks the drive, and
`max_spins` cannot cut it short. A net whose actions can hang should set `action_timeout_secs`:
that action still runs on its own worker under the timeout, inline drive or not. Making inline
the default would silently remove that guard from existing callers, and would change the
behaviour the subnet-failure tests pin (a drive that gives up on a still-running action). With
every drive forced inline, the full suite passed except those six tests.

## Consequences

- The #79 repro drops from about 137 to about 50 µs per firing (about 2.8×) on a shared Linux
  container; `bench_timed_backlog.py`'s drive arm gains an `inline` column (about 1.5× there,
  where the action does more work).
- The `sleep(0)` spin stays for the default path. Replacing it with a condition variable would
  change what `max_spins` counts, which callers and tests set; that is left for a separate change.
- A future default flip is possible once callers have had the option, if the hung-action guard
  is judged better served by `action_timeout_secs` alone.

## Alternatives considered

- **Condition-variable barrier only.** Removes the spin but keeps the handoff and the `submit`
  cost, and changes `max_spins` semantics.
- **A net-level flag.** The setting belongs to how a net is driven, not to the net, and a
  per-call argument flows to subnet drives the same way `max_ticks` and `max_spins` already do.
- **Batching several firings per handoff.** Changes when outputs become visible to the next
  enablement check, which the drive's single-firing barrier exists to prevent.
