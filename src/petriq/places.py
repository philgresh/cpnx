"""Place types for the petriq Petri net executor.

All place classes are thread-safe. Choose the right type for your use case:

- :class:`Place`              — unbounded FIFO queue for data / work tokens
- :class:`ResourcePlace`      — bounded permit pool (GPU slots, DB connections)
- :class:`PacedResourcePlace` — permit pool with per-token cooldown (API rate limits)
- :class:`ThresholdPlace`     — accumulates tokens until a batch threshold is met

**CPN alignment:** In Coloured Petri Net theory a place has a *colour set* —
the type of tokens it accepts. :attr:`Place.color_set` exposes this directly.
:class:`ResourcePlace` and :class:`PacedResourcePlace` are Python shorthands for
a place whose colour set is ``{"resource"}`` with a pre-filled initial marking.
"""

import threading
import time
from collections import deque

from petriq.tokens import Token


class Place:
    """An unbounded FIFO queue that holds tokens flowing through a Petri net.

    **CPN equivalent:** a place with an unrestricted colour set (accepts any
    colour) and no initial marking. Set ``color_set`` to restrict accepted
    colours; set ``initial_marking`` to pre-fill with tokens at construction.

    All operations are thread-safe via an internal :class:`threading.Lock`.
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
            name: Unique identifier for this place within a :class:`PetriNet`.
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

        for token in initial_marking or []:
            self._tokens.append(token)
            self.last_deposit_time = time.monotonic()

    def deposit(self, token: Token, model_time: float | None = None) -> None:
        """Append *token* to the tail of the queue.

        Updates :attr:`last_deposit_time` and calls :meth:`_on_deposit`.

        Args:
            token: The token to deposit.
            model_time: Optional logical clock timestamp.
        """
        with self._lock:
            if self.color_set is not None and token.color not in self.color_set:
                raise TypeError(
                    f"Place '{self.name}' has color_set {self.color_set!r} — "
                    f"cannot deposit token with color {token.color!r}."
                )
            self._tokens.append(token)
            self.last_deposit_time = time.monotonic()
            self._on_deposit(token)

    def _on_deposit(self, token: Token) -> None:
        """Extension hook called immediately after a token is deposited (under the place lock).

        Override in subclasses to react to new arrivals. Default is a no-op.

        Args:
            token: The token that was just deposited.
        """

    def retrieve(self, count: int = 1, model_time: float | None = None) -> list[Token]:
        """Remove and return *count* tokens from the head of the queue (FIFO order).

        Args:
            count: Number of tokens to retrieve. Must be ≥ 1.
            model_time: Optional logical clock timestamp to filter available tokens.

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
            remove_ids = {t.id for t in to_return}
            self._tokens = deque(t for t in self._tokens if t.id not in remove_ids)
            return to_return

    def retrieve_specific(self, tokens: list[Token], model_time: float | None = None) -> list[Token]:
        """Remove and return exactly the tokens in *tokens* (matched by ``id``).

        Used by the engine when an :class:`~petriq.transitions.InputArc` has an
        ``expression`` that selects a specific subset of tokens to consume.
        Uses an O(n) deque rebuild.

        Args:
            tokens: Tokens to remove. Each must be present in this place.
            model_time: Optional logical clock timestamp to filter available tokens.

        Returns:
            The removed tokens in the order given by *tokens*.

        Raises:
            ValueError: If any token id in *tokens* is not found in this place or is not available.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            for t in tokens:
                if t.available_at > t_limit:
                    raise ValueError(
                        f"Place '{self.name}': token {t.id} is not yet available at {t_limit} "
                        f"(available_at={t.available_at})."
                    )
            remove_ids = {t.id for t in tokens}
            present_ids = {t.id for t in self._tokens}
            missing = remove_ids - present_ids
            if missing:
                raise ValueError(
                    f"Place '{self.name}': token id(s) {missing} not found — "
                    f"cannot retrieve_specific tokens that are not present."
                )
            self._tokens = deque(t for t in self._tokens if t.id not in remove_ids)
            return tokens

    def retrieve_all(self, model_time: float | None = None) -> list[Token]:
        """Remove and return every token currently in the place.

        Args:
            model_time: Optional logical clock timestamp to filter available tokens.

        Returns:
            All tokens in FIFO order; empty list if the place is empty.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            available = [t for t in self._tokens if t.available_at <= t_limit]
            remove_ids = {t.id for t in available}
            self._tokens = deque(t for t in self._tokens if t.id not in remove_ids)
            return available

    def peek(self, count: int = 1, model_time: float | None = None) -> list[Token]:
        """Return up to *count* tokens from the head without removing them.

        Args:
            count: Maximum number of tokens to inspect.
            model_time: Optional logical clock timestamp to filter available tokens.

        Returns:
            List of up to *count* tokens; may be shorter than requested if fewer
            are present. Does not modify the queue.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            available = [t for t in self._tokens if t.available_at <= t_limit]
            return available[:count]

    def can_retrieve(self, count: int = 1, model_time: float | None = None) -> bool:
        """Return ``True`` if at least *count* tokens are available for retrieval.

        Args:
            count: Number of tokens needed.
            model_time: Optional logical clock timestamp to filter available tokens.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            available = sum(1 for t in self._tokens if t.available_at <= t_limit)
            return available >= count

    def can_deposit(self, count: int = 1) -> bool:
        """Return ``True`` if the place can accept *count* more tokens without exceeding its bound.

        Implements k-bounded place semantics: a place with ``bound=k`` blocks when
        depositing would push the token count above ``k``. Unbounded places
        (``bound=None``) always return ``True``.

        Args:
            count: Number of tokens to be deposited.
        """
        with self._lock:
            if self.bound is None:
                return True
            return len(self._tokens) + count <= self.bound

    def can_accept(self, token: Token) -> bool:
        """Return ``True`` if the place can accept the token without violating colour sets.

        This is a non-mutating pre-flight check that does not modify the place's tokens.
        """
        with self._lock:
            if self.color_set is not None and token.color not in self.color_set:
                return False
            return True

    @property
    def tokens(self) -> tuple[Token, ...]:
        """Snapshot of current tokens as an immutable tuple (does not consume them)."""
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
    """A bounded resource-permit pool pre-filled with *capacity* resource tokens.

    **CPN equivalent:** ``Place(color_set={"resource"}, initial_marking=[Token(color="resource")] * capacity)``.
    This class is a Python shorthand — it sets the colour set and initial marking
    automatically and documents the resource-return invariant explicitly.

    Resource tokens (``color="resource"``) are consumed when a transition fires
    and must be returned via a matching output arc. This models finite resources
    such as GPU slots, database connections, or thread-pool permits.
    """

    def __init__(self, name: str, capacity: int) -> None:
        """Create a ResourcePlace pre-filled with *capacity* resource tokens.

        Args:
            name: Unique identifier for this place within a :class:`PetriNet`.
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
    """A resource pool where returned tokens must cool down before becoming reusable.

    **CPN equivalent:** a Timed CPN :class:`ResourcePlace` where returned tokens
    carry a timestamp that prevents re-use until ``pacing_secs`` have elapsed.
    This is a pragmatic extension — standard Timed CPNs put timestamps on tokens,
    not cooldown windows on places.

    Useful for enforcing API rate limits or minimum inter-request intervals.
    Tokens are available immediately at construction; after each return via
    :meth:`deposit`, they are unavailable for *pacing_secs* seconds.

    Example — 10 Serper requests per second::

        serper = PacedResourcePlace("serper", capacity=10, pacing_secs=0.1)
    """

    def __init__(self, name: str, capacity: int, pacing_secs: float) -> None:
        """Create a PacedResourcePlace.

        Args:
            name: Unique identifier for this place within a :class:`PetriNet`.
            capacity: Number of resource permits in the pool.
            pacing_secs: Seconds a token must wait after being returned before
                         it becomes available again.
        """
        self.pacing_secs = pacing_secs
        super().__init__(name, capacity)

    def deposit(self, token: Token, model_time: float | None = None) -> None:
        """Return a resource token to the pool, starting its cooldown timer.

        The token will not be retrievable until ``pacing_secs`` have elapsed.

        Args:
            token: The resource token being returned. Must have ``color="resource"``.
            model_time: Optional logical clock timestamp.
        """
        with self._lock:
            ref_time = model_time if model_time is not None else time.monotonic()
            # Create a new token with updated availability timestamp (stateless place cooldown)
            timed_token = token.evolve(available_at=ref_time + self.pacing_secs, id=token.id)
            self._tokens.append(timed_token)
            self.last_deposit_time = time.monotonic()
            self._on_deposit(timed_token)

    def can_retrieve(self, count: int = 1, model_time: float | None = None) -> bool:
        """Return ``True`` if at least *count* tokens have completed their cooldown.

        Args:
            count: Number of cooled-down tokens needed.
            model_time: Optional logical clock timestamp to filter available tokens.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            available = sum(1 for t in self._tokens if t.available_at <= t_limit)
            return available >= count

    def retrieve(self, count: int = 1, model_time: float | None = None) -> list[Token]:
        """Remove and return *count* tokens whose cooldown has expired.

        Uses an O(n) rebuild rather than O(n²) indexed deletion.

        Args:
            count: Number of cooled-down tokens to retrieve.
            model_time: Optional logical clock timestamp to filter available tokens.

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
            remove_ids = {t.id for t in to_return}
            # O(n) rebuild instead of O(n²) indexed deletion on deque
            self._tokens = deque(t for t in self._tokens if t.id not in remove_ids)
            return to_return

    def peek(self, count: int = 1, model_time: float | None = None) -> list[Token]:
        """Return up to *count* cooled-down tokens without removing them.

        Args:
            count: Maximum number of tokens to inspect.
            model_time: Optional logical clock timestamp to filter available tokens.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            available = [t for t in self._tokens if t.available_at <= t_limit]
            return available[:count]


