"""Concurrent Petri net executor."""

import concurrent.futures
import itertools
import math
import random
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Iterator, TypeAlias

from cpnx.places import PacedResourcePlace, Place, ResourcePlace, SinkPlace
from cpnx.sandbox import SandboxEvaluator
from cpnx.tokens import Token
from cpnx.transitions import BindingPolicy, InputArc, OutputArc, SubstitutionTransition, Transition
from cpnx.visualization import snapshot, to_dot

_DepositFn: TypeAlias = Callable[[str, Token], None]
"""Callable that deposits a token into a named place. Must be invoked under the engine lock."""

_Binding: TypeAlias = list[tuple[InputArc, list[Token]]]
"""A resolved binding: each input arc paired with the exact tokens it will consume."""


@dataclass(frozen=True)
class DriveResult:
    """Counters from a [`drive_to_quiescence`][cpnx.PetriNet.drive_to_quiescence] run."""

    steps: int  #: number of successful ``step()`` firings
    ticks: int  #: number of logical-clock advances (cooldown/settle boundaries jumped)


def _flatten_binding(binding: _Binding) -> list[Token]:
    """Flatten a resolved binding into a single guard-candidate token list, in arc order."""
    return [t for _, tokens in binding for t in tokens]


def _default_priority_key(tokens: list[Token]) -> float:
    """Default `BindingPolicy.PRIORITY` key: oldest-first by minimum data-token `created_at`.

    Resource tokens (`Token.is_resource`) are excluded from the minimum: a permit created at
    net construction is older than any data token, so including it would make every candidate
    binding tie on the permit's timestamp and collapse the selection to insertion order — the
    "oldest-first" default would be silently defeated exactly for the resource-arc pattern the
    docs recommend. A resource-only binding (no data tokens) falls back to the minimum over all
    tokens. Note the exclusion keys off the built-in resource colour, so user-defined
    resource-like colours are not filtered.
    """
    data_ts = [t.created_at for t in tokens if not t.is_resource]
    return min(data_ts) if data_ts else min(t.created_at for t in tokens)


def _enact_planned_deposits(
    planned_deposits: list[tuple[str, Token]],
    active_outputs: list[tuple[OutputArc, bool]],
    res_deque: deque[Token],
    out_deque: deque[Token],
    *,
    deposit: _DepositFn,
) -> list[tuple[str, Token]]:
    """Commit deposits and drain consumed tokens from their deques.

    ``deposit`` must be called under the engine lock.
    """
    deposited: list[tuple[str, Token]] = []
    for place_name, token in planned_deposits:
        deposit(place_name, token)
        deposited.append((place_name, token))

    for arc, is_res_place in active_outputs:
        for _ in range(arc.count):
            if is_res_place:
                res_deque.popleft()
            else:
                out_deque.popleft()
    return deposited


def _return_leftover_resources(
    res_deque: deque[Token],
    token_sources: list[tuple[str, Token]],
    *,
    deposit: _DepositFn,
) -> list[tuple[str, Token]]:
    """Return unconsumed resource tokens to their source places.

    ``deposit`` must be called under the engine lock.
    """
    deposited: list[tuple[str, Token]] = []
    while res_deque:
        leftover_token = res_deque.popleft()
        for src_name, t in token_sources:
            if t.id == leftover_token.id:
                deposit(src_name, leftover_token)
                deposited.append((src_name, leftover_token))
                break
    return deposited


def _rollback_data_token(
    t: Token,
    src_name: str,
    transition: Transition,
    retry_delay: float,
    error_place: str,
    ref_time: float,
) -> tuple[str, Token, bool]:
    max_retries = transition.max_retries
    if max_retries is None or t.attempts < max_retries:
        retry_at = ref_time + retry_delay
        return src_name, t.evolve(available_at=retry_at, attempts=t.attempts + 1), False
    return error_place, t.evolve(available_at=0.0), True


def _process_rollback_token(
    t: Token,
    src_name: str,
    transition: Transition,
    retry_delay: float,
    error_place: str,
    ref_time: float,
) -> tuple[str, Token, bool]:
    if t.is_resource:
        return src_name, t, False
    return _rollback_data_token(t, src_name, transition, retry_delay, error_place, ref_time)


def _rollback_failed_transition(
    transition: Transition,
    token_sources: list[tuple[str, Token]],
    *,
    deposit: _DepositFn,
    retry_delay: float,
    error_place: str,
    ref_time: float,
) -> tuple[list[tuple[str, Token]], list[Token], list[Token]]:
    """Return all consumed tokens to their source places after a failed firing.

    ``deposit`` must be a callable that is safe to invoke under the engine lock —
    callers are responsible for holding it before calling this function.

    ``retry_delay`` is applied against ``ref_time``, so a retried token's ``available_at``
    lands on the same clock as pacing/settle checks. Callers pass the net's resolved clock
    (`PetriNet._get_model_time_under_lock`), which is the logical clock when one is set and
    ``time.monotonic()`` otherwise.
    """
    deposited: list[tuple[str, Token]] = []
    dead_lettered_data_tokens: list[Token] = []
    data_tokens = [t for _, t in token_sources if not t.is_resource]
    for src_name, t in token_sources:
        dest, rollback_t, is_dead_letter = _process_rollback_token(
            t, src_name, transition, retry_delay, error_place, ref_time
        )
        deposit(dest, rollback_t)
        deposited.append((dest, rollback_t))
        if is_dead_letter:
            dead_lettered_data_tokens.append(rollback_t)
    return deposited, dead_lettered_data_tokens, data_tokens


