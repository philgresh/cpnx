"""Place types for the petriq Petri net executor.

All place classes are thread-safe. Choose the right type for your use case:

- :class:`Place`              — unbounded FIFO queue for data / work tokens
- :class:`ResourcePlace`      — bounded permit pool (GPU slots, DB connections)
- :class:`PacedResourcePlace` — permit pool with per-token cooldown (API rate limits)
- :class:`ThresholdPlace`     — accumulates tokens until a batch threshold is met
"""

import threading
import time
from collections import deque

from petriq.tokens import Token


class Place:
    """An unbounded FIFO queue that holds tokens flowing through a Petri net.

    All operations are thread-safe via an internal :class:`threading.Lock`.
    """

    def __init__(self, name: str) -> None:
        """Create a new Place.

        Args:
            name: Unique identifier for this place within a :class:`PetriNet`.
        """
        self.name = name
        self._tokens: deque[Token] = deque()
        self._lock = threading.Lock()
        self.last_deposit_time: float = 0.0

    def deposit(self, token: Token) -> None:
        """Append *token* to the tail of the queue.

        Updates :attr:`last_deposit_time` and calls :meth:`_on_deposit`.

        Args:
            token: The token to deposit.
        """
        with self._lock:
            self._tokens.append(token)
            self.last_deposit_time = time.monotonic()
            self._on_deposit(token)

    def _on_deposit(self, token: Token) -> None:
        """Extension hook called immediately after a token is deposited (under the place lock).

        Override in subclasses to react to new arrivals. Default is a no-op.

        Args:
            token: The token that was just deposited.
        """

    def retrieve(self, count: int = 1) -> list[Token]:
        """Remove and return *count* tokens from the head of the queue (FIFO order).

        Args:
            count: Number of tokens to retrieve. Must be ≥ 1.

        Returns:
            List of retrieved tokens in FIFO order.

        Raises:
            ValueError: If fewer than *count* tokens are present.
        """
        with self._lock:
            if len(self._tokens) < count:
                raise ValueError(
                    f"Place '{self.name}': cannot retrieve {count} token(s) — "
                    f"only {len(self._tokens)} available."
                )
            return [self._tokens.popleft() for _ in range(count)]

    def retrieve_all(self) -> list[Token]:
        """Remove and return every token currently in the place.

        Returns:
            All tokens in FIFO order; empty list if the place is empty.
        """
        with self._lock:
            ret = list(self._tokens)
            self._tokens.clear()
            return ret

    def peek(self, count: int = 1) -> list[Token]:
        """Return up to *count* tokens from the head without removing them.

        Args:
            count: Maximum number of tokens to inspect.

        Returns:
            List of up to *count* tokens; may be shorter than requested if fewer
            are present. Does not modify the queue.
        """
        with self._lock:
            return list(self._tokens)[:count]

    def can_retrieve(self, count: int = 1) -> bool:
        """Return ``True`` if at least *count* tokens are available for retrieval.

        Args:
            count: Number of tokens needed.
        """
        with self._lock:
            return len(self._tokens) >= count

    @property
    def tokens(self) -> list[Token]:
        """Snapshot of current tokens as a list copy (does not consume them)."""
        with self._lock:
            return list(self._tokens)


class ResourcePlace(Place):
    """A bounded resource-permit pool pre-filled with *capacity* resource tokens.

    Resource tokens (``is_resource=True``) are consumed when a transition fires
    and must be returned via a matching output arc. This models finite resources
    such as GPU slots, database connections, or thread-pool permits.

    The pool is initialised with exactly *capacity* tokens. Do not deposit plain
    data tokens here — the engine routes resource tokens based on
    :attr:`~petriq.tokens.Token.is_resource`.
    """

    def __init__(self, name: str, capacity: int) -> None:
        """Create a ResourcePlace pre-filled with *capacity* resource tokens.

        Args:
            name: Unique identifier for this place within a :class:`PetriNet`.
            capacity: Number of resource permits in the pool. ``0`` is valid
                      (creates an empty, permanently-blocking place).
        """
        super().__init__(name)
        self.capacity = capacity
        for _ in range(capacity):
            self._tokens.append(Token(is_resource=True))


