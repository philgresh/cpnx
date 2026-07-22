"""Place types for the cpnx Petri net executor.

All place classes are thread-safe. Choose the right type for your use case:

- [`Place`][cpnx.Place]              — unbounded FIFO queue for data / work tokens
- [`ResourcePlace`][cpnx.ResourcePlace]      — bounded permit pool (GPU slots, DB connections)
- [`PacedResourcePlace`][cpnx.PacedResourcePlace] — permit pool with per-token cooldown (API rate limits)
- [`ThresholdPlace`][cpnx.ThresholdPlace]     — accumulates tokens until a batch threshold is met

**CPN alignment:** In Coloured Petri Net theory a place has a *colour set* —
the type of tokens it accepts. `color_set` exposes this directly.
[`ResourcePlace`][cpnx.ResourcePlace] and [`PacedResourcePlace`][cpnx.PacedResourcePlace] are Python
shorthands for a place whose colour set is ``{"resource"}`` with a pre-filled initial marking.
"""

import threading
import time
from collections import deque

from cpnx.tokens import Token


class Place:
    """An unbounded FIFO queue that holds tokens flowing through a Petri net.

    **CPN equivalent:** a place with an unrestricted colour set (accepts any
    colour) and no initial marking. Set ``color_set`` to restrict accepted
    colours; set ``initial_marking`` to pre-fill with tokens at construction.

    All operations are thread-safe via an internal `threading.Lock`.
    """

    def __init__(
        self,
        name: str,
        bound: int | None = None,
        color_set: set[str] | None = None,
        initial_marking: list[Token] | None = None,
    ) -> None:
        """Create a new Place.

        Args:
            name: Unique identifier for this place within a [`PetriNet`][cpnx.PetriNet].
            bound: Optional k-bound (capacity constraint). The engine will not
                   fire a transition whose unguarded output arc targets this place
                   if doing so would exceed the bound. ``None`` (default) means
                   unbounded — this is standard CPN k-bounded place semantics.
            color_set: Set of accepted token colours. ``None`` (default) accepts
                       any colour. Pass e.g. ``{"data", "priority"}`` to enforce
                       typing at deposit time.
            initial_marking: Tokens to deposit at construction (CPN *I* function).
                             Deposited before any external code runs.
        """
        self.name = name
        self.bound = bound
        self.color_set = color_set
        self._tokens: deque[Token] = deque()
        self._lock = threading.Lock()
        self.last_deposit_time: float = 0.0
        self.last_deposit_time_model: float = 0.0

        for token in initial_marking or []:
            self._tokens.append(token)
            self.last_deposit_time = time.monotonic()

    def _has_available(self, t_limit: float, need: int) -> bool:
        """Return ``True`` as soon as *need* tokens with ``available_at <= t_limit`` are seen.

        Early-exits without scanning the rest of `_tokens` once *need* is reached, unlike
        building the full available list first. Callers must already hold `self._lock`.

        Args:
            t_limit: Effective time; tokens with `available_at` at or before this are counted.
            need: Number of available tokens required.

        Returns:
            ``True`` if at least *need* tokens are available, ``False`` otherwise.
        """
        seen = 0
        if need <= 0:
            return True
        for t in self._tokens:
            if t.available_at <= t_limit:
                seen += 1
                if seen >= need:
                    return True
        return False

    def deposit(self, token: Token, model_time: float | None = None) -> None:
        """Append *token* to the tail of the FIFO queue, enforcing the place's colour set.

        Updates `last_deposit_time` (and `last_deposit_time_model` if *model_time* is given),
        then calls `_on_deposit`.

        Args:
            token: The token to deposit.
            model_time: Optional logical clock timestamp recorded alongside the deposit.
                        Does not affect wall-clock availability checks.

        Raises:
            TypeError: If `color_set` is set and *token*'s colour is not in it.
        """
        with self._lock:
            if self.color_set is not None and token.color not in self.color_set:
                raise TypeError(
                    f"Place '{self.name}' has color_set {self.color_set!r} — "
                    f"cannot deposit token with color {token.color!r}."
                )
            self._tokens.append(token)
            self.last_deposit_time = time.monotonic()
            if model_time is not None:
                self.last_deposit_time_model = model_time
            self._on_deposit(token)

    def _on_deposit(self, token: Token) -> None:
        """Extension hook called immediately after a token is deposited (under the place lock).

        Override in subclasses to react to new arrivals. Default is a no-op.

        Args:
            token: The token that was just deposited.
        """

    def retrieve(self, count: int = 1, model_time: float | None = None) -> list[Token]:
        """Remove and return *count* tokens from the head of the queue, in FIFO order.

        A token is only eligible if its `available_at` timestamp is at or before the
        effective time (`model_time` if given, else `time.monotonic()`); tokens still
        in the future (e.g. cooling down) are skipped.

        Args:
            count: Number of tokens to retrieve. Must be >= 1.
            model_time: Optional logical clock timestamp used instead of wall-clock time
                        to determine which tokens are available.

        Returns:
            List of retrieved tokens in FIFO order.

        Raises:
            ValueError: If fewer than *count* tokens are available.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            available = [t for t in self._tokens if t.available_at <= t_limit]
            if len(available) < count:
                raise ValueError(
                    f"Place '{self.name}': cannot retrieve {count} token(s) — only {len(available)} available."
                )
            to_return = available[:count]
            remove_ids = {id(t) for t in to_return}
            self._tokens = deque(t for t in self._tokens if id(t) not in remove_ids)
            return to_return

    def retrieve_specific(self, tokens: list[Token], model_time: float | None = None) -> list[Token]:
        """Remove and return exactly the given *tokens*, matched by ``id`` rather than FIFO order.

        Used by the engine when an [`InputArc`][cpnx.InputArc] has an ``expression`` that
        selects a specific subset of tokens to consume rather than the head of the queue.
        Rebuilds the internal deque in O(n) rather than removing tokens one at a time.

        Args:
            tokens: Tokens to remove, identified by their ``id``. Each must currently be
                    present in this place and available at the effective time.
            model_time: Optional logical clock timestamp used instead of wall-clock time
                        to check token availability.

        Returns:
            The removed tokens, in the same order as *tokens* (not necessarily FIFO order).

        Raises:
            ValueError: If any token in *tokens* is not yet available (its `available_at`
                        is after the effective time) or is not found in this place.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            for t in tokens:
                if t.available_at > t_limit:
                    raise ValueError(
                        f"Place '{self.name}': token {t.id} is not yet available at {t_limit} "
                        f"(available_at={t.available_at})."
                    )
            requested_ids_by_identity = {id(t): t.id for t in tokens}
            present_ids = {id(t) for t in self._tokens}
            missing_identities = set(requested_ids_by_identity) - present_ids
            if missing_identities:
                missing = {requested_ids_by_identity[i] for i in missing_identities}
                raise ValueError(
                    f"Place '{self.name}': token id(s) {missing} not found — "
                    f"cannot retrieve_specific tokens that are not present."
                )
            remove_ids = set(requested_ids_by_identity)
            self._tokens = deque(t for t in self._tokens if id(t) not in remove_ids)
            return tokens

    def retrieve_all(self, model_time: float | None = None) -> list[Token]:
        """Remove and return every currently-available token, leaving not-yet-available tokens behind.

        Args:
            model_time: Optional logical clock timestamp used instead of wall-clock time
                        to determine which tokens are available.

        Returns:
            All available tokens in FIFO order; empty list if none are available.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            available = [t for t in self._tokens if t.available_at <= t_limit]
            remove_ids = {id(t) for t in available}
            self._tokens = deque(t for t in self._tokens if id(t) not in remove_ids)
            return available

    def peek(self, count: int = 1, model_time: float | None = None) -> list[Token]:
        """Return up to *count* available tokens from the head without removing them.

        Stops scanning `_tokens` as soon as *count* available tokens are collected
        (early-exit) rather than building the full available list first.

        Args:
            count: Maximum number of tokens to inspect.
            model_time: Optional logical clock timestamp used instead of wall-clock time
                        to determine which tokens are available.

        Returns:
            List of up to *count* tokens; may be shorter than requested if fewer
            are present or available. Does not modify the queue.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            if count <= 0:
                return []
            result: list[Token] = []
            for t in self._tokens:
                if t.available_at <= t_limit:
                    result.append(t)
                    if len(result) >= count:
                        break
            return result

    def can_retrieve(self, count: int = 1, model_time: float | None = None) -> bool:
        """Return ``True`` if at least *count* tokens are currently available for retrieval.

        Args:
            count: Number of tokens needed. Defaults to 1.
            model_time: Optional logical clock timestamp used instead of wall-clock time
                        to determine which tokens are available.

        Returns:
            ``True`` if at least *count* tokens have `available_at` at or before the
            effective time, ``False`` otherwise.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            return self._has_available(t_limit, count)

    def can_deposit(self, count: int = 1) -> bool:
        """Return ``True`` if the place can accept *count* more tokens without exceeding its bound.

        Implements k-bounded place semantics: a place with ``bound=k`` blocks when
        depositing would push the token count above ``k``. Unbounded places
        (``bound=None``) always return ``True``. Ignores colour — use `can_accept`
        to check colour compatibility.

        Args:
            count: Number of tokens to be deposited. Defaults to 1.

        Returns:
            ``True`` if depositing *count* tokens would not exceed `bound`, ``False`` otherwise.
        """
        with self._lock:
            if self.bound is None:
                return True
            return len(self._tokens) + count <= self.bound

    def can_accept(self, token: Token) -> bool:
        """Return ``True`` if *token*'s colour is compatible with this place's colour set.

        This is a non-mutating pre-flight check that does not modify the place's tokens
        and does not consider capacity — use `can_deposit` for bound checks.

        Args:
            token: The token to check for colour compatibility.

        Returns:
            ``True`` if `color_set` is ``None`` or contains *token*'s colour, ``False`` otherwise.
        """
        with self._lock:
            if self.color_set is not None and token.color not in self.color_set:
                return False
            return True

    @property
    def tokens(self) -> tuple[Token, ...]:
        """Snapshot of all current tokens (including not-yet-available ones) as an immutable tuple.

        Does not filter by `available_at` and does not consume or remove any tokens.
        """
        with self._lock:
            return tuple(self._tokens)

    def __len__(self) -> int:
        """Return the number of tokens currently in the place."""
        with self._lock:
            return len(self._tokens)

    def __bool__(self) -> bool:
        """A Place is always truthy, even when it contains no tokens."""
        return True


class ResourcePlace(Place):
    """A [`Place`][cpnx.Place] pre-filled with *capacity* resource tokens, for modelling finite permits.

    **CPN equivalent:** ``Place(color_set={"resource"}, initial_marking=[Token(color="resource")] * capacity)``.
    This class is a Python shorthand — it sets the colour set and initial marking
    automatically and documents the resource-return invariant explicitly. It does not
    otherwise change [`Place`][cpnx.Place]'s behavior: all inherited methods (`deposit`,
    `retrieve`, etc.) work exactly as on the base class.

    Resource tokens (``color="resource"``) are consumed when a transition fires
    and must be returned via a matching output arc. This models finite resources
    such as GPU slots, database connections, or thread-pool permits.

    Example:
        ```python
        gpu_pool = ResourcePlace("gpu_slots", capacity=4)
        ```
    """

    def __init__(self, name: str, capacity: int) -> None:
        """Create a ResourcePlace pre-filled with *capacity* resource tokens.

        Args:
            name: Unique identifier for this place within a [`PetriNet`][cpnx.PetriNet].
            capacity: Number of resource permits in the pool. ``0`` is valid
                      (creates an empty, permanently-blocking place).
        """
        self.capacity = capacity
        super().__init__(
            name,
            color_set={"resource"},
            initial_marking=[Token(color="resource") for _ in range(capacity)],
        )


class PacedResourcePlace(ResourcePlace):
    """A [`ResourcePlace`][cpnx.ResourcePlace] where returned tokens must cool down before becoming reusable.

    **CPN equivalent:** a Timed CPN [`ResourcePlace`][cpnx.ResourcePlace] where returned tokens
    carry a timestamp that prevents re-use until ``pacing_secs`` have elapsed.
    This is a pragmatic extension — standard Timed CPNs put timestamps on tokens,
    not cooldown windows on places.

    Useful for enforcing API rate limits or minimum inter-request intervals.
    Tokens are available immediately at construction; after each return via
    [`deposit`][cpnx.PacedResourcePlace.deposit], they are unavailable for *pacing_secs* seconds.

    Example — 10 Serper requests per second:
        ```python
        serper = PacedResourcePlace("serper", capacity=10, pacing_secs=0.1)
        ```
    """

    def __init__(self, name: str, capacity: int, pacing_secs: float) -> None:
        """Create a PacedResourcePlace.

        Args:
            name: Unique identifier for this place within a [`PetriNet`][cpnx.PetriNet].
            capacity: Number of resource permits in the pool.
            pacing_secs: Seconds a token must wait after being returned before
                         it becomes available again.
        """
        self.pacing_secs = pacing_secs
        super().__init__(name, capacity)

    def deposit(self, token: Token, model_time: float | None = None) -> None:
        """Return a resource token to the pool, replacing its `available_at` to start a cooldown timer.

        Differs from [`Place.deposit`][cpnx.Place.deposit]: instead of appending *token* unchanged,
        this creates a copy of *token* with `available_at` set to the effective time plus
        `pacing_secs`, so the token cannot be retrieved again until the cooldown elapses.
        Does not validate `color_set` (unlike the base class).

        Args:
            token: The resource token being returned. Must have ``color="resource"``.
            model_time: Optional logical clock timestamp used instead of wall-clock time
                        as the cooldown's start reference, and recorded in
                        `last_deposit_time_model`.
        """
        with self._lock:
            ref_time = model_time if model_time is not None else time.monotonic()
            # Create a new token with updated availability timestamp (stateless place cooldown)
            timed_token = token.evolve(available_at=ref_time + self.pacing_secs, id=token.id)
            self._tokens.append(timed_token)
            self.last_deposit_time = time.monotonic()
            if model_time is not None:
                self.last_deposit_time_model = model_time
            self._on_deposit(timed_token)

    def can_retrieve(self, count: int = 1, model_time: float | None = None) -> bool:
        """Return ``True`` if at least *count* tokens have completed their cooldown and are usable.

        Behaves identically to [`Place.can_retrieve`][cpnx.Place.can_retrieve]; documented
        separately here because "available" specifically means "cooldown has expired"
        for this class.

        Args:
            count: Number of cooled-down tokens needed. Defaults to 1.
            model_time: Optional logical clock timestamp used instead of wall-clock time
                        to determine which tokens have finished cooling down.

        Returns:
            ``True`` if at least *count* tokens are past their cooldown, ``False`` otherwise.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            return self._has_available(t_limit, count)

    def retrieve(self, count: int = 1, model_time: float | None = None) -> list[Token]:
        """Remove and return *count* tokens whose cooldown has expired, in expiry order.

        Behaves like [`Place.retrieve`][cpnx.Place.retrieve] but the error message reports
        how many tokens are still cooling down, which is specific to this class's semantics.
        Uses an O(n) rebuild rather than O(n^2) indexed deletion.

        Args:
            count: Number of cooled-down tokens to retrieve. Defaults to 1.
            model_time: Optional logical clock timestamp used instead of wall-clock time
                        to determine which tokens have finished cooling down.

        Returns:
            List of retrieved resource tokens in cooldown-expiry order.

        Raises:
            ValueError: If fewer than *count* tokens are past their cooldown, with
                        a message indicating how many are ready vs still cooling down.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            available = [t for t in self._tokens if t.available_at <= t_limit]
            if len(available) < count:
                cooling = len(self._tokens) - len(available)
                raise ValueError(
                    f"PacedResourcePlace '{self.name}': {len(available)} token(s) ready, "
                    f"{count} requested — {cooling} token(s) still in cooldown "
                    f"(pacing_secs={self.pacing_secs})."
                )
            to_return = available[:count]
            remove_ids = {id(t) for t in to_return}
            # O(n) rebuild instead of O(n²) indexed deletion on deque
            self._tokens = deque(t for t in self._tokens if id(t) not in remove_ids)
            return to_return

    def peek(self, count: int = 1, model_time: float | None = None) -> list[Token]:
        """Return up to *count* cooled-down tokens without removing them.

        Behaves identically to [`Place.peek`][cpnx.Place.peek]; "available" specifically
        means "cooldown has expired" for this class. Early-exits once *count* cooled-down
        tokens are collected.

        Args:
            count: Maximum number of tokens to inspect. Defaults to 1.
            model_time: Optional logical clock timestamp used instead of wall-clock time
                        to determine which tokens have finished cooling down.

        Returns:
            List of up to *count* cooled-down tokens; may be shorter than requested.
            Does not modify the pool.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            if count <= 0:
                return []
            result: list[Token] = []
            for t in self._tokens:
                if t.available_at <= t_limit:
                    result.append(t)
                    if len(result) >= count:
                        break
            return result


