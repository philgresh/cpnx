"""Concurrent Petri net executor."""

import concurrent.futures
import random
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, TypeAlias

from cpnx.places import PacedResourcePlace, Place, ResourcePlace
from cpnx.sandbox import SandboxEvaluator
from cpnx.tokens import Token
from cpnx.transitions import InputArc, OutputArc, SubstitutionTransition, Transition
from cpnx.visualization import snapshot, to_dot

_DepositFn: TypeAlias = Callable[[str, Token], None]
"""Callable that deposits a token into a named place. Must be invoked under the engine lock."""


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
) -> tuple[str, Token, bool]:
    max_retries = transition.max_retries
    if max_retries is None or t.attempts < max_retries:
        retry_at = time.monotonic() + retry_delay
        return src_name, t.evolve(available_at=retry_at, attempts=t.attempts + 1), False
    return error_place, t.evolve(available_at=0.0), True


def _process_rollback_token(
    t: Token,
    src_name: str,
    transition: Transition,
    retry_delay: float,
    error_place: str,
) -> tuple[str, Token, bool]:
    if t.is_resource:
        return src_name, t, False
    return _rollback_data_token(t, src_name, transition, retry_delay, error_place)


def _rollback_failed_transition(
    transition: Transition,
    token_sources: list[tuple[str, Token]],
    *,
    deposit: _DepositFn,
    retry_delay: float,
    error_place: str,
) -> tuple[list[tuple[str, Token]], list[Token], list[Token]]:
    """Return all consumed tokens to their source places after a failed firing.

    ``deposit`` must be a callable that is safe to invoke under the engine lock —
    callers are responsible for holding it before calling this function.
    """
    deposited: list[tuple[str, Token]] = []
    dead_lettered_data_tokens: list[Token] = []
    data_tokens = [t for _, t in token_sources if not t.is_resource]
    for src_name, t in token_sources:
        dest, rollback_t, is_dead_letter = _process_rollback_token(t, src_name, transition, retry_delay, error_place)
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

    A. **Colour-routed error (primary/canonical)**: The action catches its own exception,
       returns an error-coloured token, and output-arc expressions (e.g. using
       `OutputArc.expression`) route success vs error tokens to different places.
       This preserves firing rules and token conservation (1-in-1-out).
    B. **Bounded atomic-retry**: On action failure/exception, the data token is rolled back
       to its source place with a delay (`retry_delay`) and an incremented `attempts`
       counter, retrying up to `Transition.max_retries` times (default 5). Once exhausted,
       it is dead-lettered to `error_place`.
    C. **Immediate dead-letter**: By setting `max_retries=0` on a transition, any action
       failure immediately routes the data token to `error_place`.

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
                               while holding the engine lock, so this value directly
                               caps how long concurrent `deposit()` and `step()`
                               calls will block. Keep well under 1 s (default: 0.1).
            retry_delay: Delay in seconds to apply to data tokens when rolling them
                         back to their source places on transient failure (default: 1.0).
        """
        self.max_workers = max_workers
        self.error_place = error_place
        self.cooldown_interval = cooldown_interval
        self.timeout_secs = timeout_secs
        self.expr_timeout_secs = expr_timeout_secs
        self.retry_delay = retry_delay
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
    def _filter_highest_priority(transitions: list[Transition]) -> list[Transition]:
        if not transitions:
            return []
        min_priority = min(t.priority for t in transitions)
        return [t for t in transitions if t.priority == min_priority]

    def _select_transition_to_fire(self) -> Transition | None:
        enabled = [t for t in self.transitions.values() if self._is_transition_enabled(t)]
        candidates = self._filter_highest_priority(enabled)
        return random.choice(candidates) if candidates else None

    def _retrieve_tokens_for_arc(self, arc: InputArc, place: Place, m_time: float | None) -> list[Token]:
        if arc.consume_all:
            return place.retrieve_all(model_time=m_time)
        if arc.expression is not None:
            # Consumption uses the same string-vs-callable dispatch as enabling, but must let
            # a raising expression propagate so _retrieve_consumed_tokens can roll back the
            # tokens already consumed from earlier arcs (unlike the enable check, which treats
            # a raising expression as "not enabled").
            available = place.peek(len(place), model_time=m_time)
            ordered = self._eval_expression(arc.expression, arc._compiled_expression, available)  # type: ignore[attr-defined]
            return place.retrieve_specific(ordered[: arc.count], model_time=m_time)
        return place.retrieve(arc.count, model_time=m_time)

    def _retrieve_consumed_tokens(
        self, transition: Transition, m_time: float | None
    ) -> tuple[list[Token], list[tuple[str, Token]]]:
        consumed_tokens: list[Token] = []
        token_sources: list[tuple[str, Token]] = []
        try:
            for arc in transition.inputs:
                place = self.places[arc.place]
                tokens = self._retrieve_tokens_for_arc(arc, place, m_time)
                consumed_tokens.extend(tokens)
                for t in tokens:
                    token_sources.append((arc.place, t))
        except Exception:
            # Expression raised mid-loop — return already-consumed tokens to their
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
        with self._lock:
            selected = self._select_transition_to_fire()
            if not selected:
                return False

            m_time = self._get_model_time_under_lock()
            consumed_tokens, token_sources = self._retrieve_consumed_tokens(selected, m_time)

            self._running_count += 1
            try:
                self._executor.submit(self._execute_transition, selected, consumed_tokens, token_sources)
            except Exception:
                # submit() failed (e.g. executor shut down) — undo the increment so
                # is_quiescent() doesn't permanently block.
                self._running_count -= 1
                # Return consumed tokens back to their source places
                for src_name, t in token_sources:
                    self._deposit_under_lock(src_name, t)
                raise

        return True

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
                return False
            return not any(self._is_transition_potentially_enabled(t) for t in self.transitions.values())

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
            return not any(self._is_transition_enabled(t) for t in self.transitions.values())

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
        """Return ``True`` if all preconditions for firing *transition* are satisfied.

        Checks token availability (including cooldowns and thresholds), settle
        windows, and the user-supplied guard. Called while holding ``self._lock``.
        """
        m_time = self._get_model_time_under_lock()
        ok, candidate_tokens = self._check_input_preconditions(transition, m_time)
        if not ok:
            return False

        if not self._check_output_capacity(transition):
            return False

        return self._check_transition_guard(transition, candidate_tokens)

    def _is_place_ready(self, arc: InputArc, place: Place | None, m_time: float | None) -> bool:
        if not place or not place.can_retrieve(arc.count, model_time=m_time):
            return False
        return self._is_settle_time_met(place, arc)

    def _check_arc_preconditions(self, arc: InputArc, place: Place | None, m_time: float | None) -> list[Token] | None:
        if not self._is_place_ready(arc, place, m_time):
            return None
        available = place.peek(len(place), model_time=m_time)  # type: ignore[union-attr]
        # _resolve_input_tokens enforces the multiplicity rule (returns None if fewer
        # than arc.count tokens are eligible) and already slices to arc.count.
        return self._resolve_input_tokens(arc, available)

    def _check_input_preconditions(self, transition: Transition, m_time: float | None) -> tuple[bool, list[Token]]:
        tokens_for_guard: list[Token] = []
        for arc in transition.inputs:
            resolved = self._check_arc_preconditions(arc, self.places.get(arc.place), m_time)
            if resolved is None:
                return False, []
            tokens_for_guard.extend(resolved)
        return True, tokens_for_guard

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

        Shared by the firing check (via `_check_arc_preconditions`) and the
        timing-agnostic `_is_transition_potentially_enabled` so both apply
        the identical multiplicity rule; callers differ only in how `available`
        is computed (real/model time vs. ignoring timing).
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
        `PacedResourcePlace` (tokens in cooldown are counted
        as present) and settle windows. Used by [`is_quiescent`][cpnx.PetriNet.is_quiescent] to
        distinguish "no work possible" from "work temporarily blocked by timing".
        """
        candidate_tokens: list[Token] = []
        for arc in transition.inputs:
            place = self.places.get(arc.place)
            if place is None:
                return False
            # Check count ignoring timing (model_time=float("inf"))
            if not place.can_retrieve(arc.count, model_time=float("inf")):
                return False
            # Speculatively resolve candidate tokens ignoring timing
            available = place.peek(len(place), model_time=float("inf"))
            tokens = self._resolve_input_tokens(arc, available)
            if tokens is None:
                return False
            candidate_tokens.extend(tokens)

        return self._check_transition_guard(transition, candidate_tokens)

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
