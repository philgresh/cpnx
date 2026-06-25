import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import uuid4


class FrozenDict(Mapping):
    """An immutable dictionary wrapper that recursively freezes nested dicts and lists."""

    def __init__(self, data: Mapping | None = None, **kwargs) -> None:
        self._data = {}
        if data is not None:
            for k, v in data.items():
                self._data[k] = self._freeze(v)
        for k, v in kwargs.items():
            self._data[k] = self._freeze(v)
        self._hash = None

    def _freeze(self, val: Any) -> Any:
        if isinstance(val, (dict, Mapping)):
            return FrozenDict(val)
        if isinstance(val, list):
            return tuple(self._freeze(item) for item in val)
        return val

    def __getitem__(self, key: Any) -> Any:
        return self._data[key]

    def __len__(self) -> int:
        return len(self._data)

    def __iter__(self):
        return iter(self._data)

    def __hash__(self) -> int:
        if self._hash is None:
            self._hash = hash(frozenset(self._data.items()))
        return self._hash

    def __repr__(self) -> str:
        return f"FrozenDict({self._data!r})"

    def copy(self) -> dict:
        return self._data.copy()

    def set(self, key: Any, value: Any) -> "FrozenDict":
        """Functional update: return a new FrozenDict with key=value."""
        new_d = dict(self._data)
        new_d[key] = value
        return FrozenDict(new_d)


@dataclass(frozen=True)
class Token:
    """An immutable token flowing through a Petri net.

    In Coloured Petri Net (CPN) theory, a token is a value drawn from its
    place's colour set. Here, ``color`` is that colour: ``None`` for uncoloured
    data tokens, ``"resource"`` for permit/resource tokens, or any user-defined
    string for domain-specific colours.

    Attributes:
        id: Unique identifier (16-char hex). Auto-generated.
        payload: Immutable dict accumulating enrichment data as the token traverses
                 the net. Not used for resource tokens.
        created_at: Monotonic timestamp set at construction.
        color: CPN colour. ``None`` = uncoloured data token;
               ``"resource"`` = permit token (see :class:`~petriq.places.ResourcePlace`);
               any other string = user-defined colour.
        available_at: Monotonic timestamp after which the token is available (timed CPNs).
    """

    id: str = field(default_factory=lambda: uuid4().hex[:16])
    payload: FrozenDict = field(default_factory=FrozenDict)
    created_at: float = field(default_factory=time.monotonic)
    color: str | None = None
    available_at: float = 0.0

    def __post_init__(self):
        if not isinstance(self.payload, FrozenDict):
            object.__setattr__(self, "payload", FrozenDict(self.payload))

    @property
    def is_resource(self) -> bool:
        """True when this token's colour is ``"resource"``.

        Python-friendly shorthand for ``token.color == "resource"``.
        """
        return self.color == "resource"

    def evolve(self, payload_updates: dict[str, Any] | None = None, **field_updates) -> "Token":
        """Construct a new Token instance, merging payload updates and overriding fields."""
        new_fields = field_updates
        if payload_updates is not None:
            merged_payload = dict(self.payload)
            merged_payload.update(payload_updates)
            new_fields["payload"] = FrozenDict(merged_payload)
        return replace(self, **new_fields)
