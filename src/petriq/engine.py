"""Concurrent Petri net executor."""

import random
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from petriq.places import PacedResourcePlace, Place, ResourcePlace
from petriq.tokens import Token
from petriq.transitions import Transition
from petriq.visualization import snapshot, to_dot


class PetriNet:
    """Concurrent Petri net executor.

    Manages places, transitions, and a thread pool for firing transitions
    concurrently. Resource tokens are guaranteed to be returned to their source
    places even when a transition's action raises — data tokens are routed to
    :attr:`error_place` instead.

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
        net.run(deadline=time.monotonic() + 30)

    Use as a context manager to ensure the thread pool shuts down cleanly::

        with PetriNet(max_workers=4) as net:
            ...
            net.run(deadline=time.monotonic() + 30)
    """

    def __init__(
        self,
        max_workers: int = 4,
        error_place: str = "failed",
        places: list[Place] | None = None,
        transitions: list[Transition] | None = None,
        cooldown_interval: float = 0.05,
    ) -> None:
        """Initialise the executor.

        Args:
            max_workers: Maximum number of transitions that may fire concurrently.
            error_place: Name of the place that receives data tokens from failed
                         transitions. Created automatically — do not register a
                         place with this name manually.
            places: Optional list of :class:`~petriq.places.Place` instances to
                    register at construction time.
            transitions: Optional list of :class:`~petriq.transitions.Transition`
                         instances to register at construction time.
        """
        self.max_workers = max_workers
        self.error_place = error_place
        self.cooldown_interval = cooldown_interval
        self._has_timed_features = False
        self.places: dict[str, Place] = {}
        self.transitions: dict[str, Transition] = {}
        self._lock = threading.Lock()
        self._running_count = 0
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
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

        #: Called when a transition's action raises an exception.
        #: Signature: ``(transition_name: str, exc: Exception, token: Token | None) -> None``.
        #: *token* is the data token that was routed to the error place, or ``None``
        #: if the transition had no data inputs.
        #: Fires outside the engine lock.
        self.on_error: Callable[[str, Exception, Token | None], None] | None = None

        self.add_place(Place(error_place))
        for p in places or []:
            self.add_place(p)
        for t in transitions or []:
            self.add_transition(t)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_place(self, place: Place) -> None:
        """Register a place with the net.

        Must be called before any transition arc references this place by name
        and before :meth:`run` or :meth:`step` is invoked.

        Args:
            place: A :class:`~petriq.places.Place`,
                   :class:`~petriq.places.ResourcePlace`,
                   :class:`~petriq.places.PacedResourcePlace`, or
                   :class:`~petriq.places.ThresholdPlace` instance.
        """
        with self._lock:
            self.places[place.name] = place
            if isinstance(place, PacedResourcePlace):
                self._has_timed_features = True

    def add_transition(self, transition: Transition) -> None:
        """Register a transition with the net.

        All places referenced by the transition's input and output arcs must be
        registered via :meth:`add_place` before the first time the transition
        fires — referencing an undeclared name raises :exc:`KeyError` at fire time.

        Args:
            transition: The :class:`~petriq.transitions.Transition` to register.
        """
        with self._lock:
            self.transitions[transition.name] = transition
            if any(arc.settle_secs > 0.0 for arc in transition.inputs):
                self._has_timed_features = True

    def deposit(self, place_name: str, token: Token) -> None:
        """Deposit *token* into *place_name*, creating the place if it does not exist.

        This is the primary entry point for injecting work items from external
        sources (data loaders, scheduled events, API responses, etc.).

        Note:
            Auto-creation always produces a bare :class:`~petriq.places.Place`.
            If you need a :class:`~petriq.places.ResourcePlace` or
            :class:`~petriq.places.ThresholdPlace`, call :meth:`add_place` first.

        Args:
            place_name: Name of the target place.
            token: The token to deposit.
        """
        with self._lock:
            if place_name not in self.places:
                self.places[place_name] = Place(place_name)
            self.places[place_name].deposit(token)
        self._work_available.set()
        if self.on_token_deposited:
            try:
                self.on_token_deposited(place_name, token)
            except Exception:
                pass

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
            enabled = [t for t in self.transitions.values() if self._is_transition_enabled(t)]
            if not enabled:
                return False

            min_priority = min(t.priority for t in enabled)
            candidates = [t for t in enabled if t.priority == min_priority]
            selected = random.choice(candidates)

            consumed_tokens: list[Token] = []
            token_sources: list[tuple[str, Token]] = []
            for arc in selected.inputs:
                place = self.places[arc.place]
                if arc.consume_all:
                    tokens = place.retrieve_all()
                elif arc.expression is not None:
                    ordered = arc.expression(place.tokens)
                    tokens = place.retrieve_specific(ordered[: arc.count])
                else:
                    tokens = place.retrieve(arc.count)
                consumed_tokens.extend(tokens)
                for t in tokens:
                    token_sources.append((arc.place, t))

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

    def run(self, deadline: float) -> None:
        """Fire enabled transitions until the net is quiescent or the deadline passes.

        Sleeps efficiently on a :class:`threading.Event` rather than busy-waiting,
        waking immediately when new tokens become available.

        Args:
            deadline: **Absolute** monotonic timestamp after which the loop exits.
                      Always construct this as ``time.monotonic() + <seconds>``.

        Warning:
            Passing a raw duration (e.g. ``run(30)``) instead of an absolute
            deadline causes immediate exit because ``time.monotonic()`` is always
            much larger than small floats. Use ``run(deadline=time.monotonic() + 30)``.

        Example::

            net.run(deadline=time.monotonic() + 30)  # run for up to 30 seconds
        """
        while not self.is_quiescent():
            if time.monotonic() > deadline:
                break
            self._work_available.clear()
            if not self.step():
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    # Cap at cooldown_interval if timed features are active.
                    timeout = min(remaining, self.cooldown_interval) if self._has_timed_features else remaining
                    self._work_available.wait(timeout=timeout)

    def is_quiescent(self) -> bool:
        """Return ``True`` if no transitions are running and none can currently fire.

        A net is quiescent when ``_running_count == 0`` and every transition is
        blocked (insufficient tokens, guards False, etc.).

        For :class:`~petriq.places.PacedResourcePlace`, tokens in cooldown are
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
    def marking(self) -> dict[str, list[Token]]:
        """Current marking: maps each place name to its live token list.

        In CPN formalism the *marking* ``M`` is a function from places to
        multisets of colour values. This property returns live
        :class:`~petriq.tokens.Token` objects — mutating them affects the net.
        For a JSON-serialisable snapshot use :meth:`snapshot` instead.

        Returns:
            Dict mapping place name → list of tokens currently in that place.
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

    def __del__(self) -> None:
        try:
            self._executor.shutdown(wait=False)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _deposit_under_lock(self, place_name: str, token: Token) -> None:
        """Deposit *token* into an already-registered place (caller holds ``self._lock``).

        Unlike the public :meth:`deposit`, this method does NOT auto-create missing
        places — it raises :exc:`KeyError` so typos in arc names are caught loudly
        rather than silently creating a bare :class:`~petriq.places.Place` with the
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
        self.places[place_name].deposit(token)

    def _is_transition_enabled(self, transition: Transition) -> bool:
        """Return ``True`` if all preconditions for firing *transition* are satisfied.

        Checks token availability (including cooldowns and thresholds), settle
        windows, and the user-supplied guard. Called while holding ``self._lock``.
        """
        for arc in transition.inputs:
            place = self.places.get(arc.place)
            if place is None:
                return False
            if not place.can_retrieve(arc.count):
                return False
            if arc.settle_secs > 0.0:
                # last_deposit_time is only written while self._lock is held, so
                # reading it here (also under self._lock) is safe without place._lock.
                if time.monotonic() - place.last_deposit_time < arc.settle_secs:
                    return False
        # Back-pressure: refuse to fire if an unguarded output arc's target place is full.
        # Guarded arcs are skipped — their target may never receive a token, so checking
        # capacity speculatively would cause spurious blocking.
        for arc in transition.outputs:
            if arc.expression is not None:
                continue
            place = self.places.get(arc.place)
            if place is not None and not place.can_deposit(arc.count):
                return False

        if transition.guard is not None:
            try:
                if not transition.guard():
                    return False
            except Exception:
                return False
        return True

    def _is_transition_potentially_enabled(self, transition: Transition) -> bool:
        """Return ``True`` if *transition* could fire given current token counts, ignoring timing.

        Unlike :meth:`_is_transition_enabled`, this ignores cooldown timers on
        :class:`~petriq.places.PacedResourcePlace` (tokens in cooldown are counted
        as present) and settle windows. Used by :meth:`is_quiescent` to distinguish
        "no work possible" from "work temporarily blocked by timing".
        """
        for arc in transition.inputs:
            place = self.places.get(arc.place)
            if place is None:
                return False
            if isinstance(place, PacedResourcePlace):
                if len(place) < arc.count:
                    return False
            else:
                if not place.can_retrieve(arc.count):
                    return False
        if transition.guard is not None:
            try:
                if not transition.guard():
                    return False
            except Exception:
                return False
        return True

    def _execute_transition(
        self,
        transition: Transition,
        consumed_tokens: list[Token],
        token_sources: list[tuple[str, Token]],
    ) -> None:
        """Execute a transition action and distribute output tokens.

        Runs on the thread pool. Guarantees:

        - Resource tokens always return to their source places, even on failure.
        - Data tokens from a failed transition are routed to :attr:`error_place`.
        - Callbacks (:attr:`on_token_deposited`, :attr:`on_error`) fire **outside**
          the engine lock to prevent re-entrant deadlocks.
        - :attr:`_running_count` is always decremented exactly once.
        """
        start_time = time.monotonic()
        success = False
        output_tokens: list[Token] = []
        error: BaseException | None = None

        try:
            output_tokens = transition.action(consumed_tokens)
            success = True
        except Exception as exc:
            error = exc

        deposited: list[tuple[str, Token]] = []
        duration = 0.0
        data_tokens: list[Token] = []

        with self._lock:
            duration = time.monotonic() - start_time

            if success:
                res_deque: deque[Token] = deque(t for t in consumed_tokens if t.is_resource)
                out_deque: deque[Token] = deque(t for t in output_tokens if not t.is_resource)

                # Pass 1: evaluate arc guards to determine which output arcs fire.
                # Guards receive the non-resource output tokens (CPN arc guard semantics).
                # Resource arcs are never guarded — resources must always return to a place.
                output_tokens_data = list(out_deque)
                active_outputs: list[tuple[Transition, bool]] = []  # type: ignore[type-arg]
                for arc in transition.outputs:
                    is_res = isinstance(self.places.get(arc.place), (ResourcePlace, PacedResourcePlace))
                    if arc.expression is None or (not is_res and arc.expression(output_tokens_data)):
                        active_outputs.append((arc, is_res))  # type: ignore[arg-type]

                # Pass 2: pre-flight — validate supply against active arcs only so that
                # guarded-out arcs don't inflate the demand count and cause spurious failures.
                resource_demand = sum(arc.count for arc, is_res in active_outputs if is_res)  # type: ignore[attr-defined]
                data_demand = sum(arc.count for arc, is_res in active_outputs if not is_res)  # type: ignore[attr-defined]
                if len(res_deque) < resource_demand:
                    success = False
                    error = ValueError(
                        f"Transition '{transition.name}': active resource output arcs require "
                        f"{resource_demand} resource token(s) but only {len(res_deque)} were consumed. "
                        f"Ensure each ResourcePlace/PacedResourcePlace InputArc has a matching OutputArc."
                    )
                elif len(out_deque) < data_demand:
                    success = False
                    error = ValueError(
                        f"Transition '{transition.name}': action returned {len(out_deque)} non-resource "
                        f"token(s) but active non-resource output arcs require {data_demand}. "
                        f"Ensure your action returns at least as many tokens as the sum of "
                        f"non-resource OutputArc counts (after arc guard evaluation)."
                    )

            if success:
                for arc, is_res_place in active_outputs:  # type: ignore[typing-target]
                    for _ in range(arc.count):  # type: ignore[attr-defined]
                        t = res_deque.popleft() if is_res_place else out_deque.popleft()
                        self._deposit_under_lock(arc.place, t)  # type: ignore[attr-defined]
                        deposited.append((arc.place, t))  # type: ignore[attr-defined]

                # Return any leftover resource tokens to their original source places
                while res_deque:
                    leftover_token = res_deque.popleft()
                    for src_name, t in token_sources:
                        if t.id == leftover_token.id:
                            self._deposit_under_lock(src_name, leftover_token)
                            deposited.append((src_name, leftover_token))
                            break

            if not success:
                # Return all resource tokens to their source places.
                for src_name, t in token_sources:
                    if t.is_resource:
                        self._deposit_under_lock(src_name, t)
                        deposited.append((src_name, t))

                # Route data tokens to the error sink.
                data_tokens = [t for _, t in token_sources if not t.is_resource]
                for dt in data_tokens:
                    self._deposit_under_lock(self.error_place, dt)
                    deposited.append((self.error_place, dt))

        # --- OUTSIDE THE LOCK ---
        if success:
            if self.on_transition_fired:
                try:
                    self.on_transition_fired(transition.name, duration)
                except Exception:
                    pass
        else:
            if self.on_error and error:
                # Unified dispatch: if there are no data tokens, call once with None.
                dispatch_tokens: list[Token | None] = list(data_tokens) if data_tokens else [None]
                for dt in dispatch_tokens:
                    try:
                        self.on_error(transition.name, error, dt)
                    except Exception:
                        pass

        if self.on_token_deposited:
            for pname, tok in deposited:
                try:
                    self.on_token_deposited(pname, tok)
                except Exception:
                    pass

        # Decrement running count and signal work available under lock
        with self._lock:
            self._running_count -= 1
        self._work_available.set()