class PetriNet:
    """A concurrent, thread-safe executor for coloured Petri nets.

    A [`PetriNet`][cpnx.PetriNet] owns a collection of named
    [`Place`][cpnx.Place] instances and [`Transition`][cpnx.Transition] instances connected by
    arcs, plus three internal thread pools: one that selects and fires transitions
    (`max_workers` wide), one that runs transition actions (guarded per-transition by
    `Transition.action_timeout_secs`), and a small pool used to evaluate guard/arc-selection
    expressions under a timeout. All mutations of places and transitions (`deposit`, firing,
    rollback) happen while holding a single internal engine lock, so the net is safe to drive
    from multiple threads concurrently; transition *actions* themselves run outside that lock.

    Resource tokens (`Token.is_resource`) are always returned to their source place once a
    firing completes, whether it succeeds or fails. For data tokens, the net supports three
    error-handling dispositions:

    - **A. Colour-routed error (primary/canonical)** — the action catches its own
      exception, returns an error-coloured token, and output-arc expressions (e.g. using
      `OutputArc.expression`) route success vs error tokens to different places. This
      preserves firing rules and token conservation (1-in-1-out).
    - **B. Bounded atomic-retry** — on action failure/exception, the data token is rolled
      back to its source place with a delay (`retry_delay`) and an incremented `attempts`
      counter, retrying up to `Transition.max_retries` times (default 5). Once exhausted,
      it is dead-lettered to `error_place`.
    - **C. Immediate dead-letter** — setting `max_retries=0` on a transition routes any
      action failure immediately to `error_place`.

    Note that `error_place` can be configured as a [`SinkPlace`][cpnx.SinkPlace]
    (e.g. `SinkPlace("failed", keep_last=10)`) to keep only the last N failures for
    diagnostics, preventing unbounded memory growth in long-running streaming nets.

    Attributes:
        max_workers: Maximum number of transitions that may fire concurrently.
        error_place: Name of the place that receives dead-lettered data tokens.
        cooldown_interval: Polling interval in seconds used by [`run`][cpnx.PetriNet.run]
            while waiting for paced/cooldown tokens to become available.
        timeout_secs: Maximum execution time in seconds for transition action callables.
        expr_timeout_secs: Maximum execution time in seconds for guard and arc-selection
            expression callables.
        retry_delay: Delay in seconds applied to data tokens rolled back on transient failure.
        places: Mapping of registered place name to `Place` instance.
        transitions: Mapping of registered transition name to `Transition` instance.
        on_transition_fired: Optional callback `(transition_name: str, duration_secs: float)
            -> None`, called after a transition's action completes successfully. Fires outside
            the engine lock, so it is safe to call [`deposit`][cpnx.PetriNet.deposit] from here.
        on_token_deposited: Optional callback `(place_name: str, token: Token) -> None`, called
            after any token is deposited into any place. Fires outside the engine lock. Do
            **not** call [`add_place`][cpnx.PetriNet.add_place] or
            [`add_transition`][cpnx.PetriNet.add_transition] from within this callback.
        on_token_dead_lettered: Optional callback `(transition_name: str, token: Token) -> None`,
            called when a data token is dead-lettered to `error_place` (retries exhausted, or
            `max_retries=0`). Fires outside the engine lock.
        on_error: Optional callback `(transition_name: str, exc: Exception, token: Token | None)
            -> None`, called when a transition's action raises. `token` is the data token that
            was routed to the error place or rolled back, or `None` if the transition consumed
            no data tokens. Fires outside the engine lock.

    Example:
        ```python
        net = PetriNet(
            max_workers=4,
            places=[Place("source"), Place("sink")],
            transitions=[
                Transition(
                    name="process",
                    inputs=[InputArc("source")],
                    outputs=[OutputArc("sink")],
                    action=lambda tokens: tokens,
                )
            ],
        )
        net.deposit("source", Token(payload={"job_id": 1}))
        net.run()  # runs to quiescence
        ```

    Use as a context manager to ensure the thread pool shuts down cleanly:

        ```python
        with PetriNet(max_workers=4) as net:
            ...
            net.run()
        ```
    """

    def __init__(
        self,
        max_workers: int = 4,
        error_place: str = "failed",
        places: list[Place] | None = None,
        transitions: list[Transition] | None = None,
        cooldown_interval: float = 0.05,
        timeout_secs: float = 1.0,
        expr_timeout_secs: float = 0.1,
        retry_delay: float = 1.0,
        binding_policy: BindingPolicy = BindingPolicy.LEGACY,
        binding_search_limit: int = 1000,
        seed: int | None = None,
    ) -> None:
        """Construct the net, its thread pools, and register any initial places/transitions.

        Args:
            max_workers: Maximum number of transitions that may fire concurrently
                (default: 4). Also sizes the internal action thread pool.
            error_place: Name of the place that receives data tokens from failed
                         transitions (default: `"failed"`). Created automatically as a
                         standard `Place`, but can be overridden by registering a custom
                         place (like a `SinkPlace`) with the same name before it is needed.
            places: Optional list of [`Place`][cpnx.Place] (or subclass) instances to
                    register at construction time (default: `None`).
            transitions: Optional list of [`Transition`][cpnx.Transition] instances to
                         register at construction time (default: `None`).
            cooldown_interval: Cooldown check polling interval in seconds, used by
                               [`run`][cpnx.PetriNet.run] (default: 0.05).
            timeout_secs: Maximum allowed execution time in seconds for transition
                          action callables that declare no per-transition timeout
                          (run off the engine lock) (default: 1.0).
            expr_timeout_secs: Maximum allowed execution time in seconds for guard
                               and arc expression callables. These are evaluated
                               while holding the engine lock, so this value caps how long a
                               *single* guard/expression evaluation can block concurrent
                               `deposit()` and `step()` calls. Note that under
                               `BindingPolicy.FIRST` one enabling check may evaluate a
                               callable guard up to `binding_search_limit` times in sequence,
                               so the worst-case lock-hold time is
                               `binding_search_limit * expr_timeout_secs` — size both
                               accordingly. Keep well under 1 s (default: 0.1).
            retry_delay: Delay in seconds to apply to data tokens when rolling them
                         back to their source places on transient failure (default: 1.0).
            binding_policy: Net-wide default strategy for resolving which input tokens
                         bind a transition — see [`BindingPolicy`][cpnx.BindingPolicy].
                         Defaults to `BindingPolicy.LEGACY` (historical head-of-queue
                         behavior), so existing nets are unaffected. A transition may
                         override this via its own `binding_policy`.
            binding_search_limit: Maximum number of input-token combinations tried per
                         enablement check when a transition uses `BindingPolicy.FIRST`
                         (default: 1000). Bounds both the work and the memory of a search
                         (each arc's candidate stream is truncated to this many groups), and
                         with a callable guard also bounds the lock-hold time to roughly
                         `binding_search_limit * expr_timeout_secs`. If exhausted without
                         finding a guard-satisfying binding, the transition is treated as
                         disabled for that check and `on_binding_search_exhausted` (if set) is
                         invoked after the lock releases. Because exhaustion means "disabled",
                         a net whose only satisfiable binding lies beyond the limit can reach
                         quiescence — and [`run`][cpnx.PetriNet.run] can return — with that
                         work still pending, signalled only via the callback; raise the limit
                         if this is a concern. Must be `>= 1`. Ignored under
                         `BindingPolicy.LEGACY` and for guard-free `FIRST` transitions, which
                         never search. Under `BindingPolicy.RANDOM`/`PRIORITY` the search
                         cannot short-circuit (it must scan every candidate to sample or rank),
                         so the limit truncates the selection space to the first `limit`
                         candidates: if that prefix contains a satisfying binding they select
                         over it and fire (signalling the truncation); if it does not, the
                         transition is disabled for that check and can stall exactly like
                         `FIRST`.
            seed: Optional integer seed for the net's internal random generator
                         (`random.Random(seed)`). When set, it drives **both** the scheduler's
                         tie-break among equal-priority enabled transitions **and**
                         `BindingPolicy.RANDOM` binding selection. `None` (default) uses an
                         unseeded instance (non-reproducible). Reproducibility caveats: use
                         `max_workers=1` for strict replay — above 1, identical seeds are **not
                         guaranteed** to reproduce, because concurrent action-commit ordering
                         (an OS-scheduled race) can change which transitions are enabled at a
                         firing step and hence the draw sequence. Each
                         [`SubstitutionTransition`][cpnx.SubstitutionTransition] subnet is its
                         own `PetriNet` with its own RNG, so seed subnets separately if they
                         contain `RANDOM` transitions. Seeded streams are not stable across
                         cpnx versions.

        Raises:
            ValueError: If `binding_search_limit < 1`.
        """
        self.max_workers = max_workers
        self.error_place = error_place
        self.cooldown_interval = cooldown_interval
        self.timeout_secs = timeout_secs
        self.expr_timeout_secs = expr_timeout_secs
        self.retry_delay = retry_delay
        self.binding_policy = binding_policy
        if binding_search_limit < 1:
            raise ValueError(f"binding_search_limit must be >= 1, got {binding_search_limit}.")
        self.binding_search_limit = binding_search_limit
        self._rng = random.Random(seed)
        self._has_timed_features = False
        self._model_time: float | None = None
        self.places: dict[str, Place] = {}
        self.transitions: dict[str, Transition] = {}
        self._lock = threading.Lock()
        self._running_count = 0
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._action_executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="cpnx-action")
        self._expr_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cpnx-expr")
        # Signalled whenever tokens are deposited; wakes run() without busy-waiting.
        self._work_available = threading.Event()

        #: Called after a transition completes successfully.
        #: Signature: `(transition_name: str, duration_secs: float) -> None`.
        #: Fires outside the engine lock — safe to call `deposit` from here.
        self.on_transition_fired: Callable[[str, float], None] | None = None

        #: Called after any token is deposited into any place.
        #: Signature: `(place_name: str, token: Token) -> None`.
        #: Fires outside the engine lock — safe to call `deposit` from here.
        #: Warning: do **not** call `add_place` or `add_transition` from
        #: within this callback.
        self.on_token_deposited: Callable[[str, Token], None] | None = None

        #: Called when a data token is dead-lettered to the error place (due to exhausted retries or immediate failure).
        #: Signature: `(transition_name: str, token: Token) -> None`.
        #: Fires outside the engine lock.
        self.on_token_dead_lettered: Callable[[str, Token], None] | None = None

        #: Called when a transition's action raises an exception.
        #: Signature: `(transition_name: str, exc: Exception, token: Token | None) -> None`.
        #: `token` is the data token that was routed to the error place or rolled back,
        #: or `None` if the transition had no data inputs.
        #: Fires outside the engine lock.
        self.on_error: Callable[[str, Exception, Token | None], None] | None = None

        #: Called with a transition name when a binding search reaches `binding_search_limit`.
        #: The meaning depends on policy: under `BindingPolicy.FIRST` the limit was hit
        #: **without** a guard-satisfying binding, so the transition is treated as disabled for
        #: that check; under `BindingPolicy.RANDOM`/`PRIORITY` (which must scan every candidate)
        #: it signals that the candidate space was **truncated** to the first `limit`
        #: candidates — a binding may still have been selected and the transition may still
        #: fire. In both cases: raise `binding_search_limit` if you need the full space
        #: considered.
        #: Signature: `(transition_name: str) -> None`.
        #: Fires **outside** the engine lock — it is safe to call back into the net (e.g.
        #: `deposit`) from here. Exhaustions are de-duplicated *within* a single enabling pass
        #: (so cross-thread interleavings collapse to one call), but it still fires once per
        #: pass: a busy `run()` loop re-checks each transition on every `step()`/
        #: `is_quiescent()` iteration, so an over-limit transition signals repeatedly while
        #: the loop runs. Keep the callback cheap, and debounce on your side if needed.
        self.on_binding_search_exhausted: Callable[[str], None] | None = None
        #: Transition names whose binding search exhausted the limit during the current
        #: lock-holding enabling pass; drained and dispatched (de-duplicated) after the lock
        #: is released. A `set` so repeated exhaustions in one pass collapse to one callback.
        self._pending_exhaustions: set[str] = set()
        #: Transition name -> first exception from a `binding_priority_key` that raised for
        #: *every* candidate binding during the current enabling pass (so `PRIORITY` silently
        #: fell back to insertion order). Drained and dispatched via `on_error` (de-duplicated)
        #: after the lock releases, so a broken key surfaces instead of failing silently.
        self._pending_key_failures: dict[str, Exception] = {}

        self.add_place(Place(error_place))
        for p in places or []:
            self.add_place(p)
        for t in transitions or []:
            self.add_transition(t)

    def _call_expr(self, fn, *args, timeout: float | None = None):
        from concurrent.futures import TimeoutError as FuturesTimeout

        t = timeout if timeout is not None else self.timeout_secs
        fut = self._expr_executor.submit(fn, *args)
        try:
            return fut.result(timeout=t)
        except FuturesTimeout as exc:
            raise RuntimeError(f"Expression {fn!r} exceeded {t}s — possible I/O call") from exc

    def _get_model_time_under_lock(self) -> float:
        """Internal helper to get the model time without acquiring the engine lock."""
        if self._model_time is not None:
            return self._model_time
        return time.monotonic()

    @property
    def model_time(self) -> float:
        """Current time used for settle windows, cooldowns, and thresholds.

        Returns the logical clock set via [`advance_time`][cpnx.PetriNet.advance_time], if one
        has ever been set; otherwise returns the real wall-clock time (`time.monotonic()`).
        A net that never calls `advance_time` runs entirely on real time.

        Returns:
            The current logical clock value, or `time.monotonic()` if no logical clock is set.
        """
        with self._lock:
            return self._get_model_time_under_lock()

    def advance_time(self, new_time: float) -> None:
        """Advance the net's logical clock to `new_time` and wake any waiting `run` loop.

        Once called, the net switches from real (`time.monotonic()`) to logical time for all
        settle-window, cooldown, and threshold checks. Subsequent calls must strictly increase
        the clock.

        Args:
            new_time: The new logical timestamp. Must be strictly greater than the current
                model time (if one has already been set).

        Raises:
            ValueError: If `new_time` is less than or equal to the current model time.
        """
        with self._lock:
            if self._model_time is not None and new_time <= self._model_time:
                raise ValueError(
                    f"Clock mutability violation: cannot decrement global clock backward or equal "
                    f"from {self._model_time} to {new_time}."
                )
            self._model_time = new_time
        # Signal that new work might have become available due to time advancing!
        self._work_available.set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_place(self, place: Place) -> None:
        """Register `place` with the net under its `place.name`.

        Must be called before any transition arc references this place by name
        and before [`run`][cpnx.PetriNet.run] or [`step`][cpnx.PetriNet.step] is invoked.

        Args:
            place: A [`Place`][cpnx.Place], [`ResourcePlace`][cpnx.ResourcePlace],
                   [`PacedResourcePlace`][cpnx.PacedResourcePlace], or
                   `ThresholdPlace`/`SinkPlace` instance.

        Raises:
            ValueError: If `place.name` is already registered as a transition name.
        """
        with self._lock:
            if place.name in self.transitions:
                raise ValueError(f"Name overlap: '{place.name}' is already registered as a Transition.")
            self.places[place.name] = place
            if isinstance(place, PacedResourcePlace):
                self._has_timed_features = True

    def _validate_new_transition(self, transition: Transition) -> None:
        if transition.name in self.places:
            raise ValueError(f"Name overlap: '{transition.name}' is already registered as a Place.")
        for arc in transition.inputs + transition.outputs:
            if arc.place == transition.name or arc.place in self.transitions:
                raise TypeError(
                    f"Arc target '{arc.place}' is a Transition, not a Place. Arcs must connect Places↔Transitions only."
                )

    def add_transition(self, transition: Transition) -> None:
        """Register `transition` with the net under its `transition.name`.

        All places referenced by the transition's input and output arcs must be
        registered via [`add_place`][cpnx.PetriNet.add_place] before the first time the
        transition fires — referencing an undeclared name raises `KeyError` at fire time.

        Args:
            transition: The [`Transition`][cpnx.Transition] (or
                [`SubstitutionTransition`][cpnx.SubstitutionTransition]) to register.

        Raises:
            ValueError: If `transition.name` is already registered as a place name.
            TypeError: If one of the transition's arcs targets another transition's name
                instead of a place.
        """
        with self._lock:
            self._validate_new_transition(transition)
            self.transitions[transition.name] = transition
            if any(arc.settle_secs > 0.0 for arc in transition.inputs):
                self._has_timed_features = True

    def deposit(self, place_name: str, token: Token) -> None:
        """Deposit `token` into `place_name`, auto-creating a bare place if it does not exist.

        This is the primary entry point for injecting work items from external
        sources (data loaders, scheduled events, API responses, etc.). Also wakes any
        thread blocked in [`run`][cpnx.PetriNet.run] and invokes `on_token_deposited`
        (if set) outside the engine lock.

        Note:
            Auto-creation always produces a bare [`Place`][cpnx.Place].
            If you need a [`ResourcePlace`][cpnx.ResourcePlace] or
            `ThresholdPlace`, call [`add_place`][cpnx.PetriNet.add_place] first.

        Args:
            place_name: Name of the target place.
            token: The token to deposit.
        """
        with self._lock:
            if place_name not in self.places:
                self.places[place_name] = Place(place_name)
            self.places[place_name].deposit(token, model_time=self._get_model_time_under_lock())
        self._work_available.set()
        if self.on_token_deposited:
            try:
                self.on_token_deposited(place_name, token)
            except Exception:
                pass

    @staticmethod
    def _filter_highest_priority(
        pairs: list[tuple[Transition, _Binding]],
    ) -> list[tuple[Transition, _Binding]]:
        if not pairs:
            return []
        min_priority = min(t.priority for t, _ in pairs)
        return [(t, b) for t, b in pairs if t.priority == min_priority]

    def _enabled_transition_bindings(self) -> list[tuple[Transition, _Binding]]:
        """Resolve each transition's binding once, keeping those that are currently enabled.

        For every transition, checks output capacity and resolves a guard-satisfying binding
        under its effective [`BindingPolicy`][cpnx.BindingPolicy]. Returning the resolved
        binding alongside the transition lets [`step`][cpnx.PetriNet.step] consume exactly
        those tokens without re-resolving (so the guard is evaluated once per firing).
        """
        m_time = self._get_model_time_under_lock()
        result: list[tuple[Transition, _Binding]] = []
        for t in self.transitions.values():
            if not self._check_output_capacity(t):
                continue
            binding = self._resolve_binding(t, m_time)
            if binding is not None:
                result.append((t, binding))
        return result

    def _select_transition_to_fire(self) -> tuple[Transition, _Binding] | None:
        candidates = self._filter_highest_priority(self._enabled_transition_bindings())
        return self._rng.choice(candidates) if candidates else None

    def _consume_binding(self, binding: _Binding, m_time: float | None) -> tuple[list[Token], list[tuple[str, Token]]]:
        """Remove the exact tokens named by `binding` from their source places.

        Each arc's tokens were already resolved (and its guard satisfied) by
        `_resolve_binding`, so consumption is a straight `retrieve_specific` by id — no
        arc expression is re-evaluated here. If a token has vanished (e.g. a concurrent
        consumer removed it between resolution and consumption), the already-consumed
        tokens are returned to their source places before the error propagates, so none
        are silently lost.

        Args:
            binding: The resolved binding — arcs paired with the tokens they will consume.
            m_time: The current model time used to validate token availability.

        Returns:
            A tuple of `(consumed_tokens, token_sources)`, where `token_sources` pairs each
            consumed token with the name of the place it came from (for rollback).
        """
        consumed_tokens: list[Token] = []
        token_sources: list[tuple[str, Token]] = []
        try:
            for arc, tokens in binding:
                place = self.places[arc.place]
                got = place.retrieve_specific(tokens, model_time=m_time)
                consumed_tokens.extend(got)
                for t in got:
                    token_sources.append((arc.place, t))
        except Exception:
            # A token vanished mid-loop — return already-consumed tokens to their
            # source places so they are not silently lost.
            for src_name, t in token_sources:
                self._deposit_under_lock(src_name, t)
            raise
        return consumed_tokens, token_sources

    def step(self) -> bool:
        """Fire one enabled transition and return immediately, without waiting for it to finish.

        Selects the highest-priority enabled transition (ties broken at random among
        transitions sharing the lowest `priority` value), consumes its input tokens, and
        submits its action to the thread pool — all atomically under the engine lock. Returns
        before the action completes; the transition's effects are committed later, from a
        worker thread, once the action finishes.

        Returns:
            `True` if a transition was selected and its action was scheduled; `False` if no
            transition is currently enabled. A `False` return does not mean the net is
            finished — transitions may still be in flight, or may become enabled once
            in-flight transitions or cooldowns complete. Use
            [`is_quiescent`][cpnx.PetriNet.is_quiescent] to check for that.

        Raises:
            RuntimeError: If submitting to the internal thread pool fails (e.g. the pool has
                already been shut down, such as after exiting a `with` block). Any tokens
                already consumed from input places are returned before the error propagates.
        """
        try:
            with self._lock:
                selected = self._select_transition_to_fire()
                if not selected:
                    fired = False
                else:
                    # The binding was resolved during selection (guard evaluated once); consume
                    # exactly those tokens. Under BindingPolicy.FIRST the chosen tokens may not
                    # be at the head of their places, so consumption is by token id, not FIFO.
                    transition, binding = selected
                    m_time = self._get_model_time_under_lock()
                    consumed_tokens, token_sources = self._consume_binding(binding, m_time)

                    self._running_count += 1
                    try:
                        self._executor.submit(self._execute_transition, transition, consumed_tokens, token_sources)
                    except Exception:
                        # submit() failed (e.g. executor shut down) — undo the increment so
                        # is_quiescent() doesn't permanently block.
                        self._running_count -= 1
                        # Return consumed tokens back to their source places
                        for src_name, t in token_sources:
                            self._deposit_under_lock(src_name, t)
                        raise
                    fired = True
        finally:
            # Dispatch any deferred callbacks accumulated during selection, off the lock —
            # even if consume/submit raised, so buffered signals are never delayed.
            self._flush_search_exhaustions()
            self._flush_priority_key_failures()

        return fired

    def _validate_transition_arcs(self, transition_name: str, transition: Transition) -> None:
        for arc in transition.inputs + transition.outputs:
            if arc.place in self.transitions:
                raise TypeError(
                    f"Arc target '{arc.place}' in transition '{transition_name}' is a Transition, not a Place. "
                    f"Arcs must connect Places↔Transitions only."
                )
            if arc.place not in self.places:
                raise KeyError(f"Place '{arc.place}' referenced by transition '{transition_name}' is not registered.")

    def validate(self) -> None:
        """Check the net's structural topology and raise on the first problem found.

        Checks for name overlaps between places and transitions, and verifies
        that all transition arcs connect to valid, registered places rather than
        transitions. Called automatically at the start of [`run`][cpnx.PetriNet.run].

        Raises:
            ValueError: If a name is registered as both a place and a transition.
            TypeError: If a transition's arc targets another transition's name.
            KeyError: If a transition's arc references a place that has not been
                registered via [`add_place`][cpnx.PetriNet.add_place].
        """
        with self._lock:
            # Check overlap between place names and transition names
            overlaps = set(self.places.keys()) & set(self.transitions.keys())
            if overlaps:
                raise ValueError(f"Name overlap: '{list(overlaps)[0]}' is registered as both a Place and a Transition.")

            for name, transition in self.transitions.items():
                self._validate_transition_arcs(name, transition)

    def _wait_for_work(self, deadline: float | None, stop_event: threading.Event | None) -> None:
        timeout = self.cooldown_interval
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            timeout = min(remaining, timeout)

        if stop_event is not None:
            timeout = min(timeout, 0.1)

        self._work_available.wait(timeout=timeout)

    @staticmethod
    def _should_stop_run(deadline: float | None, stop_event: threading.Event | None) -> bool:
        if stop_event is not None and stop_event.is_set():
            return True
        return deadline is not None and time.monotonic() > deadline

    def run(
        self,
        deadline: float | None = None,
        *,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Repeatedly call `step` until the net is quiescent, the deadline passes, or stopped.

        Validates the net first (see [`validate`][cpnx.PetriNet.validate]), then loops:
        clear the work-available signal, try to fire a transition via
        [`step`][cpnx.PetriNet.step], and if nothing fired, sleep on an internal
        `threading.Event` (bounded by `cooldown_interval`, or by 0.1s when `stop_event` is
        given) rather than busy-waiting — the event wakes immediately when new tokens are
        deposited, a transition completes, or the clock advances. The loop checks
        `stop_event`/`deadline` before each firing attempt, so it exits promptly rather than
        only between full sleep cycles.

        Args:
            deadline: **Absolute** monotonic timestamp after which the loop exits, even if
                the net is not yet quiescent. Always construct this as
                `time.monotonic() + <seconds>`. If `None` (default), runs until the net is
                quiescent, with no time limit.
            stop_event: Optional `threading.Event`. If set (by another thread) at any point
                during the loop, `run` exits promptly, leaving any in-flight transitions
                running in the background.

        Warning:
            Passing a raw duration (e.g. `run(30)`) instead of an absolute
            deadline causes immediate exit because `time.monotonic()` is always
            much larger than small floats. Use `run(deadline=time.monotonic() + 30)`.

        Note:
            A `BindingPolicy.FIRST` transition whose only satisfiable binding lies beyond
            `binding_search_limit` counts as disabled, so `run` can return "quiescent" while
            that token still sits in its place. The exhaustion is surfaced via
            `on_binding_search_exhausted`; raise `binding_search_limit` if such tokens must be
            processed. `RANDOM`/`PRIORITY` stall the same way *when no satisfying binding lies
            within the first `limit` candidates*; if one does, they select over that truncated
            prefix and fire (still signalling the truncation).

        Note:
            Reproducibility of a seeded net (`PetriNet(seed=...)`) holds for **synchronous
            stepping** and for nets whose enablement does not depend on in-flight deposits.
            In a pipelined net with concurrent actions, whether a worker-thread deposit lands
            before or after the next `step` acquires the lock is OS-scheduled; that can change
            which transitions are enabled at a firing step and hence the RNG draw sequence.
            For strict replay use `max_workers=1`; above 1, identical seeds are **not**
            guaranteed to reproduce. Seeded streams are also not stable across cpnx versions.

        Example:
            ```python
            net.run(deadline=time.monotonic() + 30)  # run for up to 30 seconds
            net.run()  # run to quiescence
            ```
        """
        self.validate()
        while not self.is_quiescent():
            if self._should_stop_run(deadline, stop_event):
                break
            self._work_available.clear()
            if not self.step():
                self._wait_for_work(deadline, stop_event)

    def drive_to_quiescence(self, *, max_ticks: int = 1_000_000) -> DriveResult:
        """Drive the net to quiescence on its **logical clock**, jumping over timed waits.

        The deterministic counterpart to [`run`][cpnx.PetriNet.run]. Where `run` waits out
        cooldowns and settle windows on the real wall clock — so a net with an 8-second grinder
        cooldown takes 8 real seconds — this drives the net on its logical clock: fire every
        transition enabled at the current instant (awaiting each action so its outputs can enable
        the next), then, once nothing more fires, jump [`advance_time`][cpnx.PetriNet.advance_time]
        straight to the next availability boundary. Blocking/back-pressure is fully preserved (a
        cooling resource is genuinely unavailable for its full *logical* cooldown), but the waiting
        costs no wall-clock time. Because it never races real time, the marking observed after this
        returns is a true fixed point — useful for benchmarking engine CPU cost and for
        property-test oracles that must not race a wall-clock settle window.

        The first call anchors the net onto the logical clock (via `advance_time`); subsequent
        calls continue from the current logical time. The await after every `step` keeps at most one
        action in flight, which is what makes the drive single-threaded and deterministic —
        `max_workers` does not affect it. Use [`run`][cpnx.PetriNet.run] to exploit concurrency.

        Args:
            max_ticks: Safety cap on logical-clock advances, so a pathological net cannot loop
                forever.

        Returns:
            A [`DriveResult`][cpnx.DriveResult] with the number of firings and clock advances.
        """
        # Anchor onto the logical clock the first time only. `math.nextafter` (not `+ 1e-9`)
        # guarantees a strictly-greater value: at monotonic-clock magnitudes one ULP is ~2e-9, so a
        # fixed epsilon can round back to the current time — the same float hazard guarded against
        # in `_next_availability_boundary`.
        if self._model_time is None:
            self.advance_time(math.nextafter(self.model_time, math.inf))

        steps = 0
        ticks = 0
        while ticks < max_ticks:
            # Fire everything enabled at the current logical instant. The await after each firing is
            # deliberate: `step` returns as soon as the action is submitted, so awaiting means at
            # most one action is ever in flight. This keeps the drive single-threaded (determinism)
            # and ensures each completed action's outputs are committed before the next enablement
            # check (instant accounting).
            while self.step():
                steps += 1
                self._await_inflight()
            # Nothing fires right now. Done, or just waiting out a cooldown/settle window?
            if self.is_quiescent():
                break
            boundary = self._next_availability_boundary()
            if boundary is None or boundary <= self._model_time:
                break
            self.advance_time(boundary)
            ticks += 1
        return DriveResult(steps=steps, ticks=ticks)

    def is_quiescent(self) -> bool:
        """Return `True` if there is no in-flight work and nothing could become enabled soon.

        A net is quiescent when no transition is currently running (in the middle of an
        action) *and* no transition could possibly fire even once currently-cooling-down or
        not-yet-settled tokens become available. Unlike [`is_dead`][cpnx.PetriNet.is_dead],
        which is a pure snapshot of the current marking, `is_quiescent` also accounts for
        in-flight transitions and treats time-gated tokens (cooldowns, settle windows) as if
        they were already present — so the net is not considered quiescent merely because
        work is temporarily blocked by timing. This is the condition [`run`][cpnx.PetriNet.run]
        loops until.

        Returns:
            `True` if the net has no pending or in-flight work; `False` if a transition is
            running or could eventually fire (including after a cooldown or settle window
            elapses).
        """
        with self._lock:
            if self._running_count > 0:
                result = False
            else:
                result = not any(self._is_transition_potentially_enabled(t) for t in self.transitions.values())
        self._flush_search_exhaustions()
        self._flush_priority_key_failures()
        return result

    @property
    def marking(self) -> dict[str, tuple[Token, ...]]:
        """Snapshot the current marking: every place name mapped to its live tokens.

        In CPN formalism the *marking* `M` is a function from places to
        multisets of colour values. This property returns, for each registered place, a
        tuple of the [`Token`][cpnx.Token] objects currently held there (taken under the
        engine lock, so it reflects a single consistent instant).

        Returns:
            Dict mapping place name to a tuple of tokens currently in that place.
        """
        with self._lock:
            return {name: place.tokens for name, place in self.places.items()}

    def is_dead(self) -> bool:
        """Return `True` if the current marking enables no transition right now (CPN dead state).

        In CPN theory a *dead marking* is one in which no transition can fire
        given the current token distribution. This checks each transition's full enabling
        condition — token availability, cooldowns, settle windows, output capacity, and the
        guard — exactly as of this instant. Unlike [`is_quiescent`][cpnx.PetriNet.is_quiescent],
        it does **not** account for in-flight transitions, nor does it treat cooling-down or
        not-yet-settled tokens as available; a net can be dead right now yet become enabled
        moments later once a cooldown expires or an in-flight transition deposits a token.

        Returns:
            `True` if every transition's enabling condition currently fails.
        """
        with self._lock:
            result = not any(self._is_transition_enabled(t) for t in self.transitions.values())
        self._flush_search_exhaustions()
        self._flush_priority_key_failures()
        return result

    def snapshot(self) -> dict:
        """Return a JSON-serialisable snapshot of current place markings and running count.

        Delegates to the module-level [`snapshot`][cpnx.snapshot] function.

        Returns:
            Dict with `"places"` (mapping place name to a list of token dicts with
            keys `id`, `payload`, `created_at`, `is_resource`) and
            `"running_count"` (number of transitions currently executing).

        Example:
            ```python
            import json
            print(json.dumps(net.snapshot(), indent=2))
            ```
        """
        return snapshot(self)

    def to_dot(self) -> str:
        """Render the net's places, transitions, and arcs as a Graphviz DOT string.

        Delegates to the module-level [`to_dot`][cpnx.to_dot] function.

        Place nodes are circles annotated with current token counts.
        Transition nodes are boxes. Arc labels include `count`,
        `consume_all`, and `settle_secs` where non-default.

        Returns:
            A DOT language string. Render with Graphviz or paste into
            https://dreampuf.github.io/GraphvizOnline/.
        """
        return to_dot(self)

    # ------------------------------------------------------------------
    # Context manager — preferred over __del__ for deterministic shutdown
    # ------------------------------------------------------------------

    def __enter__(self) -> "PetriNet":
        return self

    def __exit__(self, *_: object) -> None:
        """Shut down the thread pool, waiting for in-flight transitions to finish."""
        self._executor.shutdown(wait=True)
        # Zombie action threads may still be running after a timeout — don't wait.
        self._action_executor.shutdown(wait=False, cancel_futures=True)
        self._expr_executor.shutdown(wait=True)

    def __del__(self) -> None:
        try:
            self._executor.shutdown(wait=False)
        except Exception:
            pass
        try:
            self._action_executor.shutdown(wait=False)
        except Exception:
            pass
        try:
            self._expr_executor.shutdown(wait=False)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _deposit_under_lock(self, place_name: str, token: Token) -> None:
        """Deposit `token` into an already-registered place (caller holds `self._lock`).

        Unlike the public `deposit`, this method does NOT auto-create missing
        places — it raises `KeyError` so typos in arc names are caught loudly
        rather than silently creating a bare `Place` with the
        wrong type. Callbacks are NOT fired here; callers collect deposits and fire
        them after releasing the lock.

        Args:
            place_name: Name of an already-registered place.
            token: The token to deposit.

        Raises:
            KeyError: If `place_name` has not been registered with `add_place`.
        """
        if place_name not in self.places:
            raise KeyError(
                f"Place '{place_name}' is not registered. Call add_place() before referencing it in a Transition arc."
            )
        self.places[place_name].deposit(token, model_time=self._get_model_time_under_lock())

    def _is_transition_enabled(self, transition: Transition) -> bool:
        """Return `True` if all preconditions for firing `transition` are satisfied right now.

        Checks output-place capacity (back-pressure), then resolves a binding that
        satisfies token availability (including cooldowns, thresholds, and settle windows)
        and the guard, honoring the transition's effective
        [`BindingPolicy`][cpnx.BindingPolicy]. Called while holding `self._lock`.

        Args:
            transition: The transition to test.

        Returns:
            `True` if a guard-satisfying binding exists and every unguarded output arc has
            capacity; `False` otherwise.
        """
        if not self._check_output_capacity(transition):
            return False
        m_time = self._get_model_time_under_lock()
        return self._binding_exists(transition, m_time)

    def _effective_policy(self, transition: Transition) -> BindingPolicy:
        """Return the binding policy in force for `transition` (its own, else the net default)."""
        return self.binding_policy if transition.binding_policy is None else transition.binding_policy

    def _arc_available(
        self, arc: InputArc, place: Place | None, m_time: float | None, ignore_timing: bool
    ) -> list[Token] | None:
        """Return the tokens eligible to satisfy one input `arc`, or `None` if it cannot be met.

        Eligibility accounts for token count and — unless `ignore_timing` is set — cooldowns
        and the arc's settle window. Returns `None` when the place is missing, holds fewer
        than `arc.count` eligible tokens, or has not yet settled.

        Args:
            arc: The input arc whose source place is inspected.
            place: The source place, or `None` if unregistered.
            m_time: The current model time (used when `ignore_timing` is `False`).
            ignore_timing: When `True`, treat cooling-down/not-yet-settled tokens as available
                (used by [`is_quiescent`][cpnx.PetriNet.is_quiescent]).

        Returns:
            The eligible tokens in FIFO order, or `None` if the arc cannot be satisfied.
        """
        if place is None:
            return None
        t_limit = float("inf") if ignore_timing else m_time
        if not place.can_retrieve(arc.count, model_time=t_limit):
            return None
        if not ignore_timing and not self._is_settle_time_met(place, arc):
            return None
        return place.peek(len(place), model_time=t_limit)

    def _gather_arc_pools(
        self, transition: Transition, m_time: float | None, ignore_timing: bool
    ) -> list[tuple[InputArc, list[Token]]] | None:
        """Collect the eligible-token pool for every input arc, or `None` if any is unmet."""
        pools: list[tuple[InputArc, list[Token]]] = []
        for arc in transition.inputs:
            available = self._arc_available(arc, self.places.get(arc.place), m_time, ignore_timing)
            if available is None:
                return None
            pools.append((arc, available))
        return pools

    def _is_head_only(self, transition: Transition, policy: BindingPolicy) -> bool:
        """Whether `transition` resolves via the O(1) head binding rather than a search.

        `LEGACY` always uses the head. `FIRST` with no guard reduces to the head (the first
        candidate is the head and trivially satisfies). `RANDOM`/`PRIORITY` must always
        enumerate — even guard-free, they select *among* eligible groups, not just the head.
        """
        if policy is BindingPolicy.LEGACY:
            return True
        return policy is BindingPolicy.FIRST and transition.guard is None

    def _resolve_binding(
        self, transition: Transition, m_time: float | None, *, ignore_timing: bool = False
    ) -> _Binding | None:
        """Resolve the concrete binding `transition` will fire with, or `None` if not enabled.

        Gathers each input arc's eligible tokens, then selects a guard-satisfying binding
        according to the transition's effective [`BindingPolicy`][cpnx.BindingPolicy]:

        - `LEGACY` (and guard-free `FIRST`): take the first `count` tokens of each arc (its
          head, or the leading tokens of the arc-expression ordering) and accept it only if
          the guard holds — the historical behavior, O(1).
        - `FIRST`: return the first guard-satisfying combination in insertion order.
        - `RANDOM`: return a uniformly-random guard-satisfying combination (drawing from the
          net's seeded RNG).
        - `PRIORITY`: return the guard-satisfying combination minimizing
          `binding_priority_key`.

        All search policies are bounded by `binding_search_limit`. This is the **firing**
        path — it may consume RNG state — so probes must use
        `_binding_exists` instead.

        Args:
            transition: The transition to resolve.
            m_time: The current model time used for availability/settle checks.
            ignore_timing: When `True`, ignore cooldowns and settle windows (used by
                [`is_quiescent`][cpnx.PetriNet.is_quiescent]).

        Returns:
            The resolved binding (each arc paired with the tokens it will consume), or `None`
            if no guard-satisfying binding exists within the search limit.
        """
        pools = self._gather_arc_pools(transition, m_time, ignore_timing)
        if pools is None:
            return None
        policy = self._effective_policy(transition)
        if self._is_head_only(transition, policy):
            return self._head_binding(transition, pools)
        return self._select_binding(transition, pools, policy)

    def _binding_exists(self, transition: Transition, m_time: float | None, *, ignore_timing: bool = False) -> bool:
        """Return whether *any* guard-satisfying binding exists — a probe that never draws RNG.

        Existence is policy-independent, so this short-circuits at the first satisfying binding
        regardless of policy. Used by the enabling/quiescence predicates
        ([`is_dead`][cpnx.PetriNet.is_dead], [`is_quiescent`][cpnx.PetriNet.is_quiescent]),
        which must not perturb the seeded RNG — otherwise a timing-dependent number of probe
        calls would make `RANDOM` runs non-reproducible.
        """
        pools = self._gather_arc_pools(transition, m_time, ignore_timing)
        if pools is None:
            return False
        if self._is_head_only(transition, self._effective_policy(transition)):
            return self._head_binding(transition, pools) is not None
        return next(self._iter_satisfying_bindings(transition, pools), None) is not None

    def _head_binding(self, transition: Transition, pools: list[tuple[InputArc, list[Token]]]) -> _Binding | None:
        """Build the head binding (first `count` per arc) and accept it only if the guard holds."""
        binding: _Binding = []
        for arc, available in pools:
            tokens = self._resolve_input_tokens(arc, available)
            if tokens is None:
                return None
            binding.append((arc, tokens))
        if self._check_transition_guard(transition, _flatten_binding(binding)):
            return binding
        return None

    def _iter_satisfying_bindings(
        self, transition: Transition, pools: list[tuple[InputArc, list[Token]]]
    ) -> Iterator[tuple[_Binding, list[Token]]]:
        """Yield each guard-satisfying `(binding, flat_tokens)` in insertion order, bounded by the limit.

        Enumerates candidate bindings deterministically and yields those whose guard holds,
        paired with the flattened token list already computed for the guard check (so
        `PRIORITY`'s key does not re-flatten). Stops after `binding_search_limit` candidates
        have been examined, signalling exhaustion via `on_binding_search_exhausted` (deferred
        until the lock releases). This is the shared engine for every search policy: `FIRST`
        takes the first item, `RANDOM` samples, and `PRIORITY` takes the min-key item.
        """
        for examined, binding in enumerate(self._iter_candidate_bindings(pools), start=1):
            if examined > self.binding_search_limit:
                self._signal_search_exhausted(transition.name)
                return
            flat = _flatten_binding(binding)
            if self._check_transition_guard(transition, flat):
                yield binding, flat

    def _select_binding(
        self, transition: Transition, pools: list[tuple[InputArc, list[Token]]], policy: BindingPolicy
    ) -> _Binding | None:
        """Pick one satisfying binding under a search policy (`FIRST`/`RANDOM`/`PRIORITY`)."""
        gen = self._iter_satisfying_bindings(transition, pools)
        if policy is BindingPolicy.RANDOM:
            return self._reservoir_pick(gen)
        if policy is BindingPolicy.PRIORITY:
            return self._min_key_pick(gen, transition)
        pair = next(gen, None)
        return pair[0] if pair is not None else None

    def _reservoir_pick(self, bindings: Iterator[tuple[_Binding, list[Token]]]) -> _Binding | None:
        """Uniformly sample one binding from `bindings` in a single pass (reservoir, size 1).

        Draws from the net's seeded RNG, so a seeded net reproduces the choice. Returns `None`
        if the iterator is empty.
        """
        chosen: _Binding | None = None
        for seen, (binding, _flat) in enumerate(bindings, start=1):
            if self._rng.random() < 1.0 / seen:
                chosen = binding
        return chosen

    def _min_key_pick(
        self, bindings: Iterator[tuple[_Binding, list[Token]]], transition: Transition
    ) -> _Binding | None:
        """Return the binding minimizing `binding_priority_key` (default: oldest `created_at`).

        Ties are broken by insertion order (the first-encountered minimum wins), so the choice
        is deterministic. Both the key evaluation *and* the comparison are guarded: a candidate
        whose key raises, or whose key is not comparable with the running best, is skipped
        rather than aborting the enabling check (mirroring how a raising guard is treated as
        `False`). If **every** candidate is skipped this way but satisfying bindings exist, the
        first satisfying binding (insertion order) is returned — never `None` while bindings
        exist — so this firing path stays consistent with the RNG-free
        `_binding_exists` probe (which does not evaluate the
        key). That silent fallback is surfaced via `on_error` (deferred, de-duplicated) so a
        wholly-broken key does not vanish without a trace. Returns `None` only if the iterator
        is empty.
        """
        key_fn = _default_priority_key if transition.binding_priority_key is None else transition.binding_priority_key
        best, first, first_exc = self._reduce_min_key(bindings, key_fn)
        if best is None and first is not None and first_exc is not None:
            # Every satisfying candidate's key raised/was incomparable: we still fire the
            # insertion-order fallback, but PRIORITY was effectively ignored — signal it.
            self._signal_priority_key_failure(transition.name, first_exc)
        return best if best is not None else first

    @staticmethod
    def _reduce_min_key(
        bindings: Iterator[tuple[_Binding, list[Token]]], key_fn: Callable[[list[Token]], object]
    ) -> tuple[_Binding | None, _Binding | None, Exception | None]:
        """Scan `bindings`, returning `(best, first, first_exc)` for the min-key selection.

        `best` is the minimum-key binding (ties → first-encountered), or `None` if every key
        evaluation/comparison raised. `first` is the first satisfying binding (insertion order),
        used as the fallback. `first_exc` is the first exception a key raised, if any.
        """
        first: _Binding | None = None
        best: _Binding | None = None
        best_key: object = None
        first_exc: Exception | None = None
        for binding, flat in bindings:
            if first is None:
                first = binding
            try:
                candidate_key = key_fn(flat)
                if best is None or candidate_key < best_key:  # type: ignore[operator]
                    best, best_key = binding, candidate_key
            except Exception as exc:
                if first_exc is None:
                    first_exc = exc
        return best, first, first_exc

    def _iter_candidate_bindings(self, pools: list[tuple[InputArc, list[Token]]]) -> Iterator[_Binding]:
        """Yield candidate bindings as the Cartesian product of each arc's token-group options.

        Options are generated in insertion order, so the first binding yielded is the head
        selection and iteration proceeds deterministically from there.

        Each per-arc option stream is truncated to `binding_search_limit + 1` groups before
        being handed to `itertools.product`. This keeps both time and memory bounded by the
        search limit: `product` materializes each input iterable in full before yielding, so
        an un-truncated arc with `C(n, count)` combinations would be built eagerly (defeating
        the bound and risking OOM). The truncation is loss-free within the limit — in
        `product`'s lexicographic output order a candidate's overall rank is at least its
        index in any single dimension, so any group beyond index `binding_search_limit` can
        never appear among the first `binding_search_limit` bindings the search inspects.
        """
        arcs = [arc for arc, _ in pools]
        cap = self.binding_search_limit + 1
        option_lists = [list(itertools.islice(self._arc_options(arc, available), cap)) for arc, available in pools]
        for combo in itertools.product(*option_lists):
            yield list(zip(arcs, combo, strict=True))

    def _arc_options(self, arc: InputArc, available: list[Token]) -> Iterator[list[Token]]:
        """Yield each `count`-sized token group one input `arc` could consume, in order.

        A `consume_all` arc has a single option (every eligible token). Otherwise the tokens
        are ordered (by the arc expression, else FIFO) and every `count`-sized combination is
        yielded in index order — so the first yielded group is the arc's head selection. An
        arc whose expression raises, or that has fewer than `count` eligible tokens, yields
        nothing (making the transition unbindable).
        """
        if arc.consume_all:
            yield available
            return
        ordered = self._order_available(arc, available)
        if ordered is None or len(ordered) < arc.count:
            return
        if arc.count == 1:
            # Fast path for the common single-token arc. `combinations(ordered, 1)` yields one
            # 1-tuple per token which is then rebuilt as a list; emitting `[token]` directly
            # produces the identical groups in the identical order (so seeded `RANDOM` streams
            # are unaffected) while skipping the combinations machinery in the hottest loop of
            # the binding search.
            for token in ordered:
                yield [token]
            return
        for combo in itertools.combinations(ordered, arc.count):
            yield list(combo)

    def _order_available(self, arc: InputArc, available: list[Token]) -> list[Token] | None:
        """Return `available` ordered by the arc's selection expression, or `None` if it raises.

        With no expression, returns `available` unchanged (FIFO). A string expression is
        evaluated via the sandboxed, precompiled path; a callable via the timed expression
        pool.
        """
        if arc.expression is None:
            return available
        try:
            return list(self._eval_expression(arc.expression, arc._compiled_expression, available))  # type: ignore[attr-defined]
        except Exception:
            return None

    def _signal_search_exhausted(self, transition_name: str) -> None:
        """Record a search exhaustion for `transition_name` to dispatch after the lock releases.

        Called from inside the enabling check (under the engine lock). The callback itself
        must run *outside* the lock (so it may safely call back into the net), so this only
        buffers the transition name; `_flush_search_exhaustions` drains the buffer and fires
        the callback once the caller has released the lock.
        """
        self._pending_exhaustions.add(transition_name)

    def _flush_search_exhaustions(self) -> None:
        """Dispatch any buffered search-exhaustion callbacks. Must be called off the lock.

        Swaps out the pending set under a brief lock, then fires `on_binding_search_exhausted`
        once per distinct transition, swallowing callback errors. Safe to call unconditionally;
        a no-op when nothing exhausted.
        """
        with self._lock:
            if not self._pending_exhaustions:
                return
            pending = self._pending_exhaustions
            self._pending_exhaustions = set()
        callback = self.on_binding_search_exhausted
        if callback is None:
            return
        for name in pending:
            try:
                callback(name)
            except Exception:
                pass

    def _signal_priority_key_failure(self, transition_name: str, first_exc: Exception) -> None:
        """Buffer a "PRIORITY key failed for every candidate" event for off-lock `on_error` dispatch.

        Called under the engine lock from `_min_key_pick`; keeps only the first exception per
        transition (`on_error` must run off the lock, so this only buffers). See
        `_flush_priority_key_failures`.
        """
        self._pending_key_failures.setdefault(transition_name, first_exc)

    def _flush_priority_key_failures(self) -> None:
        """Dispatch buffered PRIORITY-key failures via `on_error`. Must be called off the lock.

        Swaps out the pending map under a brief lock, then fires `on_error(name, exc, None)` once
        per distinct transition with a descriptive `RuntimeError` (chaining the first key error),
        swallowing callback errors. Safe to call unconditionally; a no-op when nothing failed.
        """
        with self._lock:
            if not self._pending_key_failures:
                return
            pending = self._pending_key_failures
            self._pending_key_failures = {}
        callback = self.on_error
        if callback is None:
            return
        for name, first_exc in pending.items():
            err = RuntimeError(
                f"binding_priority_key raised for every candidate binding of transition '{name}'; "
                f"PRIORITY selection fell back to insertion order. First error: {first_exc!r}"
            )
            err.__cause__ = first_exc
            try:
                callback(name, err, None)
            except Exception:
                pass

    def _is_settle_time_met(self, place: Place, arc: InputArc) -> bool:
        if arc.settle_secs <= 0.0:
            return True
        if self._model_time is not None:
            elapsed = self._model_time - place.last_deposit_time_model
        else:
            elapsed = time.monotonic() - place.last_deposit_time
        return elapsed >= arc.settle_secs

    def _eval_expression(self, expression, compiled, tokens: list[Token]):
        """Evaluate a string (precompiled, sandboxed) or callable `expression` over `tokens`.

        Centralizes the string-vs-callable dispatch shared by input-arc selection
        (`_resolve_input_tokens`), output-arc guards (`_is_arc_active`),
        and transition guards (`_check_transition_guard`). Callers are
        responsible for coercing/bounding the result and for exception handling.
        """
        if isinstance(expression, str):
            return SandboxEvaluator.evaluate_compiled(compiled, {"tokens": tokens})
        return self._call_expr(expression, tokens, timeout=self.expr_timeout_secs)

    def _resolve_input_tokens(self, arc: InputArc, available: list[Token]) -> list[Token] | None:
        """Resolve which tokens input `arc` would consume from `available`.

        Returns the selected tokens, or `None` if the arc cannot be satisfied —
        either its selection expression raised, or fewer than `arc.count` tokens
        are eligible. In CPN semantics arc multiplicity is all-or-nothing: an arc
        demanding `count` tokens is not enabled unless at least `count` are
        resolved, so a selection that yields fewer (or none) disables the
        transition rather than firing with a short or zero-length token list.

        Used by `_head_binding` to build the head selection whenever a transition
        resolves head-only — under `BindingPolicy.LEGACY`, or guard-free
        `BindingPolicy.FIRST` (see `_is_head_only`).
        Guard-free `RANDOM`/`PRIORITY` do **not** use this path; they enumerate. The
        multiplicity rule applies either way, so a short selection disables the transition
        rather than firing with too few tokens.
        """
        if arc.consume_all:
            tokens = available
        elif arc.expression is not None:
            try:
                ordered = self._eval_expression(arc.expression, arc._compiled_expression, available)  # type: ignore[attr-defined]
                tokens = ordered[: arc.count]
            except Exception:
                return None
        else:
            tokens = available[: arc.count]
        if len(tokens) < arc.count:
            return None
        return tokens

    def _check_output_capacity(self, transition: Transition) -> bool:
        # Back-pressure: refuse to fire if an unguarded output arc's target place is full.
        # Guarded arcs are skipped — their target may never receive a token, so checking
        # capacity speculatively would cause spurious blocking.
        for arc in transition.outputs:
            if arc.expression is not None:
                continue
            place = self.places.get(arc.place)
            if place is not None and not place.can_deposit(arc.count):
                return False
        return True

    def _check_transition_guard(self, transition: Transition, candidate_tokens: list[Token]) -> bool:
        if transition.guard is None:
            return True
        try:
            return bool(self._eval_expression(transition.guard, transition._compiled_guard, candidate_tokens))  # type: ignore[attr-defined]
        except Exception:
            return False

    def _is_transition_potentially_enabled(self, transition: Transition) -> bool:
        """Return `True` if `transition` could fire given current token counts, ignoring timing.

        Unlike `_is_transition_enabled`, this ignores cooldown timers on
        `PacedResourcePlace` (tokens in cooldown are counted as present), settle windows, and
        output-place back-pressure. It probes for a guard-satisfying binding under the
        transition's effective [`BindingPolicy`][cpnx.BindingPolicy] via
        `_binding_exists` (never drawing the seeded RNG, so
        quiescence polling cannot perturb `RANDOM` reproducibility). Used by
        [`is_quiescent`][cpnx.PetriNet.is_quiescent] to distinguish "no work possible" from
        "work temporarily blocked by timing".

        Args:
            transition: The transition to test.

        Returns:
            `True` if a guard-satisfying binding exists once timing is ignored.
        """
        return self._binding_exists(transition, float("inf"), ignore_timing=True)

    def _next_availability_boundary(self) -> float | None:
        """Smallest future logical time at which a currently-blocked token/window becomes available.

        Scans token cooldowns (`available_at`, set by `PacedResourcePlace` and retries) and
        input-arc settle windows. Returns `None` if nothing is time-gated (a genuine deadlock or
        completion), so [`drive_to_quiescence`][cpnx.PetriNet.drive_to_quiescence] stops instead of
        advancing the clock forever. Called only between firings, after `_await_inflight`, so no
        worker thread is mutating the marking.
        """
        now = self.model_time
        best: float | None = None
        for place in self.places.values():
            if isinstance(place, SinkPlace):
                continue
            for tok in place.tokens:
                if tok.available_at > now and (best is None or tok.available_at < best):
                    best = tok.available_at
        for transition in self.transitions.values():
            for arc in transition.inputs:
                if arc.settle_secs <= 0:
                    continue
                place = self.places.get(arc.place)
                if place is None or isinstance(place, SinkPlace) or len(place) == 0:
                    continue
                last_deposit = getattr(place, "last_deposit_time_model", 0.0)
                if now - last_deposit >= arc.settle_secs:
                    continue  # window already satisfied; nothing to wait for
                # The window is still pending, so its boundary is mathematically in the future — but
                # `last_deposit + settle_secs` can *round* to exactly `now` in float64 (one ULP at
                # monotonic-clock magnitudes is ~2e-9). Stepping to the next representable float
                # guarantees forward progress rather than stranding with the window unmet.
                boundary = max(last_deposit + arc.settle_secs, math.nextafter(now, math.inf))
                if boundary > now and (best is None or boundary < best):
                    best = boundary
        return best

    def _await_inflight(self, *, max_spins: int = 10_000_000) -> None:
        """Block until no transition action is mid-flight (barrier for `drive_to_quiescence`).

        Spins on `_running_count` with `time.sleep(0)` (yields the GIL to worker threads without a
        real sleep). Safe as a barrier: `_execute_transition` decrements `_running_count` *after*
        committing its output deposits, so observing zero implies every completed action's outputs
        are visible.

        Args:
            max_spins: Safety cap. Actions are expected to complete promptly; a hung action would
                otherwise spin forever, so exceeding the cap raises rather than hangs silently.

        Raises:
            RuntimeError: If `max_spins` is exceeded while an action is still in flight.
        """
        spins = 0
        while self._running_count > 0:
            if spins >= max_spins:
                raise RuntimeError(
                    f"_await_inflight exceeded max_spins={max_spins} with "
                    f"{self._running_count} action(s) still in flight — hung action?"
                )
            spins += 1
            time.sleep(0)

    def _commit_or_rollback_transition(
        self,
        transition: Transition,
        success: bool,
        consumed_tokens: list[Token],
        output_tokens: list[Token],
        token_sources: list[tuple[str, Token]],
        error: BaseException | None,
    ) -> tuple[bool, BaseException | None, list[Token], list[Token], list[tuple[str, Token]]]:
        data_tokens: list[Token] = []
        dl_data: list[Token] = []
        deposited: list[tuple[str, Token]] = []

        if success:
            try:
                success, error, data_tokens, dl_data, deposited = self._try_commit_transition(
                    transition, consumed_tokens, output_tokens, token_sources
                )
            except BaseException as exc:
                success = False
                error = exc

        if not success:
            deposited, dl_data, data_tokens = _rollback_failed_transition(
                transition,
                token_sources,
                deposit=self._deposit_under_lock,
                retry_delay=self.retry_delay,
                error_place=self.error_place,
                ref_time=self._get_model_time_under_lock(),
            )
        return success, error, data_tokens, dl_data, deposited

    def _execute_transition(
        self,
        transition: Transition,
        consumed_tokens: list[Token],
        token_sources: list[tuple[str, Token]],
    ) -> None:
        start_time = time.monotonic()
        try:
            success, output_tokens, error = self._execute_transition_action(transition, consumed_tokens, token_sources)
            with self._lock:
                duration = time.monotonic() - start_time
                success, error, data_tokens, dl_data, deposited = self._commit_or_rollback_transition(
                    transition, success, consumed_tokens, output_tokens, token_sources, error
                )

            # --- OUTSIDE THE LOCK ---
            self._invoke_transition_callbacks(transition, success, duration, error, data_tokens, dl_data, deposited)

            if error is not None and not isinstance(error, Exception):
                raise error

        finally:
            # Decrement running count and signal work available under lock
            with self._lock:
                self._running_count -= 1
            self._work_available.set()

    def _try_commit_transition(
        self,
        transition: Transition,
        consumed_tokens: list[Token],
        output_tokens: list[Token],
        token_sources: list[tuple[str, Token]],
    ) -> tuple[bool, BaseException | None, list[Token], list[Token], list[tuple[str, Token]]]:
        res_deque: deque[Token] = deque(t for t in consumed_tokens if t.is_resource)
        out_deque: deque[Token] = deque(t for t in output_tokens if not t.is_resource)
        active_outputs = self._evaluate_output_guards(transition, list(out_deque))

        planned_deposits, plan_error = self._plan_and_validate_deposits(
            transition, active_outputs, res_deque, out_deque
        )

        if plan_error is not None:
            return False, plan_error, [], [], []

        deposited = _enact_planned_deposits(
            planned_deposits,
            active_outputs,
            res_deque,
            out_deque,
            deposit=self._deposit_under_lock,
        )
        deposited.extend(
            _return_leftover_resources(
                res_deque,
                token_sources,
                deposit=self._deposit_under_lock,
            )
        )
        return True, None, [], [], deposited

    def _execute_transition_action(
        self,
        transition: Transition,
        consumed_tokens: list[Token],
        token_sources: list[tuple[str, Token]],
    ) -> tuple[bool, list[Token], BaseException | None]:
        success = False
        output_tokens: list[Token] = []
        error: BaseException | None = None
        try:
            if isinstance(transition, SubstitutionTransition):
                output_tokens = self._execute_substitution_transition(transition, consumed_tokens, token_sources)
            elif transition.action_timeout_secs is None:
                output_tokens = transition.action(consumed_tokens)
            else:
                fut = self._action_executor.submit(transition.action, consumed_tokens)
                try:
                    output_tokens = fut.result(timeout=transition.action_timeout_secs)
                except concurrent.futures.TimeoutError:
                    raise RuntimeError(
                        f"Transition '{transition.name}' action exceeded "
                        f"{transition.action_timeout_secs}s timeout — tokens rolled back. "
                        f"The action thread is still running in the background; "
                        f"use native I/O timeouts inside your action to prevent zombie accumulation."
                    ) from None
            success = True
        except BaseException as exc:
            error = exc
        return success, output_tokens, error

    def _is_arc_active(self, arc: OutputArc, output_tokens_data: list[Token]) -> bool:
        if arc.expression is None:
            return True
        return bool(self._eval_expression(arc.expression, arc._compiled_expression, output_tokens_data))  # type: ignore[attr-defined]

    def _evaluate_output_guards(
        self, transition: Transition, output_tokens_data: list[Token]
    ) -> list[tuple[OutputArc, bool]]:
        active_outputs: list[tuple[OutputArc, bool]] = []
        for arc in transition.outputs:
            is_res = isinstance(self.places.get(arc.place), (ResourcePlace, PacedResourcePlace))
            if self._is_arc_active(arc, output_tokens_data):
                active_outputs.append((arc, is_res))
        return active_outputs

    @staticmethod
    def _verify_token_demand(
        transition_name: str,
        active_outputs: list[tuple[OutputArc, bool]],
        res_count: int,
        out_count: int,
    ) -> ValueError | None:
        resource_demand = sum(arc.count for arc, is_res in active_outputs if is_res)
        data_demand = sum(arc.count for arc, is_res in active_outputs if not is_res)

        if res_count < resource_demand:
            return ValueError(
                f"Transition '{transition_name}': active resource output arcs require "
                f"{resource_demand} resource token(s) but only {res_count} were consumed. "
                f"Ensure each ResourcePlace/PacedResourcePlace InputArc has a matching OutputArc."
            )
        if out_count < data_demand:
            return ValueError(
                f"Transition '{transition_name}': action returned {out_count} non-resource "
                f"token(s) but active non-resource output arcs require {data_demand}. "
                f"Ensure your action returns at least as many tokens as the sum of "
                f"non-resource OutputArc counts (after arc guard evaluation)."
            )
        return None

    @staticmethod
    def _build_deposit_plan(
        active_outputs: list[tuple[OutputArc, bool]],
        res_deque: deque[Token],
        out_deque: deque[Token],
    ) -> list[tuple[str, Token]]:
        planned_deposits: list[tuple[str, Token]] = []
        res_temp = deque(res_deque)
        out_temp = deque(out_deque)
        for arc, is_res_place in active_outputs:
            for _ in range(arc.count):
                t = res_temp.popleft() if is_res_place else out_temp.popleft()
                planned_deposits.append((arc.place, t))
        return planned_deposits

    @staticmethod
    def _get_deposit_counts(planned_deposits: list[tuple[str, Token]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for place_name, _ in planned_deposits:
            counts[place_name] = counts.get(place_name, 0) + 1
        return counts

    def _check_token_acceptance(self, planned_deposits: list[tuple[str, Token]]) -> Exception | None:
        for place_name, token in planned_deposits:
            place = self.places.get(place_name)
            if place is None:
                return KeyError(f"Place '{place_name}' is not registered.")
            if not place.can_accept(token):
                return TypeError(f"Place '{place_name}' cannot accept token with color '{token.color}'.")
        return None

    def _check_capacity_bounds(self, counts: dict[str, int]) -> Exception | None:
        for place_name, count in counts.items():
            place = self.places.get(place_name)
            if place is not None and not place.can_deposit(count):
                return ValueError(f"Place '{place_name}' would exceed its bound of {place.bound}.")
        return None

    def _verify_deposit_constraints(
        self,
        planned_deposits: list[tuple[str, Token]],
    ) -> Exception | None:
        err = self._check_token_acceptance(planned_deposits)
        if err:
            return err
        return self._check_capacity_bounds(self._get_deposit_counts(planned_deposits))

    def _plan_and_validate_deposits(
        self,
        transition: Transition,
        active_outputs: list[tuple[OutputArc, bool]],
        res_deque: deque[Token],
        out_deque: deque[Token],
    ) -> tuple[list[tuple[str, Token]], Exception | None]:
        demand_err = self._verify_token_demand(transition.name, active_outputs, len(res_deque), len(out_deque))
        if demand_err:
            return [], demand_err

        planned_deposits = self._build_deposit_plan(active_outputs, res_deque, out_deque)

        constraint_err = self._verify_deposit_constraints(planned_deposits)
        if constraint_err:
            return [], constraint_err

        return planned_deposits, None

    def _dispatch_transition_fired(self, transition_name: str, duration: float) -> None:
        if self.on_transition_fired:
            try:
                self.on_transition_fired(transition_name, duration)
            except Exception:
                pass

    def _dispatch_transition_error(
        self, transition_name: str, error: BaseException | None, data_tokens: list[Token]
    ) -> None:
        # We explicitly check for Exception (ignoring BaseException like SystemExit/KeyboardInterrupt)
        # because on_error is meant for business logic/execution errors. Fatal process signals
        # should bubble up without triggering user-defined monitoring hooks.
        if not (self.on_error and isinstance(error, Exception)):
            return
        # When the firing consumed no data tokens, still notify once with None.
        dispatch_tokens: list[Token | None] = list(data_tokens) if data_tokens else [None]
        for dt in dispatch_tokens:
            try:
                self.on_error(transition_name, error, dt)
            except Exception:
                pass

    def _dispatch_dead_letters(self, transition_name: str, dead_lettered_data_tokens: list[Token]) -> None:
        if self.on_token_dead_lettered and dead_lettered_data_tokens:
            for dt in dead_lettered_data_tokens:
                try:
                    self.on_token_dead_lettered(transition_name, dt)
                except Exception:
                    pass

    def _dispatch_deposits(self, deposited: list[tuple[str, Token]]) -> None:
        if self.on_token_deposited:
            for pname, tok in deposited:
                try:
                    self.on_token_deposited(pname, tok)
                except Exception:
                    pass

    def _invoke_transition_callbacks(
        self,
        transition: Transition,
        success: bool,
        duration: float,
        error: BaseException | None,
        data_tokens: list[Token],
        dead_lettered_data_tokens: list[Token],
        deposited: list[tuple[str, Token]],
    ) -> None:
        if success:
            self._dispatch_transition_fired(transition.name, duration)
        else:
            self._dispatch_transition_error(transition.name, error, data_tokens)
            self._dispatch_dead_letters(transition.name, dead_lettered_data_tokens)

        self._dispatch_deposits(deposited)

    @staticmethod
    def _map_sockets_to_ports(port_socket_map: dict[str, str]) -> dict[str, list[str]]:
        socket_to_ports: dict[str, list[str]] = {}
        for port, socket in port_socket_map.items():
            socket_to_ports.setdefault(socket, []).append(port)
        return socket_to_ports

    @staticmethod
    def _verify_port_socket_boundaries(
        token_sources: list[tuple[str, Token]], socket_to_ports: dict[str, list[str]]
    ) -> None:
        for socket_name, _ in token_sources:
            if socket_name not in socket_to_ports:
                raise ValueError(
                    f"Port/Socket Boundary Violation: Parent place '{socket_name}' "
                    f"is not mapped to any port, but tokens were consumed from it."
                )

    @staticmethod
    def _deposit_into_subnet(
        subnet: "PetriNet", token_sources: list[tuple[str, Token]], socket_to_ports: dict[str, list[str]]
    ) -> None:
        for socket_name, token in token_sources:
            for port_name in socket_to_ports[socket_name]:
                subnet.deposit(port_name, token.evolve())

    def _sync_subnet_time(self, subnet: "PetriNet") -> None:
        with self._lock:
            current_model_time = self._model_time
        if current_model_time is not None:
            subnet.advance_time(current_model_time)

    def _retrieve_subnet_outputs(
        self, subnet: "PetriNet", port_socket_map: dict[str, str], parent_outputs: list[str]
    ) -> list[Token]:
        output_tokens = []
        for port, socket in port_socket_map.items():
            if socket in parent_outputs:
                port_place = subnet.places.get(port)
                if port_place:
                    # Retrieve all available tokens
                    tokens = port_place.retrieve_all(model_time=subnet.model_time)
                    output_tokens.extend(tokens)
        return output_tokens

    def _execute_substitution_transition(
        self,
        transition: SubstitutionTransition,
        consumed_tokens: list[Token],
        token_sources: list[tuple[str, Token]],
    ) -> list[Token]:
        """Run a hierarchical sub-net and return its output tokens, isolated from parent context."""
        subnet = transition.subnet

        socket_to_ports = self._map_sockets_to_ports(transition.port_socket_map)
        self._verify_port_socket_boundaries(token_sources, socket_to_ports)
        self._deposit_into_subnet(subnet, token_sources, socket_to_ports)
        self._sync_subnet_time(subnet)

        subnet.run(deadline=time.monotonic() + transition.subnet_deadline_secs)

        parent_outputs = [arc.place for arc in transition.outputs]
        return self._retrieve_subnet_outputs(subnet, transition.port_socket_map, parent_outputs)
