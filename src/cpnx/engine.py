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
        if t.is_resource:
            deposit(src_name, t)
            deposited.append((src_name, t))
        else:
            max_retries = transition.max_retries
            if max_retries is None or t.attempts < max_retries:
                retry_at = time.monotonic() + retry_delay
                rollback_t = t.evolve(available_at=retry_at, attempts=t.attempts + 1)
                deposit(src_name, rollback_t)
                deposited.append((src_name, rollback_t))
            else:
                rollback_t = t.evolve(available_at=0.0)
                deposit(error_place, rollback_t)
                deposited.append((error_place, rollback_t))
                dead_lettered_data_tokens.append(rollback_t)
    return deposited, dead_lettered_data_tokens, data_tokens


class PetriNet:
    """Concurrent Petri net executor.

    Manages places, transitions, and a thread pool for firing transitions
    concurrently. Resource tokens are guaranteed to be returned to their source
    places even when a transition's action raises. For data tokens, the net supports
    three error handling dispositions:

    A. **Colour-routed error (primary/canonical)**: The action catches its own exception,
       returns an error-coloured token, and output-arc expressions (e.g. using
       ``OutputArc.expression``) route success vs error tokens to different places.
       This preserves firing rules and token conservation (1-in-1-out).
    B. **Bounded atomic-retry**: On action failure/exception, the data token is rolled back
       to its source place with a delay and an incremented ``attempts`` counter, retrying
       up to ``max_retries`` times (default 5). Once exhausted, it is dead-lettered to
       ``error_place``.
    C. **Immediate dead-letter**: By setting ``max_retries=0`` on a transition, any action
       failure immediately routes the data token to ``error_place``.

    Note that ``error_place`` can be configured as a ``SinkPlace`` (e.g. ``SinkPlace("failed", keep_last=10)``)
    to keep only the last N failures for diagnostics, preventing unbounded memory growth in long-running streaming nets.

    Typical usage::

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

    Use as a context manager to ensure the thread pool shuts down cleanly::

        with PetriNet(max_workers=4) as net:
            ...
            net.run()
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
        """Initialise the executor.

        Args:
            max_workers: Maximum number of transitions that may fire concurrently.
            error_place: Name of the place that receives data tokens from failed
                         transitions. Created automatically as a standard Place,
                         but can be overridden by registering a custom place (like a
                         SinkPlace) with the same name.
            places: Optional list of :class:`~cpnx.places.Place` instances to
                    register at construction time.
            transitions: Optional list of :class:`~cpnx.transitions.Transition`
                         instances to register at construction time.
            cooldown_interval: Cooldown check polling interval in seconds.
            timeout_secs: Maximum allowed execution time in seconds for transition
                          action callables (run off the engine lock).
            expr_timeout_secs: Maximum allowed execution time in seconds for guard
                               and arc expression callables. These are evaluated
                               while holding the engine lock, so this value directly
                               caps how long concurrent ``deposit()`` and ``step()``
                               calls will block. Keep well under 1 s (default: 0.1 s).
            retry_delay: Delay in seconds to apply to data tokens when rolling them
                         back to their source places on transient failure.
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
        #: Signature: ``(transition_name: str, duration_secs: float) -> None``.
        #: Fires outside the engine lock — safe to call :meth:`deposit` from here.
        self.on_transition_fired: Callable[[str, float], None] | None = None

        #: Called after any token is deposited into any place.
        #: Signature: ``(place_name: str, token: Token) -> None``.
        #: Fires outside the engine lock — safe to call :meth:`deposit` from here.
        #: Warning: do **not** call :meth:`add_place` or :meth:`add_transition` from
        #: within this callback.
        self.on_token_deposited: Callable[[str, Token], None] | None = None

        #: Called when a data token is dead-lettered to the error place (due to exhausted retries or immediate failure).
        #: Signature: ``(transition_name: str, token: Token) -> None``.
        #: Fires outside the engine lock.
        self.on_token_dead_lettered: Callable[[str, Token], None] | None = None

        #: Called when a transition's action raises an exception.
        #: Signature: ``(transition_name: str, exc: Exception, token: Token | None) -> None``.
        #: *token* is the data token that was routed to the error place or rolled back,
        #: or ``None`` if the transition had no data inputs.
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
        """Returns the current logical or real time of the PetriNet."""
        with self._lock:
            return self._get_model_time_under_lock()

    def advance_time(self, new_time: float) -> None:
        """Advance the logical clock of the net. Must be strictly monotonic."""
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
        """Register a place with the net.

        Must be called before any transition arc references this place by name
        and before :meth:`run` or :meth:`step` is invoked.

        Args:
            place: A :class:`~cpnx.places.Place`,
                   :class:`~cpnx.places.ResourcePlace`,
                   :class:`~cpnx.places.PacedResourcePlace`, or
                   :class:`~cpnx.places.ThresholdPlace` instance.
        """
        with self._lock:
            if place.name in self.transitions:
                raise ValueError(f"Name overlap: '{place.name}' is already registered as a Transition.")
            self.places[place.name] = place
            if isinstance(place, PacedResourcePlace):
                self._has_timed_features = True

    def add_transition(self, transition: Transition) -> None:
        """Register a transition with the net.

        All places referenced by the transition's input and output arcs must be
        registered via :meth:`add_place` before the first time the transition
        fires — referencing an undeclared name raises :exc:`KeyError` at fire time.

        Args:
            transition: The :class:`~cpnx.transitions.Transition` to register.
        """
        with self._lock:
            if transition.name in self.places:
                raise ValueError(f"Name overlap: '{transition.name}' is already registered as a Place.")
            for arc in transition.inputs + transition.outputs:
                if arc.place == transition.name or arc.place in self.transitions:
                    raise TypeError(
                        f"Arc target '{arc.place}' is a Transition, not a Place. "
                        "Arcs must connect Places↔Transitions only."
                    )
            self.transitions[transition.name] = transition
            if any(arc.settle_secs > 0.0 for arc in transition.inputs):
                self._has_timed_features = True

    def deposit(self, place_name: str, token: Token) -> None:
        """Deposit *token* into *place_name*, creating the place if it does not exist.

        This is the primary entry point for injecting work items from external
        sources (data loaders, scheduled events, API responses, etc.).

        Note:
            Auto-creation always produces a bare :class:`~cpnx.places.Place`.
            If you need a :class:`~cpnx.places.ResourcePlace` or
            :class:`~cpnx.places.ThresholdPlace`, call :meth:`add_place` first.

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

    def _select_transition_to_fire(self) -> Transition | None:
        enabled = [t for t in self.transitions.values() if self._is_transition_enabled(t)]
        if not enabled:
            return None

        min_priority = min(t.priority for t in enabled)
        candidates = [t for t in enabled if t.priority == min_priority]
        return random.choice(candidates)

    def _retrieve_consumed_tokens(
        self, transition: Transition, m_time: float | None
    ) -> tuple[list[Token], list[tuple[str, Token]]]:
        consumed_tokens: list[Token] = []
        token_sources: list[tuple[str, Token]] = []
        try:
            for arc in transition.inputs:
                place = self.places[arc.place]
                if arc.consume_all:
                    tokens = place.retrieve_all(model_time=m_time)
                elif arc.expression is not None:
                    available = place.peek(len(place), model_time=m_time)
                    if isinstance(arc.expression, str):
                        ordered = SandboxEvaluator.evaluate_compiled(
                            arc._compiled_expression,
                            {"tokens": available},  # type: ignore[attr-defined]
                        )
                    else:
                        ordered = self._call_expr(arc.expression, available, timeout=self.expr_timeout_secs)
                    tokens = place.retrieve_specific(ordered[: arc.count], model_time=m_time)
                else:
                    tokens = place.retrieve(arc.count, model_time=m_time)
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
        """Fire the highest-priority enabled transition, scheduling its action asynchronously.

        Atomically selects and enables the transition, consumes its input tokens,
        and submits the action to the thread pool. Returns before the action completes.

        Returns:
            ``True`` if a transition was fired; ``False`` if no transition is
            currently enabled (net may still have in-flight transitions).

        Raises:
            RuntimeError: If the executor has been shut down (e.g. after exiting
                          a ``with`` block).
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

    def validate(self) -> None:
        """Validate the structural topology of the Petri net.

        Checks for name overlaps between places and transitions, and verifies
        that all transition arcs connect to valid places and not transitions.
        """
        with self._lock:
            # Check overlap between place names and transition names
            overlaps = set(self.places.keys()) & set(self.transitions.keys())
            if overlaps:
                raise ValueError(f"Name overlap: '{list(overlaps)[0]}' is registered as both a Place and a Transition.")

            for name, transition in self.transitions.items():
                for arc in transition.inputs + transition.outputs:
                    if arc.place in self.transitions:
                        raise TypeError(
                            f"Arc target '{arc.place}' in transition '{name}' is a Transition, not a Place. "
                            f"Arcs must connect Places↔Transitions only."
                        )
                    if arc.place not in self.places:
                        raise KeyError(f"Place '{arc.place}' referenced by transition '{name}' is not registered.")

    def run(
        self,
        deadline: float | None = None,
        *,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Fire enabled transitions until the net is quiescent or the deadline passes.

        Sleeps efficiently on a :class:`threading.Event` rather than busy-waiting,
        waking immediately when new tokens become available.

        Args:
            deadline: **Absolute** monotonic timestamp after which the loop exits.
                      Always construct this as ``time.monotonic() + <seconds>``.
                      If ``None`` (default), runs until the net is quiescent.
            stop_event: Optional :class:`threading.Event`. If set, the loop exits
                        promptly.

        Warning:
            Passing a raw duration (e.g. ``run(30)``) instead of an absolute
            deadline causes immediate exit because ``time.monotonic()`` is always
            much larger than small floats. Use ``run(deadline=time.monotonic() + 30)``.

        Example::

            net.run(deadline=time.monotonic() + 30)  # run for up to 30 seconds
            net.run()  # run to quiescence
        """
        self.validate()
        while not self.is_quiescent():
            if stop_event is not None and stop_event.is_set():
                break
            if deadline is not None and time.monotonic() > deadline:
                break
            self._work_available.clear()
            if not self.step():
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    timeout = min(remaining, self.cooldown_interval)
                    if stop_event is not None:
                        timeout = min(timeout, 0.1)
                    self._work_available.wait(timeout=timeout)
                else:
                    timeout = self.cooldown_interval
                    if stop_event is not None:
                        timeout = min(timeout, 0.1)
                    self._work_available.wait(timeout=timeout)

    def is_quiescent(self) -> bool:
        """Return ``True`` if no transitions are running and none can currently fire.

        A net is quiescent when ``_running_count == 0`` and every transition is
        blocked (insufficient tokens, guards False, etc.).

        For :class:`~cpnx.places.PacedResourcePlace`, tokens in cooldown are
        counted as *present* — the net is not considered quiescent while tokens
        are merely cooling down, since they will become available once the
        cooldown expires.

        Returns:
            ``True`` if the net has no pending or in-flight work.
        """
        with self._lock:
            if self._running_count > 0:
                return False
            return not any(self._is_transition_potentially_enabled(t) for t in self.transitions.values())

    @property
    def marking(self) -> dict[str, tuple[Token, ...]]:
        """Current marking: maps each place name to its live token tuple.

        In CPN formalism the *marking* ``M`` is a function from places to
        multisets of colour values. This property returns a tuple of live
        :class:`~cpnx.tokens.Token` objects currently in each place.

        Returns:
            Dict mapping place name → tuple of tokens currently in that place.
        """
        with self._lock:
            return {name: place.tokens for name, place in self.places.items()}

    def is_dead(self) -> bool:
        """Return ``True`` if no transition is currently enabled (CPN dead state).

        In CPN theory a *dead marking* is one in which no transition can fire
        given the current token distribution. Unlike :meth:`is_quiescent`, this
        does not check for in-flight transitions — it is a pure marking-level
        check.

        Returns:
            ``True`` if every transition's enabling condition fails.
        """
        with self._lock:
            return not any(self._is_transition_enabled(t) for t in self.transitions.values())

    def snapshot(self) -> dict:
        """Return a JSON-serialisable snapshot of current place markings.

        Returns:
            Dict with ``"places"`` (mapping place name → list of token dicts with
            keys ``id``, ``payload``, ``created_at``, ``is_resource``) and
            ``"running_count"`` (number of transitions currently executing).

        Example::

            import json
            print(json.dumps(net.snapshot(), indent=2))
        """
        return snapshot(self)

    def to_dot(self) -> str:
        """Generate a Graphviz DOT string of the net topology.

        Place nodes are circles annotated with current token counts.
        Transition nodes are boxes. Arc labels include ``count``,
        ``consume_all``, and ``settle_secs`` where non-default.

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
        """Deposit *token* into an already-registered place (caller holds ``self._lock``).

        Unlike the public :meth:`deposit`, this method does NOT auto-create missing
        places — it raises :exc:`KeyError` so typos in arc names are caught loudly
        rather than silently creating a bare :class:`~cpnx.places.Place` with the
        wrong type. Callbacks are NOT fired here; callers collect deposits and fire
        them after releasing the lock.

        Args:
            place_name: Name of an already-registered place.
            token: The token to deposit.

        Raises:
            KeyError: If *place_name* has not been registered with :meth:`add_place`.
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

    def _check_input_preconditions(self, transition: Transition, m_time: float) -> tuple[bool, list[Token]]:
        candidate_tokens: list[Token] = []
        for arc in transition.inputs:
            place = self.places.get(arc.place)
            if place is None or not place.can_retrieve(arc.count, model_time=m_time):
                return False, []

            if not self._is_settle_time_met(place, arc):
                return False, []

            # Speculatively resolve candidate tokens
            available = place.peek(len(place), model_time=m_time)
            tokens = self._resolve_input_tokens(arc, available)
            if tokens is None:
                return False, []
            candidate_tokens.extend(tokens)

        return True, candidate_tokens

    def _is_settle_time_met(self, place: Place, arc: InputArc) -> bool:
        if arc.settle_secs <= 0.0:
            return True
        if self._model_time is not None:
            elapsed = self._model_time - place.last_deposit_time_model
        else:
            elapsed = time.monotonic() - place.last_deposit_time
        return elapsed >= arc.settle_secs

    def _resolve_input_tokens(self, arc: InputArc, available: list[Token]) -> list[Token] | None:
        if arc.consume_all:
            return available
        if arc.expression is not None:
            try:
                if isinstance(arc.expression, str):
                    ordered = SandboxEvaluator.evaluate_compiled(arc._compiled_expression, {"tokens": available})  # type: ignore[attr-defined]
                else:
                    ordered = self._call_expr(arc.expression, available, timeout=self.expr_timeout_secs)
                return ordered[: arc.count]
            except Exception:
                return None
        return available[: arc.count]

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
            if isinstance(transition.guard, str):
                return bool(
                    SandboxEvaluator.evaluate_compiled(transition._compiled_guard, {"tokens": candidate_tokens})
                )  # type: ignore[attr-defined]
            else:
                return bool(self._call_expr(transition.guard, candidate_tokens, timeout=self.expr_timeout_secs))
        except Exception:
            return False

    def _is_transition_potentially_enabled(self, transition: Transition) -> bool:
        """Return ``True`` if *transition* could fire given current token counts, ignoring timing.

        Unlike :meth:`_is_transition_enabled`, this ignores cooldown timers on
        :class:`~cpnx.places.PacedResourcePlace` (tokens in cooldown are counted
        as present) and settle windows. Used by :meth:`is_quiescent` to distinguish
        "no work possible" from "work temporarily blocked by timing".
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

    def _execute_transition(
        self,
        transition: Transition,
        consumed_tokens: list[Token],
        token_sources: list[tuple[str, Token]],
    ) -> None:
        """Execute a transition action and distribute output tokens.

        Runs on the thread pool. Guarantees:

        - On failure, resource tokens are returned to their source places.
        - Data tokens are retried (returned to source with a delay and an incremented
          ``attempts`` counter) up to ``max_retries`` times, or dead-lettered to
          ``error_place`` when exhausted.
        - Callbacks (:attr:`on_token_deposited`, :attr:`on_error`, :attr:`on_token_dead_lettered`)
          fire **outside** the engine lock to prevent re-entrant deadlocks.
        - :attr:`_running_count` is always decremented exactly once.
        """
        try:
            start_time = time.monotonic()
            success, output_tokens, error = self._execute_transition_action(transition, consumed_tokens, token_sources)
            duration = 0.0

            with self._lock:
                duration = time.monotonic() - start_time
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

    def _evaluate_output_guards(
        self, transition: Transition, output_tokens_data: list[Token]
    ) -> list[tuple[OutputArc, bool]]:
        active_outputs: list[tuple[OutputArc, bool]] = []
        for arc in transition.outputs:
            is_res = isinstance(self.places.get(arc.place), (ResourcePlace, PacedResourcePlace))
            if arc.expression is None:
                active_outputs.append((arc, is_res))
            elif isinstance(arc.expression, str):
                if SandboxEvaluator.evaluate_compiled(arc._compiled_expression, {"tokens": output_tokens_data}):  # type: ignore[attr-defined]
                    active_outputs.append((arc, is_res))
            elif self._call_expr(arc.expression, output_tokens_data, timeout=self.expr_timeout_secs):
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

    def _verify_deposit_constraints(
        self,
        planned_deposits: list[tuple[str, Token]],
    ) -> Exception | None:
        place_deposit_counts: dict[str, int] = {}
        for place_name, _ in planned_deposits:
            place_deposit_counts[place_name] = place_deposit_counts.get(place_name, 0) + 1

        for place_name, token in planned_deposits:
            place = self.places.get(place_name)
            if place is None:
                return KeyError(f"Place '{place_name}' is not registered.")
            if not place.can_accept(token):
                return TypeError(f"Place '{place_name}' cannot accept token with color '{token.color}'.")

        for place_name, count in place_deposit_counts.items():
            place = self.places.get(place_name)
            if place is not None and not place.can_deposit(count):
                return ValueError(f"Place '{place_name}' would exceed its bound of {place.bound}.")
        return None

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
        if self.on_error and error and isinstance(error, Exception):
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