class ThresholdPlace(Place):
    """A [`Place`][cpnx.Place] where tokens are only retrievable once the queue depth reaches *threshold*.

    **CPN equivalent:** a plain [`Place`][cpnx.Place] whose associated transition has a
    guard requiring ``|M(p)| >= threshold`` before firing. This class is a Python
    shorthand that encodes the threshold directly on the place rather than
    duplicating it in every downstream transition's guard.

    Useful for batch processing: tokens accumulate until enough are present,
    then they are released in groups matching the transition's ``arc.count``.
    `deposit` and `peek` are inherited unchanged from [`Place`][cpnx.Place].

    Example — convene a committee once 6 validated leads are ready:
        ```python
        validated = ThresholdPlace("validated_leads", threshold=6)
        ```
    """

    def __init__(self, name: str, threshold: int) -> None:
        """Create a ThresholdPlace.

        Args:
            name: Unique identifier for this place within a [`PetriNet`][cpnx.PetriNet].
            threshold: Minimum queue depth required before any retrieval is
                       permitted. Must be >= 1.
        """
        super().__init__(name)
        self.threshold = threshold

    def can_retrieve(self, count: int = 1, model_time: float | None = None) -> bool:
        """Return ``True`` only if the batch threshold is met AND at least *count* tokens are present.

        Differs from [`Place.can_retrieve`][cpnx.Place.can_retrieve]: adds a gating condition
        on top of the plain count check — the queue must have reached `threshold` regardless
        of *count*, and separately contain at least *count* available tokens (*count* may
        exceed `threshold`).

        Args:
            count: Number of tokens needed by the requesting transition arc. Defaults to 1.
            model_time: Optional logical clock timestamp used instead of wall-clock time
                        to determine which tokens are available.

        Returns:
            ``True`` if both the threshold and *count* conditions hold, ``False`` otherwise.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            return self._has_available(t_limit, max(self.threshold, count))

    def retrieve(self, count: int = 1, model_time: float | None = None) -> list[Token]:
        """Remove and return *count* tokens from the head of the queue, but only if the threshold is met.

        Differs from [`Place.retrieve`][cpnx.Place.retrieve]: first checks that the queue
        has reached `threshold` available tokens (raising if not) before applying the
        usual *count* check, gating retrieval behind the batch threshold.

        Args:
            count: Number of tokens to retrieve. Defaults to 1.
            model_time: Optional logical clock timestamp used instead of wall-clock time
                        to determine which tokens are available.

        Returns:
            List of retrieved tokens in FIFO order.

        Raises:
            ValueError: If the threshold is not yet met, with a message showing
                        current depth vs required threshold.
            ValueError: If the threshold is met but fewer than *count* tokens are
                        available.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            available = [t for t in self._tokens if t.available_at <= t_limit]
            if len(available) < self.threshold:
                raise ValueError(
                    f"ThresholdPlace '{self.name}': threshold of {self.threshold} not met "
                    f"({len(available)} token(s) available — need {self.threshold - len(available)} more)."
                )
            if len(available) < count:
                raise ValueError(
                    f"ThresholdPlace '{self.name}': threshold met but only {len(available)} "
                    f"token(s) available, {count} requested."
                )
            to_return = available[:count]
            remove_ids = {t.id for t in to_return}
            self._tokens = deque(t for t in self._tokens if t.id not in remove_ids)
            return to_return

    def retrieve_all(self, model_time: float | None = None) -> list[Token]:
        """Remove and return every available token, but only if the threshold has been met.

        Differs from [`Place.retrieve_all`][cpnx.Place.retrieve_all]: raises instead of
        returning an empty list when fewer than `threshold` tokens are available.

        Args:
            model_time: Optional logical clock timestamp used instead of wall-clock time
                        to determine which tokens are available.

        Returns:
            All available tokens in FIFO order.

        Raises:
            ValueError: If the threshold is not yet met, with a message showing
                        current depth vs required threshold.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            available = [t for t in self._tokens if t.available_at <= t_limit]
            if len(available) < self.threshold:
                raise ValueError(
                    f"ThresholdPlace '{self.name}': threshold of {self.threshold} not met "
                    f"({len(available)} token(s) available — need {self.threshold - len(available)} more)."
                )
            remove_ids = {t.id for t in available}
            self._tokens = deque(t for t in self._tokens if t.id not in remove_ids)
            return available


class SinkPlace(Place):
    """A terminal place that counts and optionally samples tokens but never retains them for retrieval.

    Deposited tokens are absorbed: their colour and cumulative counts are recorded, and up
    to `keep_last` of the most recent tokens are kept in a ring buffer purely for inspection
    via `tokens`/`stats`/`drain_stats` — but they can never be consumed onward by a transition.
    Useful for streaming pipelines (e.g. logging sinks, dead-letter/error places) to avoid
    accumulating memory indefinitely while still exposing aggregate statistics.

    Warning:
        Avoid setting a restrictive `color_set` if this place is used as an `error_place`.
        Dead-lettered tokens preserve their original colours, and depositing a rejected
        colour will raise a `TypeError` inside the locked transition failure branch,
        causing the token to be lost rather than successfully dead-lettered.
    """

    def __init__(self, name: str, *, keep_last: int = 0, color_set: set[str] | None = None) -> None:
        """Create a new SinkPlace.

        Args:
            name: Unique identifier for this place within a [`PetriNet`][cpnx.PetriNet].
            keep_last: Number of most recent tokens to keep in a ring buffer for inspection.
                       Default is 0 (retain nothing beyond the aggregate counters).
            color_set: Set of accepted token colours. ``None`` (default) accepts any colour.
                       Do not use a restrictive color_set if used as an error_place.
        """
        super().__init__(name, bound=None, color_set=color_set)
        self.keep_last = keep_last
        self._tokens = deque(maxlen=keep_last)
        self._absorbed = 0
        self._by_color: dict[str | None, int] = {}
        self._first_deposit_time: float | None = None

    def deposit(self, token: Token, model_time: float | None = None) -> None:
        """Absorb *token*: append it to the ring buffer and update cumulative counters and timestamps.

        Differs from [`Place.deposit`][cpnx.Place.deposit]: the internal deque has
        `maxlen=keep_last`, so once full, appending silently evicts the oldest kept
        token — this is a sampling buffer, not the full token history. Also increments
        the `_absorbed` count and the per-colour tally, and records `_first_deposit_time`
        on the very first deposit.

        Args:
            token: The token to absorb.
            model_time: Optional logical clock timestamp recorded in `last_deposit_time_model`.

        Raises:
            TypeError: If `color_set` is set and *token*'s colour is not in it.
        """
        with self._lock:
            if self.color_set is not None and token.color not in self.color_set:
                raise TypeError(
                    f"Place '{self.name}' has color_set {self.color_set!r} — "
                    f"cannot deposit token with color {token.color!r}."
                )
            self._tokens.append(token)
            now = time.monotonic()
            self.last_deposit_time = now
            if self._first_deposit_time is None:
                self._first_deposit_time = now
            if model_time is not None:
                self.last_deposit_time_model = model_time

            self._absorbed += 1
            self._by_color[token.color] = self._by_color.get(token.color, 0) + 1

            self._on_deposit(token)

    def can_retrieve(self, count: int = 1, model_time: float | None = None) -> bool:
        """Always return ``False``: a sink is a terminal place, so nothing is ever retrievable.

        Differs from [`Place.can_retrieve`][cpnx.Place.can_retrieve]: arriving tokens are
        absorbed for inspection/counting only and can never be consumed onward by a
        transition, so this unconditionally reports nothing is retrievable. *count* and
        *model_time* are accepted for interface compatibility but ignored.

        Args:
            count: Ignored.
            model_time: Ignored.

        Returns:
            ``False``, always.
        """
        return False

    def retrieve(self, count: int = 1, model_time: float | None = None) -> list[Token]:
        """Always raise: a sink is terminal, so tokens cannot be retrieved by any means.

        Differs from [`Place.retrieve`][cpnx.Place.retrieve]: never returns tokens, since
        absorbed tokens are only for inspection, not downstream consumption. *count* and
        *model_time* are accepted for interface compatibility but ignored.

        Args:
            count: Ignored.
            model_time: Ignored.

        Raises:
            ValueError: Always, with message "SinkPlace is terminal — tokens are absorbed,
                        not retrievable".
        """
        raise ValueError("SinkPlace is terminal — tokens are absorbed, not retrievable")

    def retrieve_specific(self, tokens: list[Token], model_time: float | None = None) -> list[Token]:
        """Always raise: a sink is terminal, so no tokens — specific or otherwise — can be retrieved.

        Differs from [`Place.retrieve_specific`][cpnx.Place.retrieve_specific]: never
        returns tokens, since absorbed tokens are only for inspection. *tokens* and
        *model_time* are accepted for interface compatibility but ignored.

        Args:
            tokens: Ignored.
            model_time: Ignored.

        Raises:
            ValueError: Always, with message "SinkPlace is terminal — tokens are absorbed,
                        not retrievable".
        """
        raise ValueError("SinkPlace is terminal — tokens are absorbed, not retrievable")

    def retrieve_all(self, model_time: float | None = None) -> list[Token]:
        """Always raise: a sink is terminal, so its absorbed tokens can never be drained via retrieval.

        Differs from [`Place.retrieve_all`][cpnx.Place.retrieve_all]: never returns tokens.
        Use `drain_stats` to reset the aggregate counters instead. *model_time* is accepted
        for interface compatibility but ignored.

        Args:
            model_time: Ignored.

        Raises:
            ValueError: Always, with message "SinkPlace is terminal — tokens are absorbed,
                        not retrievable".
        """
        raise ValueError("SinkPlace is terminal — tokens are absorbed, not retrievable")

    def peek(self, count: int = 1, model_time: float | None = None) -> list[Token]:
        """Always raise: use the `tokens` property to inspect a sink's ring buffer instead.

        Differs from [`Place.peek`][cpnx.Place.peek]: rather than returning a possibly-empty
        list, this raises, since a sink's kept tokens are only meant to be read via `tokens`
        or `stats`. *count* and *model_time* are accepted for interface compatibility but
        ignored.

        Args:
            count: Ignored.
            model_time: Ignored.

        Raises:
            ValueError: Always, with message "SinkPlace is terminal — tokens are absorbed,
                        not retrievable".
        """
        raise ValueError("SinkPlace is terminal — tokens are absorbed, not retrievable")

    def can_deposit(self, count: int = 1) -> bool:
        """Always return ``True``: a sink has unbounded capacity and absorbs every token offered.

        Differs from [`Place.can_deposit`][cpnx.Place.can_deposit]: ignores `bound`
        entirely (a `SinkPlace` is constructed with ``bound=None`` and never rejects
        on capacity grounds — only `color_set` can reject a deposit).

        Args:
            count: Ignored.

        Returns:
            ``True``, always.
        """
        return True

    def stats(self) -> dict:
        """Return a snapshot of cumulative statistics of absorbed tokens, without resetting any counters.

        Example:
            ```python
            sink = SinkPlace("errors", keep_last=10)
            sink.deposit(Token(color="error"))
            sink.stats()
            # {"name": "errors", "absorbed": 1, "by_color": {"error": 1}, "kept": 1,
            #  "first_deposit_time": ..., "last_deposit_time": ...}
            ```

        Returns:
            Dictionary with keys ``name`` (str), ``absorbed`` (total tokens ever deposited,
            int), ``by_color`` (dict mapping colour to count), ``kept`` (number of tokens
            currently in the ring buffer), ``first_deposit_time`` (float or ``None`` if
            nothing has been deposited), and ``last_deposit_time`` (float, ``0.0`` if
            nothing has been deposited).
        """
        with self._lock:
            return {
                "name": self.name,
                "absorbed": self._absorbed,
                "by_color": dict(self._by_color),
                "kept": len(self._tokens),
                "first_deposit_time": self._first_deposit_time,
                "last_deposit_time": self.last_deposit_time,
            }

    def drain_stats(self) -> dict:
        """Atomically return the current stats snapshot and reset the cumulative counters to zero.

        Differs from `stats`: after returning the snapshot, resets `_absorbed` to 0,
        `_by_color` to an empty dict, and `_first_deposit_time` to ``None`` (the ring buffer
        of kept tokens and `last_deposit_time` are left untouched). Useful for periodic
        reporting where each report should cover only the interval since the last drain.

        Returns:
            The stats dictionary as it was immediately before the reset — same shape as
            `stats` (``name``, ``absorbed``, ``by_color``, ``kept``, ``first_deposit_time``,
            ``last_deposit_time``).
        """
        with self._lock:
            snapshot = {
                "name": self.name,
                "absorbed": self._absorbed,
                "by_color": dict(self._by_color),
                "kept": len(self._tokens),
                "first_deposit_time": self._first_deposit_time,
                "last_deposit_time": self.last_deposit_time,
            }
            self._absorbed = 0
            self._by_color = {}
            self._first_deposit_time = None
            return snapshot