class PacedResourcePlace(ResourcePlace):
    """A resource pool where returned tokens must cool down before becoming reusable.

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
        self._cooldowns: dict[str, float] = {}  # token_id → monotonic timestamp when available
        super().__init__(name, capacity)
        # All tokens available immediately at construction
        for t in self._tokens:
            self._cooldowns[t.id] = 0.0

    def deposit(self, token: Token) -> None:
        """Return a resource token to the pool, starting its cooldown timer.

        The token will not be retrievable until ``pacing_secs`` have elapsed.

        Args:
            token: The resource token being returned. Must have ``is_resource=True``.
        """
        with self._lock:
            self._tokens.append(token)
            self.last_deposit_time = time.monotonic()
            self._cooldowns[token.id] = self.last_deposit_time + self.pacing_secs
            self._on_deposit(token)

    def can_retrieve(self, count: int = 1) -> bool:
        """Return ``True`` if at least *count* tokens have completed their cooldown.

        Args:
            count: Number of cooled-down tokens needed.
        """
        with self._lock:
            now = time.monotonic()
            available = sum(1 for t in self._tokens if self._cooldowns.get(t.id, 0.0) <= now)
            return available >= count

    def retrieve(self, count: int = 1) -> list[Token]:
        """Remove and return *count* tokens whose cooldown has expired.

        Uses an O(n) rebuild rather than O(n²) indexed deletion.

        Args:
            count: Number of cooled-down tokens to retrieve.

        Returns:
            List of retrieved resource tokens in cooldown-expiry order.

        Raises:
            ValueError: If fewer than *count* tokens are past their cooldown, with
                        a message indicating how many are ready vs still cooling down.
        """
        with self._lock:
            now = time.monotonic()
            available = [t for t in self._tokens if self._cooldowns.get(t.id, 0.0) <= now]
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
            for t in to_return:
                self._cooldowns.pop(t.id, None)
            return to_return

    def peek(self, count: int = 1) -> list[Token]:
        """Return up to *count* cooled-down tokens without removing them.

        Args:
            count: Maximum number of tokens to inspect.
        """
        with self._lock:
            now = time.monotonic()
            available = [t for t in self._tokens if self._cooldowns.get(t.id, 0.0) <= now]
            return available[:count]


class ThresholdPlace(Place):
    """A FIFO place where tokens are only retrievable once the queue depth reaches *threshold*.

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

    def can_retrieve(self, count: int = 1) -> bool:
        """Return ``True`` if the threshold is met AND at least *count* tokens are present.

        Both conditions must hold: the queue must have reached its threshold AND
        contain at least *count* tokens (count may exceed the threshold).

        Args:
            count: Number of tokens needed by the requesting transition arc.
        """
        with self._lock:
            return len(self._tokens) >= self.threshold and len(self._tokens) >= count

    def retrieve(self, count: int = 1) -> list[Token]:
        """Remove and return *count* tokens if the threshold has been met.

        Args:
            count: Number of tokens to retrieve.

        Returns:
            List of retrieved tokens in FIFO order.

        Raises:
            ValueError: If the threshold is not yet met, with a message showing
                        current depth vs required threshold.
            ValueError: If the threshold is met but fewer than *count* tokens are
                        available.
        """
        with self._lock:
            if len(self._tokens) < self.threshold:
                raise ValueError(
                    f"ThresholdPlace '{self.name}': threshold of {self.threshold} not met "
                    f"({len(self._tokens)} token(s) present — need {self.threshold - len(self._tokens)} more)."
                )
            if len(self._tokens) < count:
                raise ValueError(
                    f"ThresholdPlace '{self.name}': threshold met but only {len(self._tokens)} "
                    f"token(s) available, {count} requested."
                )
            return [self._tokens.popleft() for _ in range(count)]

    def retrieve_all(self) -> list[Token]:
        """Remove and return all tokens if the threshold has been met.

        Returns:
            All tokens in FIFO order.

        Raises:
            ValueError: If the threshold is not yet met.
        """
        with self._lock:
            if len(self._tokens) < self.threshold:
                raise ValueError(
                    f"ThresholdPlace '{self.name}': threshold of {self.threshold} not met "
                    f"({len(self._tokens)} token(s) present — need {self.threshold - len(self._tokens)} more)."
                )
            ret = list(self._tokens)
            self._tokens.clear()
            return ret
