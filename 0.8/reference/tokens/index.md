# Tokens

Immutable values that flow through the net, plus the sentinels and frozen mapping used to describe them.

## cpnx.Token

An immutable, hashable-by-identity token that flows through a Petri net.

In Coloured Petri Net (CPN) theory, a token is a value drawn from its place's colour set. Here, `color` is that colour: `None` for uncoloured data tokens, `"resource"` for permit/resource tokens, or any user-defined string for domain-specific colours. Being a frozen dataclass, a `Token` is never mutated in place; state changes are made by creating a new instance via evolve.

Attributes:

| Name           | Type         | Description                                                                                                                                                              |
| -------------- | ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `id`           | `str`        | Unique identifier (16-char hex string). Auto-generated from uuid4() at construction time.                                                                                |
| `payload`      | `FrozenDict` | Immutable dict accumulating enrichment data as the token traverses the net. Coerced to a FrozenDict after construction if not already one. Not used for resource tokens. |
| `created_at`   | `float`      | Monotonic timestamp (from time.monotonic()) set at construction.                                                                                                         |
| `color`        | \`str        | None\`                                                                                                                                                                   |
| `available_at` | `float`      | Monotonic timestamp after which the token is available (timed CPNs). Defaults to AVAILABLE_NOW (0.0), which denotes immediate availability.                              |
| `attempts`     | `int`        | Number of failed firings this token has been rolled back from.                                                                                                           |

Source code in `src/cpnx/tokens.py`

````
@dataclass(frozen=True)
class Token:
    """An immutable, hashable-by-identity token that flows through a Petri net.

    In Coloured Petri Net (CPN) theory, a token is a value drawn from its
    place's colour set. Here, `color` is that colour: `None` for uncoloured
    data tokens, `"resource"` for permit/resource tokens, or any user-defined
    string for domain-specific colours. Being a frozen dataclass, a `Token` is
    never mutated in place; state changes are made by creating a new instance
    via [`evolve`][cpnx.Token.evolve].

    Attributes:
        id: Unique identifier (16-char hex string). Auto-generated from `uuid4()` at
            construction time.
        payload: Immutable dict accumulating enrichment data as the token traverses
            the net. Coerced to a [`FrozenDict`][cpnx.FrozenDict] after construction if
            not already one. Not used for resource tokens.
        created_at: Monotonic timestamp (from `time.monotonic()`) set at construction.
        color: CPN colour. `None` = uncoloured data token;
            `"resource"` = permit token (see [`ResourcePlace`][cpnx.ResourcePlace]);
            any other string = user-defined colour.
        available_at: Monotonic timestamp after which the token is available (timed CPNs).
            Defaults to [`AVAILABLE_NOW`][cpnx.AVAILABLE_NOW] (`0.0`), which denotes
            immediate availability.
        attempts: Number of failed firings this token has been rolled back from.
    """

    id: str = field(default_factory=lambda: uuid4().hex[:16])
    payload: FrozenDict = field(default_factory=FrozenDict)
    created_at: float = field(default_factory=time.monotonic)
    color: str | None = None
    available_at: float = AVAILABLE_NOW
    attempts: int = 0

    def __post_init__(self):
        if not isinstance(self.payload, FrozenDict):
            object.__setattr__(self, "payload", FrozenDict(self.payload))

    @property
    def is_resource(self) -> bool:
        """Report whether this token's colour is `"resource"`.

        Python-friendly shorthand for `token.color == "resource"`.

        Returns:
            `True` if `color` equals `"resource"`, `False` otherwise.
        """
        return self.color == "resource"

    def evolve(self, payload_updates: dict[str, Any] | None = None, **field_updates) -> "Token":
        """Create a new `Token` by merging payload updates and overriding fields.

        Since `Token` is immutable, this is the standard way to derive an updated
        token as it moves through the net. A fresh `id` is generated for the new
        token unless `id` is explicitly passed in `field_updates`.

        Args:
            payload_updates: Optional dict merged on top of the existing `payload`
                (via `dict.update`); keys not present are left unchanged. If `None`
                (default), `payload` is left as-is (unless overridden via `field_updates`).
            **field_updates (Any): Additional dataclass field values (e.g. `color`,
                `available_at`, `attempts`) to override on the new instance.

        Returns:
            A new `Token` instance with the merged payload and overridden fields;
            the original token is left unchanged.

        Example:
            ```python
            t = Token(payload={"count": 1})
            t2 = t.evolve(payload_updates={"count": 2}, attempts=1)
            # t2.payload == {"count": 2}, t2.attempts == 1, t2.id != t.id
            ```
        """
        new_fields = field_updates
        if payload_updates is not None:
            merged_payload = dict(self.payload)
            merged_payload.update(payload_updates)
            new_fields["payload"] = FrozenDict(merged_payload)
        new_fields.setdefault("id", uuid4().hex[:16])
        return replace(self, **new_fields)
````

### is_resource

```
is_resource: bool
```

Report whether this token's colour is `"resource"`.

Python-friendly shorthand for `token.color == "resource"`.

Returns:

| Type   | Description                                       |
| ------ | ------------------------------------------------- |
| `bool` | True if color equals "resource", False otherwise. |

### evolve

```
evolve(
    payload_updates: dict[str, Any] | None = None,
    **field_updates,
) -> Token
```

Create a new `Token` by merging payload updates and overriding fields.

Since `Token` is immutable, this is the standard way to derive an updated token as it moves through the net. A fresh `id` is generated for the new token unless `id` is explicitly passed in `field_updates`.

Parameters:

| Name              | Type             | Description                                                                                             | Default                                                                                                                                                                                     |
| ----------------- | ---------------- | ------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `payload_updates` | \`dict[str, Any] | None\`                                                                                                  | Optional dict merged on top of the existing payload (via dict.update); keys not present are left unchanged. If None (default), payload is left as-is (unless overridden via field_updates). |
| `**field_updates` | `Any`            | Additional dataclass field values (e.g. color, available_at, attempts) to override on the new instance. | `{}`                                                                                                                                                                                        |

Returns:

| Type    | Description                                                         |
| ------- | ------------------------------------------------------------------- |
| `Token` | A new Token instance with the merged payload and overridden fields; |
| `Token` | the original token is left unchanged.                               |

Example

```
t = Token(payload={"count": 1})
t2 = t.evolve(payload_updates={"count": 2}, attempts=1)
# t2.payload == {"count": 2}, t2.attempts == 1, t2.id != t.id
```

Source code in `src/cpnx/tokens.py`

````
def evolve(self, payload_updates: dict[str, Any] | None = None, **field_updates) -> "Token":
    """Create a new `Token` by merging payload updates and overriding fields.

    Since `Token` is immutable, this is the standard way to derive an updated
    token as it moves through the net. A fresh `id` is generated for the new
    token unless `id` is explicitly passed in `field_updates`.

    Args:
        payload_updates: Optional dict merged on top of the existing `payload`
            (via `dict.update`); keys not present are left unchanged. If `None`
            (default), `payload` is left as-is (unless overridden via `field_updates`).
        **field_updates (Any): Additional dataclass field values (e.g. `color`,
            `available_at`, `attempts`) to override on the new instance.

    Returns:
        A new `Token` instance with the merged payload and overridden fields;
        the original token is left unchanged.

    Example:
        ```python
        t = Token(payload={"count": 1})
        t2 = t.evolve(payload_updates={"count": 2}, attempts=1)
        # t2.payload == {"count": 2}, t2.attempts == 1, t2.id != t.id
        ```
    """
    new_fields = field_updates
    if payload_updates is not None:
        merged_payload = dict(self.payload)
        merged_payload.update(payload_updates)
        new_fields["payload"] = FrozenDict(merged_payload)
    new_fields.setdefault("id", uuid4().hex[:16])
    return replace(self, **new_fields)
````

## cpnx.FrozenDict

Bases: `Mapping`

Immutable, hashable mapping that recursively freezes nested dicts and lists.

Implements `collections.abc.Mapping` over an internal `types.MappingProxyType`, so instances support read-only dict-like access (`d[key]`, `len(d)`, iteration) but no in-place mutation. Nested `dict`/`Mapping` values are recursively wrapped in `FrozenDict`, and nested `list` values are recursively converted to `tuple` (with any `FrozenDict`/`Mapping` items inside also frozen). The hash is computed eagerly at construction time from `frozenset(self._data.items())`, so all values (including nested ones) must be hashable.

Parameters:

| Name       | Type      | Description                                                                                    | Default                                                                |
| ---------- | --------- | ---------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| `data`     | \`Mapping | None\`                                                                                         | Optional mapping to freeze. Entries are copied and recursively frozen. |
| `**kwargs` | `Any`     | Additional key/value pairs to include, applied after data and recursively frozen the same way. | `{}`                                                                   |

Raises:

| Type        | Description                                                                                                |
| ----------- | ---------------------------------------------------------------------------------------------------------- |
| `TypeError` | If any value (at any nesting depth) is unhashable, since the hash is computed eagerly during construction. |

Source code in `src/cpnx/tokens.py`

````
class FrozenDict(Mapping):
    """Immutable, hashable mapping that recursively freezes nested dicts and lists.

    Implements `collections.abc.Mapping` over an internal `types.MappingProxyType`,
    so instances support read-only dict-like access (`d[key]`, `len(d)`, iteration)
    but no in-place mutation. Nested `dict`/`Mapping` values are recursively wrapped
    in `FrozenDict`, and nested `list` values are recursively converted to `tuple`
    (with any `FrozenDict`/`Mapping` items inside also frozen). The hash is computed
    eagerly at construction time from `frozenset(self._data.items())`, so all values
    (including nested ones) must be hashable.

    Args:
        data: Optional mapping to freeze. Entries are copied and recursively frozen.
        **kwargs (Any): Additional key/value pairs to include, applied after `data`
            and recursively frozen the same way.

    Raises:
        TypeError: If any value (at any nesting depth) is unhashable, since the
            hash is computed eagerly during construction.
    """

    __slots__ = ("_data", "_hash")

    def __init__(self, data: Mapping | None = None, **kwargs) -> None:
        temp = {}
        if data is not None:
            for k, v in data.items():
                temp[k] = self._freeze(v)
        for k, v in kwargs.items():
            temp[k] = self._freeze(v)
        # wrap in MappingProxyType to make it truly read-only
        self._data = types.MappingProxyType(temp)
        # Eagerly compute the hash to avoid post-construction mutation (M1)
        try:
            self._hash = hash(frozenset(self._data.items()))
        except TypeError as exc:
            raise TypeError(
                f"FrozenDict payload contains an unhashable value. "
                f"Wrap sets and arrays before passing to Token(payload=...). "
                f"Detail: {exc}"
            ) from exc

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
        return self._hash

    def __repr__(self) -> str:
        return f"FrozenDict({dict(self._data)!r})"

    def as_dict(self) -> dict:
        """Recursively convert this `FrozenDict` back to a plain, mutable `dict`.

        Nested `FrozenDict` values are recursively converted to `dict`, and `tuple`
        values (frozen from `list`) are converted back to `list`, with any nested
        `FrozenDict` items also unwrapped. Useful for JSON serialisation.

        Returns:
            A new plain `dict` with all `FrozenDict`/`tuple` wrapping removed.

        Example:
            ```python
            fd = FrozenDict({"a": 1, "b": [1, 2, {"c": 3}]})
            fd.as_dict()
            # {"a": 1, "b": [1, 2, {"c": 3}]}
            ```
        """
        return {k: FrozenDict._thaw(v) for k, v in self._data.items()}

    @staticmethod
    def _thaw(v: Any) -> Any:
        """Recursively convert FrozenDict/tuple to dict/list."""
        if isinstance(v, FrozenDict):
            return v.as_dict()
        if isinstance(v, tuple):
            return [FrozenDict._thaw(item) for item in v]
        return v

    def set(self, key: Any, value: Any) -> "FrozenDict":
        """Return a new `FrozenDict` with `key` set to `value`, leaving this instance unchanged.

        Args:
            key: The key to set (or overwrite) in the returned copy.
            value: The value to associate with `key`. Frozen recursively like any
                value passed to the constructor.

        Returns:
            A new `FrozenDict` instance containing all existing entries plus the
            given `key`/`value` pair.

        Example:
            ```python
            fd = FrozenDict({"a": 1})
            fd2 = fd.set("b", 2)
            # fd is unchanged: FrozenDict({'a': 1})
            # fd2 is FrozenDict({'a': 1, 'b': 2})
            ```
        """
        new_d = dict(self._data)
        new_d[key] = value
        return FrozenDict(new_d)
````

### as_dict

```
as_dict() -> dict
```

Recursively convert this `FrozenDict` back to a plain, mutable `dict`.

Nested `FrozenDict` values are recursively converted to `dict`, and `tuple` values (frozen from `list`) are converted back to `list`, with any nested `FrozenDict` items also unwrapped. Useful for JSON serialisation.

Returns:

| Type   | Description                                                  |
| ------ | ------------------------------------------------------------ |
| `dict` | A new plain dict with all FrozenDict/tuple wrapping removed. |

Example

```
fd = FrozenDict({"a": 1, "b": [1, 2, {"c": 3}]})
fd.as_dict()
# {"a": 1, "b": [1, 2, {"c": 3}]}
```

Source code in `src/cpnx/tokens.py`

````
def as_dict(self) -> dict:
    """Recursively convert this `FrozenDict` back to a plain, mutable `dict`.

    Nested `FrozenDict` values are recursively converted to `dict`, and `tuple`
    values (frozen from `list`) are converted back to `list`, with any nested
    `FrozenDict` items also unwrapped. Useful for JSON serialisation.

    Returns:
        A new plain `dict` with all `FrozenDict`/`tuple` wrapping removed.

    Example:
        ```python
        fd = FrozenDict({"a": 1, "b": [1, 2, {"c": 3}]})
        fd.as_dict()
        # {"a": 1, "b": [1, 2, {"c": 3}]}
        ```
    """
    return {k: FrozenDict._thaw(v) for k, v in self._data.items()}
````

### set

```
set(key: Any, value: Any) -> FrozenDict
```

Return a new `FrozenDict` with `key` set to `value`, leaving this instance unchanged.

Parameters:

| Name    | Type  | Description                                                                                   | Default    |
| ------- | ----- | --------------------------------------------------------------------------------------------- | ---------- |
| `key`   | `Any` | The key to set (or overwrite) in the returned copy.                                           | *required* |
| `value` | `Any` | The value to associate with key. Frozen recursively like any value passed to the constructor. | *required* |

Returns:

| Type         | Description                                                        |
| ------------ | ------------------------------------------------------------------ |
| `FrozenDict` | A new FrozenDict instance containing all existing entries plus the |
| `FrozenDict` | given key/value pair.                                              |

Example

```
fd = FrozenDict({"a": 1})
fd2 = fd.set("b", 2)
# fd is unchanged: FrozenDict({'a': 1})
# fd2 is FrozenDict({'a': 1, 'b': 2})
```

Source code in `src/cpnx/tokens.py`

````
def set(self, key: Any, value: Any) -> "FrozenDict":
    """Return a new `FrozenDict` with `key` set to `value`, leaving this instance unchanged.

    Args:
        key: The key to set (or overwrite) in the returned copy.
        value: The value to associate with `key`. Frozen recursively like any
            value passed to the constructor.

    Returns:
        A new `FrozenDict` instance containing all existing entries plus the
        given `key`/`value` pair.

    Example:
        ```python
        fd = FrozenDict({"a": 1})
        fd2 = fd.set("b", 2)
        # fd is unchanged: FrozenDict({'a': 1})
        # fd2 is FrozenDict({'a': 1, 'b': 2})
        ```
    """
    new_d = dict(self._data)
    new_d[key] = value
    return FrozenDict(new_d)
````

## cpnx.AVAILABLE_NOW

```
AVAILABLE_NOW: float = 0.0
```

Sentinel value for Token.`available_at` meaning "available immediately".

Used as the default for `available_at` so that untimed tokens are eligible for firing as soon as they are created, rather than after some future monotonic timestamp.

## cpnx.ERROR_COLOR

```
ERROR_COLOR: str = 'error'
```

Reserved token colour conventionally used to mark dead-lettered/error tokens.

Not enforced by Token itself; consumers (e.g. error-handling transitions or sink places) may use this string as the `color` value for tokens that represent a failure, so they can be routed or filtered distinctly.
