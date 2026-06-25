import threading
import time
from collections import deque

from petriq.tokens import Token


class Place:
    def __init__(self, name: str):
        self.name = name
        self._tokens: deque[Token] = deque()
        self._lock = threading.Lock()
        self.last_deposit_time: float = 0.0

    def deposit(self, token: Token) -> None:
        with self._lock:
            self._tokens.append(token)
            self.last_deposit_time = time.monotonic()
            self._on_deposit(token)

    def _on_deposit(self, token: Token) -> None:
        pass

    def retrieve(self, count: int = 1) -> list[Token]:
        with self._lock:
            if len(self._tokens) < count:
                raise ValueError("Not enough tokens to retrieve")
            return [self._tokens.popleft() for _ in range(count)]

    def retrieve_all(self) -> list[Token]:
        with self._lock:
            ret = list(self._tokens)
            self._tokens.clear()
            return ret

    def peek(self, count: int = 1) -> list[Token]:
        with self._lock:
            return list(self._tokens)[:count]

    def can_retrieve(self, count: int = 1) -> bool:
        with self._lock:
            return len(self._tokens) >= count

    @property
    def tokens(self) -> list[Token]:
        with self._lock:
            return list(self._tokens)


class ResourcePlace(Place):
    def __init__(self, name: str, capacity: int):
        super().__init__(name)
        self.capacity = capacity
        # Pre-fill with capacity resource tokens
        for _ in range(capacity):
            self._tokens.append(Token(is_resource=True))


class PacedResourcePlace(ResourcePlace):
    def __init__(self, name: str, capacity: int, pacing_secs: float):
        self.pacing_secs = pacing_secs
        self._cooldowns: dict[str, float] = {}  # token_id -> float timestamp when available
        super().__init__(name, capacity)
        # Initial capacity tokens are available immediately (cooldown available_at = 0.0)
        for t in self._tokens:
            self._cooldowns[t.id] = 0.0

    def deposit(self, token: Token) -> None:
        with self._lock:
            self._tokens.append(token)
            self.last_deposit_time = time.monotonic()
            self._cooldowns[token.id] = self.last_deposit_time + self.pacing_secs

    def can_retrieve(self, count: int = 1) -> bool:
        with self._lock:
            now = time.monotonic()
            available_count = sum(1 for t in self._tokens if self._cooldowns.get(t.id, 0.0) <= now)
            return available_count >= count

    def retrieve(self, count: int = 1) -> list[Token]:
        with self._lock:
            now = time.monotonic()
            indices_to_remove = []
            for i, t in enumerate(self._tokens):
                if self._cooldowns.get(t.id, 0.0) <= now:
                    indices_to_remove.append(i)
                    if len(indices_to_remove) == count:
                        break
            if len(indices_to_remove) < count:
                raise ValueError("Not enough available paced tokens to retrieve")

            retrieved = []
            # Remove in reverse index order to avoid shifting indices of elements to remove
            for idx in reversed(indices_to_remove):
                t = self._tokens[idx]
                retrieved.append(t)
                del self._tokens[idx]
                self._cooldowns.pop(t.id, None)

            retrieved.reverse()
            return retrieved

    def peek(self, count: int = 1) -> list[Token]:
        with self._lock:
            now = time.monotonic()
            available = [t for t in self._tokens if self._cooldowns.get(t.id, 0.0) <= now]
            return available[:count]


class ThresholdPlace(Place):
    def __init__(self, name: str, threshold: int):
        super().__init__(name)
        self.threshold = threshold

    def can_retrieve(self, count: int = 1) -> bool:
        with self._lock:
            # Tokens only consumable when len(tokens) >= threshold
            return len(self._tokens) >= self.threshold

    def retrieve(self, count: int = 1) -> list[Token]:
        with self._lock:
            if len(self._tokens) < self.threshold:
                raise ValueError("Cannot retrieve tokens: threshold not met")
            if len(self._tokens) < count:
                raise ValueError("Not enough tokens to retrieve")
            return [self._tokens.popleft() for _ in range(count)]

    def retrieve_all(self) -> list[Token]:
        with self._lock:
            if len(self._tokens) < self.threshold:
                raise ValueError("Cannot retrieve tokens: threshold not met")
            ret = list(self._tokens)
            self._tokens.clear()
            return ret