class ThresholdPlace(Place):
    """A FIFO place where tokens are only retrievable once the queue depth reaches *threshold*.

    **CPN equivalent:** a plain :class:`Place` whose associated transition has a
    guard requiring ``|M(p)| >= threshold`` before firing. This class is a Python
    shorthand that encodes the threshold directly on the place rather than
    duplicating it in every downstream transition's guard.

    Useful for batch processing: tokens accumulate until enough are present,
    then they are released in groups matching the transition's ``arc.count``.

    Example — convene a committee once 6 validated leads are ready::

        validated = ThresholdPlace("validated_leads", threshold=6)
    """

    def __init__(self, name: str, threshold: int) -> None:
        """Create a ThresholdPlace.

        Args:
            name: Unique identifier for this place within a :class:`PetriNet`.
            threshold: Minimum queue depth required before any retrieval is
                       permitted. Must be ≥ 1.
        """
        super().__init__(name)
        self.threshold = threshold

    def can_retrieve(self, count: int = 1, model_time: float | None = None) -> bool:
        """Return ``True`` if the threshold is met AND at least *count* tokens are present.

        Both conditions must hold: the queue must have reached its threshold AND
        contain at least *count* tokens (count may exceed the threshold).

        Args:
            count: Number of tokens needed by the requesting transition arc.
            model_time: Optional logical clock timestamp to filter available tokens.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            available = [t for t in self._tokens if t.available_at <= t_limit]
            return len(available) >= self.threshold and len(available) >= count

    def retrieve(self, count: int = 1, model_time: float | None = None) -> list[Token]:
        """Remove and return *count* tokens if the threshold has been met.

        Args:
            count: Number of tokens to retrieve.
            model_time: Optional logical clock timestamp to filter available tokens.

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
        """Remove and return all tokens if the threshold has been met.

        Args:
            model_time: Optional logical clock timestamp to filter available tokens.

        Returns:
            All tokens in FIFO order.

        Raises:
            ValueError: If the threshold is not yet met.
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
