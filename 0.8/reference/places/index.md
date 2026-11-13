# Places

Places hold tokens. Beyond the plain `Place`, several variants model resource pools, pacing, thresholds, and sinks.

## cpnx.Place

An unbounded FIFO queue that holds tokens flowing through a Petri net.

**CPN equivalent:** a place with an unrestricted colour set (accepts any colour) and no initial marking. Set `color_set` to restrict accepted colours; set `initial_marking` to pre-fill with tokens at construction.

All operations are thread-safe via an internal `threading.Lock`.

Source code in `src/cpnx/places.py`

```
class Place:
    """An unbounded FIFO queue that holds tokens flowing through a Petri net.

    **CPN equivalent:** a place with an unrestricted colour set (accepts any
    colour) and no initial marking. Set ``color_set`` to restrict accepted
    colours; set ``initial_marking`` to pre-fill with tokens at construction.

    All operations are thread-safe via an internal `threading.Lock`.
    """

    _restorable: ClassVar[bool] = True
    """Whether `_restore_tokens` may replace this place's whole contents.

    A sink is never restored, and a pool rolls back by count through `_roll_back_to`.
    """

    def __init__(
        self,
        name: str,
        bound: int | None = None,
        color_set: set[str] | None = None,
        initial_marking: list[Token] | None = None,
        schema: type | Callable[[Any], bool] | None = None,
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
            schema: Optional schema or type constraint for token payloads. Can be a
                    type (checked via ``isinstance``) or a callable predicate
                    returning a bool. ``None`` (default) accepts any payload. Resource
                    permit tokens (``color="resource"``) are exempt, since permits carry
                    no payload — a payload schema never applies to them.

        Note:
            Token payloads are always coerced to an immutable [`FrozenDict`][cpnx.FrozenDict]
            (a ``Mapping``, not a ``dict`` subclass), so a type schema is only useful as
            ``dict``/``Mapping`` (always satisfied) or an ``ABC`` that ``FrozenDict`` registers
            under. To validate payload *contents*, pass a callable predicate.
        """
        self.name = name
        self.bound = bound
        self.color_set = color_set
        self.schema = schema
        self._store = _TokenStore()
        self._lock = threading.Lock()
        self.last_deposit_time: float = 0.0
        self.last_deposit_time_model: float = 0.0

        for token in initial_marking or []:
            if self.color_set is not None and token.color not in self.color_set:
                raise TypeError(
                    f"Place '{self.name}' has color_set {self.color_set!r} — "
                    f"cannot deposit token with color {token.color!r}."
                )
            self._enforce_schema(token)
            self._store.append(token)
            self.last_deposit_time = time.monotonic()

    def _schema_failure_reason(self, payload: Any) -> str | None:
        """Return ``None`` if *payload* conforms to `schema`, else a human-readable reason.

        The reason always contains the word "schema" (callers wrap it in a ``TypeError``) and,
        when a callable schema *raises*, includes that exception via ``repr`` — so a buggy or
        over-strict predicate is diagnosable rather than silently collapsed to a bare rejection.
        A raising predicate still counts as a rejection (the payload could not be validated).

        Args:
            payload: The token payload to check against `schema`.

        Returns:
            ``None`` when valid; otherwise a reason string describing the violation.
        """
        if self.schema is None:
            return None
        if isinstance(self.schema, type):
            if self.schema is dict and isinstance(payload, Mapping):
                return None
            if isinstance(payload, self.schema):
                return None
            return f"payload {payload!r} does not match schema {self.schema!r}."
        if callable(self.schema):
            try:
                ok = bool(self.schema(payload))
            except Exception as exc:
                return f"payload {payload!r} does not match schema {self.schema!r} (predicate raised {exc!r})."
            if ok:
                return None
            return f"payload {payload!r} does not match schema {self.schema!r}."
        return f"payload {payload!r} does not match schema {self.schema!r} (schema is neither a type nor callable)."

    def validate_schema(self, payload: Any) -> bool:
        """Validate whether *payload* conforms to this place's schema.

        Thin boolean wrapper over `_schema_failure_reason` (which carries the diagnostic detail).

        Args:
            payload: The token payload to check against `schema`.

        Returns:
            ``True`` if `schema` is ``None``, or if *payload* is an instance of `schema`
            (when `schema` is a type), or if ``schema(payload)`` evaluates to truthy
            (when `schema` is a callable). ``False`` otherwise, including when a callable
            schema raises an exception.
        """
        return self._schema_failure_reason(payload) is None

    def _enforce_schema(self, token: Token) -> None:
        """Raise ``TypeError`` if *token* violates this place's schema.

        Resource permit tokens (``color="resource"``) are exempt: a permit carries no payload,
        so `schema` — a *payload* constraint — never applies to it. Enforcing it would reject
        every auto-generated permit (empty payload) and, on the transition return path, silently
        route the permit to the error place instead of the pool. See `is_resource`.
        """
        if token.is_resource:
            return
        reason = self._schema_failure_reason(token.payload)
        if reason is not None:
            raise TypeError(f"Color Set / Schema Violation in Place '{self.name}': {reason}")

    def deposit(self, token: Token, model_time: float | None = None) -> None:
        """Append *token* to the tail of the FIFO queue, enforcing the place's colour set.

        Updates `last_deposit_time` (and `last_deposit_time_model` if *model_time* is given),
        then calls `_on_deposit`.

        Args:
            token: The token to deposit.
            model_time: Optional logical clock timestamp recorded alongside the deposit.
                        Does not affect wall-clock availability checks.

        Raises:
            TypeError: If `color_set` is set and *token*'s colour is not in it,
                       or if `schema` is set and *token*'s payload fails validation.
        """
        with self._lock:
            if self.color_set is not None and token.color not in self.color_set:
                raise TypeError(
                    f"Place '{self.name}' has color_set {self.color_set!r} — "
                    f"cannot deposit token with color {token.color!r}."
                )
            self._enforce_schema(token)
            self._store.append(token)
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
            if not self._store.has_available(t_limit, count):
                available_count = self._store.count_available(t_limit)
                raise ValueError(
                    f"Place '{self.name}': cannot retrieve {count} token(s) — only {available_count} available."
                )
            return self._store.take_available(count, t_limit)

    def retrieve_specific(self, tokens: list[Token], model_time: float | None = None) -> list[Token]:
        """Remove and return exactly the given *tokens*, matched by ``id`` rather than FIFO order.

        Used by the engine when an [`InputArc`][cpnx.InputArc] has a ``key``/``filter`` that
        selects a specific subset of tokens to consume rather than the head of the queue.

        Args:
            tokens: Tokens to remove, identified by their ``id``. Each must currently be
                    present in this place and available at the effective time.
            model_time: Optional logical clock timestamp used instead of wall-clock time
                        to check token availability.

        Returns:
            The removed tokens, in the same order as *tokens* (not necessarily FIFO order).

        Raises:
            ValueError: If any token in *tokens* is not yet available (its `available_at`
                        is after the effective time), is not found in this place, or appears
                        more than once in *tokens*.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            for t in tokens:
                if t.available_at > t_limit:
                    raise ValueError(
                        f"Place '{self.name}': token {t.id} is not yet available at {t_limit} "
                        f"(available_at={t.available_at})."
                    )
            ids = set(map(id, tokens))
            if len(ids) != len(tokens):
                repeated = _first_repeated(tokens).id
                raise ValueError(f"Place '{self.name}': token {repeated} passed more than once to retrieve_specific.")
            missing_identities = self._store.missing_identities(ids)
            if missing_identities:
                missing = {t.id for t in tokens if id(t) in missing_identities}
                raise ValueError(
                    f"Place '{self.name}': token id(s) {missing} not found — "
                    f"cannot retrieve_specific tokens that are not present."
                )
            self._store.remove_identity(tokens)
            return tokens

    def holds_available(self, tokens: list[Token], model_time: float | None = None) -> bool:
        """Return whether every token in *tokens* is stored here and available at the effective time.

        The check [`retrieve_specific`][cpnx.Place.retrieve_specific] makes before it removes
        anything, without raising or removing: O(len(tokens)), however many tokens are stored.
        It does not check that *tokens* are distinct.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            if any(t.available_at > t_limit for t in tokens):
                return False
            return not self._store.missing_identities(set(map(id, tokens)))

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
            return self._store.take_available(len(self._store), t_limit)

    def _return_token(self, token: Token) -> None:
        """Put back a token this place gave up, unchanged and without re-applying any deposit rule."""
        with self._lock:
            self._store.append(token)

    def _restore_tokens(self, tokens: tuple[Token, ...]) -> bool:
        """Replace the contents with exactly *tokens*, bypassing colour, schema and pacing.

        Used to roll a subnet back after a failed substitution firing; *tokens* is an earlier
        `tokens` snapshot of this same place. Returns whether the contents changed.
        """
        if not self._restorable:
            raise TypeError(f"{type(self).__name__} '{self.name}' is never restored from a checkpoint")
        with self._lock:
            current = self._store.iter_insertion_order()
            if len(current) == len(tokens) and all(a is b for a, b in zip(current, tokens, strict=True)):
                return False
            self._store.drop_all()
            for token in tokens:
                self._store.append(token)
            return True

    def _roll_back_to(self, tokens: tuple[Token, ...]) -> bool:
        """Return this place to its `tokens` snapshot from a checkpoint; whether anything changed.

        Resource pools override this to restore only their permit count.
        """
        return self._restore_tokens(tokens)

    def peek(self, count: int = 1, model_time: float | None = None) -> list[Token]:
        """Return up to *count* available tokens from the head without removing them.

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
            return self._store.peek_available(count, t_limit)

    def register_key_index(self, index_id: int, key_fn: Callable[[Token], object]) -> None:
        """Maintain a persistent ascending-key index over this place's ready tokens.

        Registered by the engine at net-build time for each [`InputArc`][cpnx.InputArc]
        whose `key` is *certified* (see [`cpnx.certification`]) — an uncertified key is not
        indexed, because keying happens on the deposit path where an unbounded callable
        cannot be allowed to run. *index_id* identifies the registering arc; re-registering
        the same id with a different function rebuilds the index, so reassigning `arc.key`
        after construction stays correct. A disabled index can also be re-armed in place,
        without changing the function, via [`rearm_key_index`][cpnx.Place.rearm_key_index]
        (issue #34).

        Registration back-fills from the tokens already present, so it is safe on a
        non-empty place. This is a pure optimisation: every read through
        [`peek_by_key`][cpnx.Place.peek_by_key] can decline, and the engine then computes
        the same answer the slow way.
        """
        with self._lock:
            self._store.register_key_index(index_id, key_fn)

    def rearm_key_index(self, index_id: int) -> bool:
        """Re-enable a disabled key-index for *index_id* and repopulate it from the ready set.

        Returns whether the index is serving afterwards. Called by the engine when selection
        succeeds again for an arc whose index disabled itself (issue #34): a keying failure is
        no longer permanent — once the offending token is gone the fast path is restored,
        rebuilt at most once per `_KEY_INDEX_REARM_MIN_DEPOSITS` deposits to bound the cost of a
        flapping key.
        """
        with self._lock:
            return self._store.rearm_key_index(index_id)

    def key_index_disabled(self, index_id: int) -> bool:
        """Whether *index_id*'s index exists and has disabled itself.

        Distinct from "cannot answer right now" (see
        [`peek_by_key`][cpnx.Place.peek_by_key], which also declines while the place holds
        cooling tokens): this is specifically the state a keying failure leaves behind, which
        is reversible via [`rearm_key_index`][cpnx.Place.rearm_key_index] (issue #34). The
        engine reads it when reporting a selection fault, so the report mentions the lost
        index only when one was actually lost — a raising `filter` never touches the index,
        and an uncertified key never has one.
        """
        with self._lock:
            index = self._store.key_index(index_id)
            return index is not None and index.disabled

    def peek_by_key(
        self, index_id: int, count: int, predicate: Callable[[Token], bool] | None = None
    ) -> list[Token] | None:
        """Return up to *count* available tokens in ascending key order, or ``None``.

        ``None`` means the index cannot answer and the caller must fall back to its own
        ordering — no index registered, the index disabled itself after a keying failure,
        or the place currently holds timed (cooling) tokens the index does not cover.

        *predicate* is applied at pop: a rejected token is skipped but stays indexed, and
        the scan continues past it, so a `filter` never causes a short read.
        """
        with self._lock:
            if count <= 0:
                return []
            return self._store.peek_by_key(index_id, count, predicate)

    def register_join_index(self, index_id: int, key_fn: Callable[[Token], object], depth: int = 1) -> None:
        """Maintain a persistent index from `key_fn(token)` to this place's tokens.

        Registered by the engine for each [`InputArc`][cpnx.InputArc] whose `join_key` is
        *certified* (see [`cpnx.certification`]), because keying runs on the deposit path.
        *index_id* identifies the registering arc. Re-registering the same id with a different
        function rebuilds the index, and registration back-fills from the tokens already
        present. Like the key index, this is a pure optimisation: [`peek_join`][cpnx.Place.peek_join]
        may decline, and the engine then computes the same groups itself.

        Args:
            index_id: Identifies the registering arc.
            key_fn: The certified `join_key`, called once per token as it is deposited.
            depth: How many of each key's leading tokens the index watches for
                [`join_head_changes`][cpnx.Place.join_head_changes]: how many join arcs of
                the registering transition read this place with *key_fn*, each taking a
                different token, or every join arc on the place when they use several
                functions. Changing it rebuilds the index. Defaults to 1.

        Raises:
            ValueError: If *depth* is not an `int` of at least 1.
        """
        _check_join_depth(depth)
        with self._lock:
            self._store.register_join_index(index_id, key_fn, depth)

    def unregister_join_index(self, index_id: int) -> None:
        """Drop the join index registered under *index_id*, so deposits stop keying for it.

        The engine calls this when the registering arc no longer reads this place through that
        index: its `join_key` was set back to `None`, or the place was replaced. Unknown ids are
        ignored.
        """
        with self._lock:
            self._store.unregister_join_index(index_id)

    def peek_join(
        self, index_id: int, keys: Iterable[object] | None, model_time: float | None = None
    ) -> "dict[object, Iterable[tuple[tuple[int, float, int], Token]]] | None":
        """Return this place's available tokens grouped by join key, or ``None`` to fall back.

        Args:
            index_id: The id the join index was registered under.
            keys: The join keys to look up, or `None` for every key with an available token.
                A key with no available token is left out of the result.
            model_time: Optional logical clock timestamp used instead of wall-clock time to
                decide which tokens are available.

        Returns:
            Each key mapped to its available tokens in this place's read order, each paired
            with a sort position that orders them the same way. A group is a sized, lazily
            read view of the index, valid until the place next changes. `None` when no index
            is registered under *index_id* or it disabled itself after a keying failure.
        """
        with self._lock:
            t_limit = model_time if model_time is not None else time.monotonic()
            return self._store.join_groups(index_id, keys, t_limit)

    def join_head_changes(
        self, sources: "tuple[tuple[int, int], ...]", generations: "tuple[int, ...] | None"
    ) -> "_HeadReport | None":
        """Report when each changed join key binds here, for the engine's cross-place key match.

        *sources* holds one ``(index_id, arcs)`` per `join_key` function the reading
        transition's join arcs use on this place: an index registered for that function, with a
        `depth` of at least every join arc on the place, and how many of those arcs use it.

        Returns ``(generations, every_head, changes)``. With the *generations* the previous call
        returned, *every_head* is ``None`` and *changes* maps each key whose earliest tokens
        changed since then to ``(when, first)``, or to ``None`` when it no longer binds. *when*
        is the time the key binds (``-inf`` when it binds now): once its `arcs`-th token is
        available for one function, and once each arc can take a token of its own for several.
        *first* is the sort position of its first token under the first source. Otherwise
        *every_head* maps every key that binds to that pair and *changes* is empty. ``None``
        when an index is not registered or not serving, exactly as for
        [`peek_join`][cpnx.Place.peek_join].
        """
        with self._lock:
            return self._store.join_head_changes(sources, generations)

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
            return self._store.has_available(t_limit, count)

    def earliest_available_boundary(self, now: float) -> float | None:
        """Smallest future ``available_at`` (strictly greater than *now*) held here, or ``None``.

        Backs the engine's logical-clock advance. O(1) for an untimed place (nothing is
        cooling), and O(log n) amortized otherwise, however many matured tokens are waiting
        here to be consumed, so the clock advance does not scan the marking on every tick.
        A *now* earlier than the previous boundary query's (for example the engine's wall-clock
        query followed by a switch to a small logical time) pays one O(n) re-split, after which
        queries from that time on are back to O(log n). Reads such as `peek` and
        `can_retrieve` do not affect this, whichever clock they use.
        """
        with self._lock:
            return self._store.earliest_cooling_after(now)

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
            return len(self._store) + count <= self.bound

    def can_accept(self, token: Token) -> bool:
        """Return ``True`` if *token* is compatible with this place's colour set and schema.

        This is a non-mutating pre-flight check that does not modify the place's tokens
        and does not consider capacity — use `can_deposit` for bound checks.

        Args:
            token: The token to check for colour compatibility and schema validation.

        Returns:
            ``True`` if `color_set` is ``None`` or contains *token*'s colour and
            `validate_schema` returns ``True``, ``False`` otherwise. Resource permit
            tokens are exempt from the schema check (see `_enforce_schema`).
        """
        with self._lock:
            if self.color_set is not None and token.color not in self.color_set:
                return False
            if not token.is_resource and not self.validate_schema(token.payload):
                return False
            return True

    @property
    def tokens(self) -> tuple[Token, ...]:
        """Snapshot of all current tokens (including not-yet-available ones) as an immutable tuple.

        Does not filter by `available_at` and does not consume or remove any tokens.
        """
        with self._lock:
            return tuple(self._store.iter_insertion_order())

    def __len__(self) -> int:
        """Return the number of tokens currently in the place."""
        with self._lock:
            return len(self._store)

    def __bool__(self) -> bool:
        """A Place is always truthy, even when it contains no tokens."""
        return True
```

### tokens

```
tokens: tuple[Token, ...]
```

Snapshot of all current tokens (including not-yet-available ones) as an immutable tuple.

Does not filter by `available_at` and does not consume or remove any tokens.

### __init__

```
__init__(
    name: str,
    bound: int | None = None,
    color_set: set[str] | None = None,
    initial_marking: list[Token] | None = None,
    schema: type | Callable[[Any], bool] | None = None,
) -> None
```

Create a new Place.

Parameters:

| Name              | Type          | Description                                         | Default                                                                                                                                                                                                                                          |
| ----------------- | ------------- | --------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `name`            | `str`         | Unique identifier for this place within a PetriNet. | *required*                                                                                                                                                                                                                                       |
| `bound`           | \`int         | None\`                                              | Optional k-bound (capacity constraint). The engine will not fire a transition whose unguarded output arc targets this place if doing so would exceed the bound. None (default) means unbounded — this is standard CPN k-bounded place semantics. |
| `color_set`       | \`set[str]    | None\`                                              | Set of accepted token colours. None (default) accepts any colour. Pass e.g. {"data", "priority"} to enforce typing at deposit time.                                                                                                              |
| `initial_marking` | \`list[Token] | None\`                                              | Tokens to deposit at construction (CPN I function). Deposited before any external code runs.                                                                                                                                                     |
| `schema`          | \`type        | Callable\[[Any], bool\]                             | None\`                                                                                                                                                                                                                                           |

Note

Token payloads are always coerced to an immutable FrozenDict (a `Mapping`, not a `dict` subclass), so a type schema is only useful as `dict`/`Mapping` (always satisfied) or an `ABC` that `FrozenDict` registers under. To validate payload *contents*, pass a callable predicate.

Source code in `src/cpnx/places.py`

```
def __init__(
    self,
    name: str,
    bound: int | None = None,
    color_set: set[str] | None = None,
    initial_marking: list[Token] | None = None,
    schema: type | Callable[[Any], bool] | None = None,
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
        schema: Optional schema or type constraint for token payloads. Can be a
                type (checked via ``isinstance``) or a callable predicate
                returning a bool. ``None`` (default) accepts any payload. Resource
                permit tokens (``color="resource"``) are exempt, since permits carry
                no payload — a payload schema never applies to them.

    Note:
        Token payloads are always coerced to an immutable [`FrozenDict`][cpnx.FrozenDict]
        (a ``Mapping``, not a ``dict`` subclass), so a type schema is only useful as
        ``dict``/``Mapping`` (always satisfied) or an ``ABC`` that ``FrozenDict`` registers
        under. To validate payload *contents*, pass a callable predicate.
    """
    self.name = name
    self.bound = bound
    self.color_set = color_set
    self.schema = schema
    self._store = _TokenStore()
    self._lock = threading.Lock()
    self.last_deposit_time: float = 0.0
    self.last_deposit_time_model: float = 0.0

    for token in initial_marking or []:
        if self.color_set is not None and token.color not in self.color_set:
            raise TypeError(
                f"Place '{self.name}' has color_set {self.color_set!r} — "
                f"cannot deposit token with color {token.color!r}."
            )
        self._enforce_schema(token)
        self._store.append(token)
        self.last_deposit_time = time.monotonic()
```

### validate_schema

```
validate_schema(payload: Any) -> bool
```

Validate whether *payload* conforms to this place's schema.

Thin boolean wrapper over `_schema_failure_reason` (which carries the diagnostic detail).

Parameters:

| Name      | Type  | Description                                | Default    |
| --------- | ----- | ------------------------------------------ | ---------- |
| `payload` | `Any` | The token payload to check against schema. | *required* |

Returns:

| Type   | Description                                                             |
| ------ | ----------------------------------------------------------------------- |
| `bool` | True if schema is None, or if payload is an instance of schema          |
| `bool` | (when schema is a type), or if schema(payload) evaluates to truthy      |
| `bool` | (when schema is a callable). False otherwise, including when a callable |
| `bool` | schema raises an exception.                                             |

Source code in `src/cpnx/places.py`

```
def validate_schema(self, payload: Any) -> bool:
    """Validate whether *payload* conforms to this place's schema.

    Thin boolean wrapper over `_schema_failure_reason` (which carries the diagnostic detail).

    Args:
        payload: The token payload to check against `schema`.

    Returns:
        ``True`` if `schema` is ``None``, or if *payload* is an instance of `schema`
        (when `schema` is a type), or if ``schema(payload)`` evaluates to truthy
        (when `schema` is a callable). ``False`` otherwise, including when a callable
        schema raises an exception.
    """
    return self._schema_failure_reason(payload) is None
```

### deposit

```
deposit(
    token: Token, model_time: float | None = None
) -> None
```

Append *token* to the tail of the FIFO queue, enforcing the place's colour set.

Updates `last_deposit_time` (and `last_deposit_time_model` if *model_time* is given), then calls `_on_deposit`.

Parameters:

| Name         | Type    | Description           | Default                                                                                                          |
| ------------ | ------- | --------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `token`      | `Token` | The token to deposit. | *required*                                                                                                       |
| `model_time` | \`float | None\`                | Optional logical clock timestamp recorded alongside the deposit. Does not affect wall-clock availability checks. |

Raises:

| Type        | Description                                                                                                    |
| ----------- | -------------------------------------------------------------------------------------------------------------- |
| `TypeError` | If color_set is set and token's colour is not in it, or if schema is set and token's payload fails validation. |

Source code in `src/cpnx/places.py`

```
def deposit(self, token: Token, model_time: float | None = None) -> None:
    """Append *token* to the tail of the FIFO queue, enforcing the place's colour set.

    Updates `last_deposit_time` (and `last_deposit_time_model` if *model_time* is given),
    then calls `_on_deposit`.

    Args:
        token: The token to deposit.
        model_time: Optional logical clock timestamp recorded alongside the deposit.
                    Does not affect wall-clock availability checks.

    Raises:
        TypeError: If `color_set` is set and *token*'s colour is not in it,
                   or if `schema` is set and *token*'s payload fails validation.
    """
    with self._lock:
        if self.color_set is not None and token.color not in self.color_set:
            raise TypeError(
                f"Place '{self.name}' has color_set {self.color_set!r} — "
                f"cannot deposit token with color {token.color!r}."
            )
        self._enforce_schema(token)
        self._store.append(token)
        self.last_deposit_time = time.monotonic()
        if model_time is not None:
            self.last_deposit_time_model = model_time
        self._on_deposit(token)
```

### retrieve

```
retrieve(
    count: int = 1, model_time: float | None = None
) -> list[Token]
```

Remove and return *count* tokens from the head of the queue, in FIFO order.

A token is only eligible if its `available_at` timestamp is at or before the effective time (`model_time` if given, else `time.monotonic()`); tokens still in the future (e.g. cooling down) are skipped.

Parameters:

| Name         | Type    | Description                                 | Default                                                                                                   |
| ------------ | ------- | ------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `count`      | `int`   | Number of tokens to retrieve. Must be >= 1. | `1`                                                                                                       |
| `model_time` | \`float | None\`                                      | Optional logical clock timestamp used instead of wall-clock time to determine which tokens are available. |

Returns:

| Type          | Description                             |
| ------------- | --------------------------------------- |
| `list[Token]` | List of retrieved tokens in FIFO order. |

Raises:

| Type         | Description                               |
| ------------ | ----------------------------------------- |
| `ValueError` | If fewer than count tokens are available. |

Source code in `src/cpnx/places.py`

```
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
        if not self._store.has_available(t_limit, count):
            available_count = self._store.count_available(t_limit)
            raise ValueError(
                f"Place '{self.name}': cannot retrieve {count} token(s) — only {available_count} available."
            )
        return self._store.take_available(count, t_limit)
```

### retrieve_specific

```
retrieve_specific(
    tokens: list[Token], model_time: float | None = None
) -> list[Token]
```

Remove and return exactly the given *tokens*, matched by `id` rather than FIFO order.

Used by the engine when an InputArc has a `key`/`filter` that selects a specific subset of tokens to consume rather than the head of the queue.

Parameters:

| Name         | Type          | Description                                                                                                                 | Default                                                                                       |
| ------------ | ------------- | --------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| `tokens`     | `list[Token]` | Tokens to remove, identified by their id. Each must currently be present in this place and available at the effective time. | *required*                                                                                    |
| `model_time` | \`float       | None\`                                                                                                                      | Optional logical clock timestamp used instead of wall-clock time to check token availability. |

Returns:

| Type          | Description                                                                   |
| ------------- | ----------------------------------------------------------------------------- |
| `list[Token]` | The removed tokens, in the same order as tokens (not necessarily FIFO order). |

Raises:

| Type         | Description                                                                                                                                                  |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `ValueError` | If any token in tokens is not yet available (its available_at is after the effective time), is not found in this place, or appears more than once in tokens. |

Source code in `src/cpnx/places.py`

```
def retrieve_specific(self, tokens: list[Token], model_time: float | None = None) -> list[Token]:
    """Remove and return exactly the given *tokens*, matched by ``id`` rather than FIFO order.

    Used by the engine when an [`InputArc`][cpnx.InputArc] has a ``key``/``filter`` that
    selects a specific subset of tokens to consume rather than the head of the queue.

    Args:
        tokens: Tokens to remove, identified by their ``id``. Each must currently be
                present in this place and available at the effective time.
        model_time: Optional logical clock timestamp used instead of wall-clock time
                    to check token availability.

    Returns:
        The removed tokens, in the same order as *tokens* (not necessarily FIFO order).

    Raises:
        ValueError: If any token in *tokens* is not yet available (its `available_at`
                    is after the effective time), is not found in this place, or appears
                    more than once in *tokens*.
    """
    with self._lock:
        t_limit = model_time if model_time is not None else time.monotonic()
        for t in tokens:
            if t.available_at > t_limit:
                raise ValueError(
                    f"Place '{self.name}': token {t.id} is not yet available at {t_limit} "
                    f"(available_at={t.available_at})."
                )
        ids = set(map(id, tokens))
        if len(ids) != len(tokens):
            repeated = _first_repeated(tokens).id
            raise ValueError(f"Place '{self.name}': token {repeated} passed more than once to retrieve_specific.")
        missing_identities = self._store.missing_identities(ids)
        if missing_identities:
            missing = {t.id for t in tokens if id(t) in missing_identities}
            raise ValueError(
                f"Place '{self.name}': token id(s) {missing} not found — "
                f"cannot retrieve_specific tokens that are not present."
            )
        self._store.remove_identity(tokens)
        return tokens
```

### holds_available

```
holds_available(
    tokens: list[Token], model_time: float | None = None
) -> bool
```

Return whether every token in *tokens* is stored here and available at the effective time.

The check retrieve_specific makes before it removes anything, without raising or removing: O(len(tokens)), however many tokens are stored. It does not check that *tokens* are distinct.

Source code in `src/cpnx/places.py`

```
def holds_available(self, tokens: list[Token], model_time: float | None = None) -> bool:
    """Return whether every token in *tokens* is stored here and available at the effective time.

    The check [`retrieve_specific`][cpnx.Place.retrieve_specific] makes before it removes
    anything, without raising or removing: O(len(tokens)), however many tokens are stored.
    It does not check that *tokens* are distinct.
    """
    with self._lock:
        t_limit = model_time if model_time is not None else time.monotonic()
        if any(t.available_at > t_limit for t in tokens):
            return False
        return not self._store.missing_identities(set(map(id, tokens)))
```

### retrieve_all

```
retrieve_all(
    model_time: float | None = None,
) -> list[Token]
```

Remove and return every currently-available token, leaving not-yet-available tokens behind.

Parameters:

| Name         | Type    | Description | Default                                                                                                   |
| ------------ | ------- | ----------- | --------------------------------------------------------------------------------------------------------- |
| `model_time` | \`float | None\`      | Optional logical clock timestamp used instead of wall-clock time to determine which tokens are available. |

Returns:

| Type          | Description                                                           |
| ------------- | --------------------------------------------------------------------- |
| `list[Token]` | All available tokens in FIFO order; empty list if none are available. |

Source code in `src/cpnx/places.py`

```
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
        return self._store.take_available(len(self._store), t_limit)
```

### peek

```
peek(
    count: int = 1, model_time: float | None = None
) -> list[Token]
```

Return up to *count* available tokens from the head without removing them.

Parameters:

| Name         | Type    | Description                          | Default                                                                                                   |
| ------------ | ------- | ------------------------------------ | --------------------------------------------------------------------------------------------------------- |
| `count`      | `int`   | Maximum number of tokens to inspect. | `1`                                                                                                       |
| `model_time` | \`float | None\`                               | Optional logical clock timestamp used instead of wall-clock time to determine which tokens are available. |

Returns:

| Type          | Description                                                        |
| ------------- | ------------------------------------------------------------------ |
| `list[Token]` | List of up to count tokens; may be shorter than requested if fewer |
| `list[Token]` | are present or available. Does not modify the queue.               |

Source code in `src/cpnx/places.py`

```
def peek(self, count: int = 1, model_time: float | None = None) -> list[Token]:
    """Return up to *count* available tokens from the head without removing them.

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
        return self._store.peek_available(count, t_limit)
```

### register_key_index

```
register_key_index(
    index_id: int, key_fn: Callable[[Token], object]
) -> None
```

Maintain a persistent ascending-key index over this place's ready tokens.

Registered by the engine at net-build time for each InputArc whose `key` is *certified* (see \[`cpnx.certification`\]) — an uncertified key is not indexed, because keying happens on the deposit path where an unbounded callable cannot be allowed to run. *index_id* identifies the registering arc; re-registering the same id with a different function rebuilds the index, so reassigning `arc.key` after construction stays correct. A disabled index can also be re-armed in place, without changing the function, via rearm_key_index (issue #34).

Registration back-fills from the tokens already present, so it is safe on a non-empty place. This is a pure optimisation: every read through peek_by_key can decline, and the engine then computes the same answer the slow way.

Source code in `src/cpnx/places.py`

```
def register_key_index(self, index_id: int, key_fn: Callable[[Token], object]) -> None:
    """Maintain a persistent ascending-key index over this place's ready tokens.

    Registered by the engine at net-build time for each [`InputArc`][cpnx.InputArc]
    whose `key` is *certified* (see [`cpnx.certification`]) — an uncertified key is not
    indexed, because keying happens on the deposit path where an unbounded callable
    cannot be allowed to run. *index_id* identifies the registering arc; re-registering
    the same id with a different function rebuilds the index, so reassigning `arc.key`
    after construction stays correct. A disabled index can also be re-armed in place,
    without changing the function, via [`rearm_key_index`][cpnx.Place.rearm_key_index]
    (issue #34).

    Registration back-fills from the tokens already present, so it is safe on a
    non-empty place. This is a pure optimisation: every read through
    [`peek_by_key`][cpnx.Place.peek_by_key] can decline, and the engine then computes
    the same answer the slow way.
    """
    with self._lock:
        self._store.register_key_index(index_id, key_fn)
```

### rearm_key_index

```
rearm_key_index(index_id: int) -> bool
```

Re-enable a disabled key-index for *index_id* and repopulate it from the ready set.

Returns whether the index is serving afterwards. Called by the engine when selection succeeds again for an arc whose index disabled itself (issue #34): a keying failure is no longer permanent — once the offending token is gone the fast path is restored, rebuilt at most once per `_KEY_INDEX_REARM_MIN_DEPOSITS` deposits to bound the cost of a flapping key.

Source code in `src/cpnx/places.py`

```
def rearm_key_index(self, index_id: int) -> bool:
    """Re-enable a disabled key-index for *index_id* and repopulate it from the ready set.

    Returns whether the index is serving afterwards. Called by the engine when selection
    succeeds again for an arc whose index disabled itself (issue #34): a keying failure is
    no longer permanent — once the offending token is gone the fast path is restored,
    rebuilt at most once per `_KEY_INDEX_REARM_MIN_DEPOSITS` deposits to bound the cost of a
    flapping key.
    """
    with self._lock:
        return self._store.rearm_key_index(index_id)
```

### key_index_disabled

```
key_index_disabled(index_id: int) -> bool
```

Whether *index_id*'s index exists and has disabled itself.

Distinct from "cannot answer right now" (see peek_by_key, which also declines while the place holds cooling tokens): this is specifically the state a keying failure leaves behind, which is reversible via rearm_key_index (issue #34). The engine reads it when reporting a selection fault, so the report mentions the lost index only when one was actually lost — a raising `filter` never touches the index, and an uncertified key never has one.

Source code in `src/cpnx/places.py`

```
def key_index_disabled(self, index_id: int) -> bool:
    """Whether *index_id*'s index exists and has disabled itself.

    Distinct from "cannot answer right now" (see
    [`peek_by_key`][cpnx.Place.peek_by_key], which also declines while the place holds
    cooling tokens): this is specifically the state a keying failure leaves behind, which
    is reversible via [`rearm_key_index`][cpnx.Place.rearm_key_index] (issue #34). The
    engine reads it when reporting a selection fault, so the report mentions the lost
    index only when one was actually lost — a raising `filter` never touches the index,
    and an uncertified key never has one.
    """
    with self._lock:
        index = self._store.key_index(index_id)
        return index is not None and index.disabled
```

### peek_by_key

```
peek_by_key(
    index_id: int,
    count: int,
    predicate: Callable[[Token], bool] | None = None,
) -> list[Token] | None
```

Return up to *count* available tokens in ascending key order, or `None`.

`None` means the index cannot answer and the caller must fall back to its own ordering — no index registered, the index disabled itself after a keying failure, or the place currently holds timed (cooling) tokens the index does not cover.

*predicate* is applied at pop: a rejected token is skipped but stays indexed, and the scan continues past it, so a `filter` never causes a short read.

Source code in `src/cpnx/places.py`

```
def peek_by_key(
    self, index_id: int, count: int, predicate: Callable[[Token], bool] | None = None
) -> list[Token] | None:
    """Return up to *count* available tokens in ascending key order, or ``None``.

    ``None`` means the index cannot answer and the caller must fall back to its own
    ordering — no index registered, the index disabled itself after a keying failure,
    or the place currently holds timed (cooling) tokens the index does not cover.

    *predicate* is applied at pop: a rejected token is skipped but stays indexed, and
    the scan continues past it, so a `filter` never causes a short read.
    """
    with self._lock:
        if count <= 0:
            return []
        return self._store.peek_by_key(index_id, count, predicate)
```

### register_join_index

```
register_join_index(
    index_id: int,
    key_fn: Callable[[Token], object],
    depth: int = 1,
) -> None
```

Maintain a persistent index from `key_fn(token)` to this place's tokens.

Registered by the engine for each InputArc whose `join_key` is *certified* (see \[`cpnx.certification`\]), because keying runs on the deposit path. *index_id* identifies the registering arc. Re-registering the same id with a different function rebuilds the index, and registration back-fills from the tokens already present. Like the key index, this is a pure optimisation: peek_join may decline, and the engine then computes the same groups itself.

Parameters:

| Name       | Type                        | Description                                                                                                                                                                                                                                                                                                | Default    |
| ---------- | --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------- |
| `index_id` | `int`                       | Identifies the registering arc.                                                                                                                                                                                                                                                                            | *required* |
| `key_fn`   | `Callable[[Token], object]` | The certified join_key, called once per token as it is deposited.                                                                                                                                                                                                                                          | *required* |
| `depth`    | `int`                       | How many of each key's leading tokens the index watches for join_head_changes: how many join arcs of the registering transition read this place with key_fn, each taking a different token, or every join arc on the place when they use several functions. Changing it rebuilds the index. Defaults to 1. | `1`        |

Raises:

| Type         | Description                           |
| ------------ | ------------------------------------- |
| `ValueError` | If depth is not an int of at least 1. |

Source code in `src/cpnx/places.py`

```
def register_join_index(self, index_id: int, key_fn: Callable[[Token], object], depth: int = 1) -> None:
    """Maintain a persistent index from `key_fn(token)` to this place's tokens.

    Registered by the engine for each [`InputArc`][cpnx.InputArc] whose `join_key` is
    *certified* (see [`cpnx.certification`]), because keying runs on the deposit path.
    *index_id* identifies the registering arc. Re-registering the same id with a different
    function rebuilds the index, and registration back-fills from the tokens already
    present. Like the key index, this is a pure optimisation: [`peek_join`][cpnx.Place.peek_join]
    may decline, and the engine then computes the same groups itself.

    Args:
        index_id: Identifies the registering arc.
        key_fn: The certified `join_key`, called once per token as it is deposited.
        depth: How many of each key's leading tokens the index watches for
            [`join_head_changes`][cpnx.Place.join_head_changes]: how many join arcs of
            the registering transition read this place with *key_fn*, each taking a
            different token, or every join arc on the place when they use several
            functions. Changing it rebuilds the index. Defaults to 1.

    Raises:
        ValueError: If *depth* is not an `int` of at least 1.
    """
    _check_join_depth(depth)
    with self._lock:
        self._store.register_join_index(index_id, key_fn, depth)
```

### unregister_join_index

```
unregister_join_index(index_id: int) -> None
```

Drop the join index registered under *index_id*, so deposits stop keying for it.

The engine calls this when the registering arc no longer reads this place through that index: its `join_key` was set back to `None`, or the place was replaced. Unknown ids are ignored.

Source code in `src/cpnx/places.py`

```
def unregister_join_index(self, index_id: int) -> None:
    """Drop the join index registered under *index_id*, so deposits stop keying for it.

    The engine calls this when the registering arc no longer reads this place through that
    index: its `join_key` was set back to `None`, or the place was replaced. Unknown ids are
    ignored.
    """
    with self._lock:
        self._store.unregister_join_index(index_id)
```

### peek_join

```
peek_join(
    index_id: int,
    keys: Iterable[object] | None,
    model_time: float | None = None,
) -> (
    dict[
        object,
        Iterable[tuple[tuple[int, float, int], Token]],
    ]
    | None
)
```

Return this place's available tokens grouped by join key, or `None` to fall back.

Parameters:

| Name         | Type               | Description                                 | Default                                                                                                                           |
| ------------ | ------------------ | ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `index_id`   | `int`              | The id the join index was registered under. | *required*                                                                                                                        |
| `keys`       | \`Iterable[object] | None\`                                      | The join keys to look up, or None for every key with an available token. A key with no available token is left out of the result. |
| `model_time` | \`float            | None\`                                      | Optional logical clock timestamp used instead of wall-clock time to decide which tokens are available.                            |

Returns:

| Type                                                                 | Description |
| -------------------------------------------------------------------- | ----------- |
| \`dict\[object, Iterable\[tuple\[tuple[int, float, int], Token\]\]\] | None\`      |
| \`dict\[object, Iterable\[tuple\[tuple[int, float, int], Token\]\]\] | None\`      |
| \`dict\[object, Iterable\[tuple\[tuple[int, float, int], Token\]\]\] | None\`      |
| \`dict\[object, Iterable\[tuple\[tuple[int, float, int], Token\]\]\] | None\`      |

Source code in `src/cpnx/places.py`

```
def peek_join(
    self, index_id: int, keys: Iterable[object] | None, model_time: float | None = None
) -> "dict[object, Iterable[tuple[tuple[int, float, int], Token]]] | None":
    """Return this place's available tokens grouped by join key, or ``None`` to fall back.

    Args:
        index_id: The id the join index was registered under.
        keys: The join keys to look up, or `None` for every key with an available token.
            A key with no available token is left out of the result.
        model_time: Optional logical clock timestamp used instead of wall-clock time to
            decide which tokens are available.

    Returns:
        Each key mapped to its available tokens in this place's read order, each paired
        with a sort position that orders them the same way. A group is a sized, lazily
        read view of the index, valid until the place next changes. `None` when no index
        is registered under *index_id* or it disabled itself after a keying failure.
    """
    with self._lock:
        t_limit = model_time if model_time is not None else time.monotonic()
        return self._store.join_groups(index_id, keys, t_limit)
```

### join_head_changes

```
join_head_changes(
    sources: tuple[tuple[int, int], ...],
    generations: tuple[int, ...] | None,
) -> _HeadReport | None
```

Report when each changed join key binds here, for the engine's cross-place key match.

*sources* holds one `(index_id, arcs)` per `join_key` function the reading transition's join arcs use on this place: an index registered for that function, with a `depth` of at least every join arc on the place, and how many of those arcs use it.

Returns `(generations, every_head, changes)`. With the *generations* the previous call returned, *every_head* is `None` and *changes* maps each key whose earliest tokens changed since then to `(when, first)`, or to `None` when it no longer binds. *when* is the time the key binds (`-inf` when it binds now): once its `arcs`-th token is available for one function, and once each arc can take a token of its own for several. *first* is the sort position of its first token under the first source. Otherwise *every_head* maps every key that binds to that pair and *changes* is empty. `None` when an index is not registered or not serving, exactly as for peek_join.

Source code in `src/cpnx/places.py`

```
def join_head_changes(
    self, sources: "tuple[tuple[int, int], ...]", generations: "tuple[int, ...] | None"
) -> "_HeadReport | None":
    """Report when each changed join key binds here, for the engine's cross-place key match.

    *sources* holds one ``(index_id, arcs)`` per `join_key` function the reading
    transition's join arcs use on this place: an index registered for that function, with a
    `depth` of at least every join arc on the place, and how many of those arcs use it.

    Returns ``(generations, every_head, changes)``. With the *generations* the previous call
    returned, *every_head* is ``None`` and *changes* maps each key whose earliest tokens
    changed since then to ``(when, first)``, or to ``None`` when it no longer binds. *when*
    is the time the key binds (``-inf`` when it binds now): once its `arcs`-th token is
    available for one function, and once each arc can take a token of its own for several.
    *first* is the sort position of its first token under the first source. Otherwise
    *every_head* maps every key that binds to that pair and *changes* is empty. ``None``
    when an index is not registered or not serving, exactly as for
    [`peek_join`][cpnx.Place.peek_join].
    """
    with self._lock:
        return self._store.join_head_changes(sources, generations)
```

### can_retrieve

```
can_retrieve(
    count: int = 1, model_time: float | None = None
) -> bool
```

Return `True` if at least *count* tokens are currently available for retrieval.

Parameters:

| Name         | Type    | Description                             | Default                                                                                                   |
| ------------ | ------- | --------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `count`      | `int`   | Number of tokens needed. Defaults to 1. | `1`                                                                                                       |
| `model_time` | \`float | None\`                                  | Optional logical clock timestamp used instead of wall-clock time to determine which tokens are available. |

Returns:

| Type   | Description                                                      |
| ------ | ---------------------------------------------------------------- |
| `bool` | True if at least count tokens have available_at at or before the |
| `bool` | effective time, False otherwise.                                 |

Source code in `src/cpnx/places.py`

```
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
        return self._store.has_available(t_limit, count)
```

### earliest_available_boundary

```
earliest_available_boundary(now: float) -> float | None
```

Smallest future `available_at` (strictly greater than *now*) held here, or `None`.

Backs the engine's logical-clock advance. O(1) for an untimed place (nothing is cooling), and O(log n) amortized otherwise, however many matured tokens are waiting here to be consumed, so the clock advance does not scan the marking on every tick. A *now* earlier than the previous boundary query's (for example the engine's wall-clock query followed by a switch to a small logical time) pays one O(n) re-split, after which queries from that time on are back to O(log n). Reads such as `peek` and `can_retrieve` do not affect this, whichever clock they use.

Source code in `src/cpnx/places.py`

```
def earliest_available_boundary(self, now: float) -> float | None:
    """Smallest future ``available_at`` (strictly greater than *now*) held here, or ``None``.

    Backs the engine's logical-clock advance. O(1) for an untimed place (nothing is
    cooling), and O(log n) amortized otherwise, however many matured tokens are waiting
    here to be consumed, so the clock advance does not scan the marking on every tick.
    A *now* earlier than the previous boundary query's (for example the engine's wall-clock
    query followed by a switch to a small logical time) pays one O(n) re-split, after which
    queries from that time on are back to O(log n). Reads such as `peek` and
    `can_retrieve` do not affect this, whichever clock they use.
    """
    with self._lock:
        return self._store.earliest_cooling_after(now)
```

### can_deposit

```
can_deposit(count: int = 1) -> bool
```

Return `True` if the place can accept *count* more tokens without exceeding its bound.

Implements k-bounded place semantics: a place with `bound=k` blocks when depositing would push the token count above `k`. Unbounded places (`bound=None`) always return `True`. Ignores colour — use `can_accept` to check colour compatibility.

Parameters:

| Name    | Type  | Description                                      | Default |
| ------- | ----- | ------------------------------------------------ | ------- |
| `count` | `int` | Number of tokens to be deposited. Defaults to 1. | `1`     |

Returns:

| Type   | Description                                                              |
| ------ | ------------------------------------------------------------------------ |
| `bool` | True if depositing count tokens would not exceed bound, False otherwise. |

Source code in `src/cpnx/places.py`

```
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
        return len(self._store) + count <= self.bound
```

### can_accept

```
can_accept(token: Token) -> bool
```

Return `True` if *token* is compatible with this place's colour set and schema.

This is a non-mutating pre-flight check that does not modify the place's tokens and does not consider capacity — use `can_deposit` for bound checks.

Parameters:

| Name    | Type    | Description                                                        | Default    |
| ------- | ------- | ------------------------------------------------------------------ | ---------- |
| `token` | `Token` | The token to check for colour compatibility and schema validation. | *required* |

Returns:

| Type   | Description                                                     |
| ------ | --------------------------------------------------------------- |
| `bool` | True if color_set is None or contains token's colour and        |
| `bool` | validate_schema returns True, False otherwise. Resource permit  |
| `bool` | tokens are exempt from the schema check (see \_enforce_schema). |

Source code in `src/cpnx/places.py`

```
def can_accept(self, token: Token) -> bool:
    """Return ``True`` if *token* is compatible with this place's colour set and schema.

    This is a non-mutating pre-flight check that does not modify the place's tokens
    and does not consider capacity — use `can_deposit` for bound checks.

    Args:
        token: The token to check for colour compatibility and schema validation.

    Returns:
        ``True`` if `color_set` is ``None`` or contains *token*'s colour and
        `validate_schema` returns ``True``, ``False`` otherwise. Resource permit
        tokens are exempt from the schema check (see `_enforce_schema`).
    """
    with self._lock:
        if self.color_set is not None and token.color not in self.color_set:
            return False
        if not token.is_resource and not self.validate_schema(token.payload):
            return False
        return True
```

### __len__

```
__len__() -> int
```

Return the number of tokens currently in the place.

Source code in `src/cpnx/places.py`

```
def __len__(self) -> int:
    """Return the number of tokens currently in the place."""
    with self._lock:
        return len(self._store)
```

### __bool__

```
__bool__() -> bool
```

A Place is always truthy, even when it contains no tokens.

Source code in `src/cpnx/places.py`

```
def __bool__(self) -> bool:
    """A Place is always truthy, even when it contains no tokens."""
    return True
```

## cpnx.ResourcePlace

Bases: `Place`

A Place pre-filled with *capacity* resource tokens, for modelling finite permits.

**CPN equivalent:** `Place(color_set={"resource"}, initial_marking=[Token(color="resource")] * capacity)`. This class is a Python shorthand — it sets the colour set and initial marking automatically and documents the resource-return invariant explicitly. It does not otherwise change Place's behavior: all inherited methods (`deposit`, `retrieve`, etc.) work exactly as on the base class. One exception: across a SubstitutionTransition boundary a port and its socket must both be `ResourcePlace`s or neither, so a plain `Place` written as above does not stand in for a pool on either side.

Resource tokens (`color="resource"`) are consumed when a transition fires and must be returned via a matching output arc. This models finite resources such as GPU slots, database connections, or thread-pool permits.

Example

```
gpu_pool = ResourcePlace("gpu_slots", capacity=4)
```

Source code in `src/cpnx/places.py`

````
class ResourcePlace(Place):
    """A [`Place`][cpnx.Place] pre-filled with *capacity* resource tokens, for modelling finite permits.

    **CPN equivalent:** ``Place(color_set={"resource"}, initial_marking=[Token(color="resource")] * capacity)``.
    This class is a Python shorthand — it sets the colour set and initial marking
    automatically and documents the resource-return invariant explicitly. It does not
    otherwise change [`Place`][cpnx.Place]'s behavior: all inherited methods (`deposit`,
    `retrieve`, etc.) work exactly as on the base class. One exception: across a
    [`SubstitutionTransition`][cpnx.SubstitutionTransition] boundary a port and its socket
    must both be `ResourcePlace`s or neither, so a plain `Place` written as above does not
    stand in for a pool on either side.

    Resource tokens (``color="resource"``) are consumed when a transition fires
    and must be returned via a matching output arc. This models finite resources
    such as GPU slots, database connections, or thread-pool permits.

    Example:
        ```python
        gpu_pool = ResourcePlace("gpu_slots", capacity=4)
        ```
    """

    _restorable: ClassVar[bool] = False

    def __init__(
        self,
        name: str,
        capacity: int,
        schema: type | Callable[[Any], bool] | None = None,
    ) -> None:
        """Create a ResourcePlace pre-filled with *capacity* resource tokens.

        Args:
            name: Unique identifier for this place within a [`PetriNet`][cpnx.PetriNet].
            capacity: Number of resource permits in the pool. ``0`` is valid
                      (creates an empty, permanently-blocking place).
            schema: Optional schema or type constraint for token payloads. Resource
                    permit tokens are exempt (they carry no payload). See [`Place`][cpnx.Place].
        """
        self.capacity = capacity
        super().__init__(
            name,
            color_set={"resource"},
            initial_marking=[Token(color="resource") for _ in range(capacity)],
            schema=schema,
        )

    def _roll_back_to(self, tokens: tuple[Token, ...]) -> bool:
        return self._restore_permits(tokens)

    def _restore_permits(self, tokens: tuple[Token, ...]) -> bool:
        """Bring the pool back to the number of permits in *tokens*, its checkpoint snapshot.

        Permits are interchangeable, and a settled firing has handed back every one it used, so
        only the count is rolled back. The permits here stay as they are: a surplus (a port's
        copy from the parent) gives up the soonest available, which keeps any cooldown running,
        and a shortfall (a permit left in another pool) is made up from the snapshot. Returns
        whether the contents changed.
        """
        with self._lock:
            current = list(self._store.iter_insertion_order())
            surplus = len(current) - len(tokens)
            if surplus == 0:
                return False
            if surplus > 0:
                dropped = {id(t) for t in sorted(current, key=lambda t: t.available_at)[:surplus]}
                kept = [t for t in current if id(t) not in dropped]
            else:
                present = Counter(id(t) for t in current)
                missing = []
                for token in tokens:
                    if present[id(token)] > 0:
                        present[id(token)] -= 1
                    else:
                        missing.append(token)
                kept = current + missing[:-surplus]
            self._store.drop_all()
            for token in kept:
                self._store.append(token)
            return True
````

### __init__

```
__init__(
    name: str,
    capacity: int,
    schema: type | Callable[[Any], bool] | None = None,
) -> None
```

Create a ResourcePlace pre-filled with *capacity* resource tokens.

Parameters:

| Name       | Type   | Description                                                                                        | Default    |
| ---------- | ------ | -------------------------------------------------------------------------------------------------- | ---------- |
| `name`     | `str`  | Unique identifier for this place within a PetriNet.                                                | *required* |
| `capacity` | `int`  | Number of resource permits in the pool. 0 is valid (creates an empty, permanently-blocking place). | *required* |
| `schema`   | \`type | Callable\[[Any], bool\]                                                                            | None\`     |

Source code in `src/cpnx/places.py`

```
def __init__(
    self,
    name: str,
    capacity: int,
    schema: type | Callable[[Any], bool] | None = None,
) -> None:
    """Create a ResourcePlace pre-filled with *capacity* resource tokens.

    Args:
        name: Unique identifier for this place within a [`PetriNet`][cpnx.PetriNet].
        capacity: Number of resource permits in the pool. ``0`` is valid
                  (creates an empty, permanently-blocking place).
        schema: Optional schema or type constraint for token payloads. Resource
                permit tokens are exempt (they carry no payload). See [`Place`][cpnx.Place].
    """
    self.capacity = capacity
    super().__init__(
        name,
        color_set={"resource"},
        initial_marking=[Token(color="resource") for _ in range(capacity)],
        schema=schema,
    )
```

## cpnx.PacedResourcePlace

Bases: `ResourcePlace`

A ResourcePlace where returned tokens must cool down before becoming reusable.

**CPN equivalent:** a Timed CPN ResourcePlace where returned tokens carry a timestamp that prevents re-use until `pacing_secs` have elapsed. This is a pragmatic extension — standard Timed CPNs put timestamps on tokens, not cooldown windows on places.

Useful for enforcing API rate limits or minimum inter-request intervals. Tokens are available immediately at construction; after each return via deposit, they are unavailable for *pacing_secs* seconds.

Example — 10 Serper requests per second:

```
serper = PacedResourcePlace("serper", capacity=10, pacing_secs=0.1)
```

Source code in `src/cpnx/places.py`

````
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

    def __init__(
        self,
        name: str,
        capacity: int,
        pacing_secs: float,
        schema: type | Callable[[Any], bool] | None = None,
    ) -> None:
        """Create a PacedResourcePlace.

        Args:
            name: Unique identifier for this place within a [`PetriNet`][cpnx.PetriNet].
            capacity: Number of resource permits in the pool.
            pacing_secs: Seconds a token must wait after being returned before
                         it becomes available again.
            schema: Optional schema or type constraint for token payloads. Resource
                    permit tokens are exempt (they carry no payload). See [`Place`][cpnx.Place].
        """
        self.pacing_secs = pacing_secs
        super().__init__(name, capacity, schema=schema)

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

        Raises:
            TypeError: If `schema` is set and *token*'s payload fails validation. Resource
                permit tokens (the usual case here) are exempt — see `_enforce_schema`.
        """
        with self._lock:
            self._enforce_schema(token)
            ref_time = model_time if model_time is not None else time.monotonic()
            # Create a new token with updated availability timestamp (stateless place cooldown)
            timed_token = token.evolve(available_at=ref_time + self.pacing_secs, id=token.id)
            self._store.append(timed_token)
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
            return self._store.has_available(t_limit, count)

    def retrieve(self, count: int = 1, model_time: float | None = None) -> list[Token]:
        """Remove and return *count* tokens whose cooldown has expired, in expiry order.

        Behaves like [`Place.retrieve`][cpnx.Place.retrieve] but the error message reports
        how many tokens are still cooling down, which is specific to this class's semantics.

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
            if not self._store.has_available(t_limit, count):
                available_count = self._store.count_available(t_limit)
                cooling = len(self._store) - available_count
                raise ValueError(
                    f"PacedResourcePlace '{self.name}': {available_count} token(s) ready, "
                    f"{count} requested — {cooling} token(s) still in cooldown "
                    f"(pacing_secs={self.pacing_secs})."
                )
            return self._store.take_available(count, t_limit)

    def peek(self, count: int = 1, model_time: float | None = None) -> list[Token]:
        """Return up to *count* cooled-down tokens without removing them.

        Behaves identically to [`Place.peek`][cpnx.Place.peek]; "available" specifically
        means "cooldown has expired" for this class.

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
            return self._store.peek_available(count, t_limit)
````

### __init__

```
__init__(
    name: str,
    capacity: int,
    pacing_secs: float,
    schema: type | Callable[[Any], bool] | None = None,
) -> None
```

Create a PacedResourcePlace.

Parameters:

| Name          | Type    | Description                                                                       | Default    |
| ------------- | ------- | --------------------------------------------------------------------------------- | ---------- |
| `name`        | `str`   | Unique identifier for this place within a PetriNet.                               | *required* |
| `capacity`    | `int`   | Number of resource permits in the pool.                                           | *required* |
| `pacing_secs` | `float` | Seconds a token must wait after being returned before it becomes available again. | *required* |
| `schema`      | \`type  | Callable\[[Any], bool\]                                                           | None\`     |

Source code in `src/cpnx/places.py`

```
def __init__(
    self,
    name: str,
    capacity: int,
    pacing_secs: float,
    schema: type | Callable[[Any], bool] | None = None,
) -> None:
    """Create a PacedResourcePlace.

    Args:
        name: Unique identifier for this place within a [`PetriNet`][cpnx.PetriNet].
        capacity: Number of resource permits in the pool.
        pacing_secs: Seconds a token must wait after being returned before
                     it becomes available again.
        schema: Optional schema or type constraint for token payloads. Resource
                permit tokens are exempt (they carry no payload). See [`Place`][cpnx.Place].
    """
    self.pacing_secs = pacing_secs
    super().__init__(name, capacity, schema=schema)
```

### deposit

```
deposit(
    token: Token, model_time: float | None = None
) -> None
```

Return a resource token to the pool, replacing its `available_at` to start a cooldown timer.

Differs from Place.deposit: instead of appending *token* unchanged, this creates a copy of *token* with `available_at` set to the effective time plus `pacing_secs`, so the token cannot be retrieved again until the cooldown elapses. Does not validate `color_set` (unlike the base class).

Parameters:

| Name         | Type    | Description                                                    | Default                                                                                                                                      |
| ------------ | ------- | -------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `token`      | `Token` | The resource token being returned. Must have color="resource". | *required*                                                                                                                                   |
| `model_time` | \`float | None\`                                                         | Optional logical clock timestamp used instead of wall-clock time as the cooldown's start reference, and recorded in last_deposit_time_model. |

Raises:

| Type        | Description                                                                                                                            |
| ----------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `TypeError` | If schema is set and token's payload fails validation. Resource permit tokens (the usual case here) are exempt — see \_enforce_schema. |

Source code in `src/cpnx/places.py`

```
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

    Raises:
        TypeError: If `schema` is set and *token*'s payload fails validation. Resource
            permit tokens (the usual case here) are exempt — see `_enforce_schema`.
    """
    with self._lock:
        self._enforce_schema(token)
        ref_time = model_time if model_time is not None else time.monotonic()
        # Create a new token with updated availability timestamp (stateless place cooldown)
        timed_token = token.evolve(available_at=ref_time + self.pacing_secs, id=token.id)
        self._store.append(timed_token)
        self.last_deposit_time = time.monotonic()
        if model_time is not None:
            self.last_deposit_time_model = model_time
        self._on_deposit(timed_token)
```

### can_retrieve

```
can_retrieve(
    count: int = 1, model_time: float | None = None
) -> bool
```

Return `True` if at least *count* tokens have completed their cooldown and are usable.

Behaves identically to Place.can_retrieve; documented separately here because "available" specifically means "cooldown has expired" for this class.

Parameters:

| Name         | Type    | Description                                         | Default                                                                                                                |
| ------------ | ------- | --------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `count`      | `int`   | Number of cooled-down tokens needed. Defaults to 1. | `1`                                                                                                                    |
| `model_time` | \`float | None\`                                              | Optional logical clock timestamp used instead of wall-clock time to determine which tokens have finished cooling down. |

Returns:

| Type   | Description                                                             |
| ------ | ----------------------------------------------------------------------- |
| `bool` | True if at least count tokens are past their cooldown, False otherwise. |

Source code in `src/cpnx/places.py`

```
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
        return self._store.has_available(t_limit, count)
```

### retrieve

```
retrieve(
    count: int = 1, model_time: float | None = None
) -> list[Token]
```

Remove and return *count* tokens whose cooldown has expired, in expiry order.

Behaves like Place.retrieve but the error message reports how many tokens are still cooling down, which is specific to this class's semantics.

Parameters:

| Name         | Type    | Description                                              | Default                                                                                                                |
| ------------ | ------- | -------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `count`      | `int`   | Number of cooled-down tokens to retrieve. Defaults to 1. | `1`                                                                                                                    |
| `model_time` | \`float | None\`                                                   | Optional logical clock timestamp used instead of wall-clock time to determine which tokens have finished cooling down. |

Returns:

| Type          | Description                                                 |
| ------------- | ----------------------------------------------------------- |
| `list[Token]` | List of retrieved resource tokens in cooldown-expiry order. |

Raises:

| Type         | Description                                                                                                             |
| ------------ | ----------------------------------------------------------------------------------------------------------------------- |
| `ValueError` | If fewer than count tokens are past their cooldown, with a message indicating how many are ready vs still cooling down. |

Source code in `src/cpnx/places.py`

```
def retrieve(self, count: int = 1, model_time: float | None = None) -> list[Token]:
    """Remove and return *count* tokens whose cooldown has expired, in expiry order.

    Behaves like [`Place.retrieve`][cpnx.Place.retrieve] but the error message reports
    how many tokens are still cooling down, which is specific to this class's semantics.

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
        if not self._store.has_available(t_limit, count):
            available_count = self._store.count_available(t_limit)
            cooling = len(self._store) - available_count
            raise ValueError(
                f"PacedResourcePlace '{self.name}': {available_count} token(s) ready, "
                f"{count} requested — {cooling} token(s) still in cooldown "
                f"(pacing_secs={self.pacing_secs})."
            )
        return self._store.take_available(count, t_limit)
```

### peek

```
peek(
    count: int = 1, model_time: float | None = None
) -> list[Token]
```

Return up to *count* cooled-down tokens without removing them.

Behaves identically to Place.peek; "available" specifically means "cooldown has expired" for this class.

Parameters:

| Name         | Type    | Description                                         | Default                                                                                                                |
| ------------ | ------- | --------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `count`      | `int`   | Maximum number of tokens to inspect. Defaults to 1. | `1`                                                                                                                    |
| `model_time` | \`float | None\`                                              | Optional logical clock timestamp used instead of wall-clock time to determine which tokens have finished cooling down. |

Returns:

| Type          | Description                                                            |
| ------------- | ---------------------------------------------------------------------- |
| `list[Token]` | List of up to count cooled-down tokens; may be shorter than requested. |
| `list[Token]` | Does not modify the pool.                                              |

Source code in `src/cpnx/places.py`

```
def peek(self, count: int = 1, model_time: float | None = None) -> list[Token]:
    """Return up to *count* cooled-down tokens without removing them.

    Behaves identically to [`Place.peek`][cpnx.Place.peek]; "available" specifically
    means "cooldown has expired" for this class.

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
        return self._store.peek_available(count, t_limit)
```

## cpnx.ThresholdPlace

Bases: `Place`

A Place where tokens are only retrievable once the queue depth reaches *threshold*.

**CPN equivalent:** a plain Place whose associated transition has a guard requiring `|M(p)| >= threshold` before firing. This class is a Python shorthand that encodes the threshold directly on the place rather than duplicating it in every downstream transition's guard.

Useful for batch processing: tokens accumulate until enough are present, then they are released in groups matching the transition's `arc.count`. `deposit` and `peek` are inherited unchanged from Place.

Example — convene a committee once 6 validated leads are ready:

```
validated = ThresholdPlace("validated_leads", threshold=6)
```

Source code in `src/cpnx/places.py`

````
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

    def __init__(
        self,
        name: str,
        threshold: int,
        schema: type | Callable[[Any], bool] | None = None,
    ) -> None:
        """Create a ThresholdPlace.

        Args:
            name: Unique identifier for this place within a [`PetriNet`][cpnx.PetriNet].
            threshold: Minimum queue depth required before any retrieval is
                       permitted. Must be >= 1.
            schema: Optional schema or type constraint for token payloads. Resource
                    permit tokens are exempt (they carry no payload). See [`Place`][cpnx.Place].
        """
        super().__init__(name, schema=schema)
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
            return self._store.has_available(t_limit, max(self.threshold, count))

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
            if self._store.has_available(t_limit, max(self.threshold, count)):
                return self._store.take_available(count, t_limit)
            available_count = self._store.count_available(t_limit)
            if available_count < self.threshold:
                raise ValueError(
                    f"ThresholdPlace '{self.name}': threshold of {self.threshold} not met "
                    f"({available_count} token(s) available — need {self.threshold - available_count} more)."
                )
            raise ValueError(
                f"ThresholdPlace '{self.name}': threshold met but only {available_count} "
                f"token(s) available, {count} requested."
            )

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
            if self._store.has_available(t_limit, self.threshold):
                return self._store.take_available(len(self._store), t_limit)
            available_count = self._store.count_available(t_limit)
            raise ValueError(
                f"ThresholdPlace '{self.name}': threshold of {self.threshold} not met "
                f"({available_count} token(s) available — need {self.threshold - available_count} more)."
            )
````

### __init__

```
__init__(
    name: str,
    threshold: int,
    schema: type | Callable[[Any], bool] | None = None,
) -> None
```

Create a ThresholdPlace.

Parameters:

| Name        | Type   | Description                                                                   | Default    |
| ----------- | ------ | ----------------------------------------------------------------------------- | ---------- |
| `name`      | `str`  | Unique identifier for this place within a PetriNet.                           | *required* |
| `threshold` | `int`  | Minimum queue depth required before any retrieval is permitted. Must be >= 1. | *required* |
| `schema`    | \`type | Callable\[[Any], bool\]                                                       | None\`     |

Source code in `src/cpnx/places.py`

```
def __init__(
    self,
    name: str,
    threshold: int,
    schema: type | Callable[[Any], bool] | None = None,
) -> None:
    """Create a ThresholdPlace.

    Args:
        name: Unique identifier for this place within a [`PetriNet`][cpnx.PetriNet].
        threshold: Minimum queue depth required before any retrieval is
                   permitted. Must be >= 1.
        schema: Optional schema or type constraint for token payloads. Resource
                permit tokens are exempt (they carry no payload). See [`Place`][cpnx.Place].
    """
    super().__init__(name, schema=schema)
    self.threshold = threshold
```

### can_retrieve

```
can_retrieve(
    count: int = 1, model_time: float | None = None
) -> bool
```

Return `True` only if the batch threshold is met AND at least *count* tokens are present.

Differs from Place.can_retrieve: adds a gating condition on top of the plain count check — the queue must have reached `threshold` regardless of *count*, and separately contain at least *count* available tokens (*count* may exceed `threshold`).

Parameters:

| Name         | Type    | Description                                                              | Default                                                                                                   |
| ------------ | ------- | ------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------- |
| `count`      | `int`   | Number of tokens needed by the requesting transition arc. Defaults to 1. | `1`                                                                                                       |
| `model_time` | \`float | None\`                                                                   | Optional logical clock timestamp used instead of wall-clock time to determine which tokens are available. |

Returns:

| Type   | Description                                                            |
| ------ | ---------------------------------------------------------------------- |
| `bool` | True if both the threshold and count conditions hold, False otherwise. |

Source code in `src/cpnx/places.py`

```
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
        return self._store.has_available(t_limit, max(self.threshold, count))
```

### retrieve

```
retrieve(
    count: int = 1, model_time: float | None = None
) -> list[Token]
```

Remove and return *count* tokens from the head of the queue, but only if the threshold is met.

Differs from Place.retrieve: first checks that the queue has reached `threshold` available tokens (raising if not) before applying the usual *count* check, gating retrieval behind the batch threshold.

Parameters:

| Name         | Type    | Description                                  | Default                                                                                                   |
| ------------ | ------- | -------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `count`      | `int`   | Number of tokens to retrieve. Defaults to 1. | `1`                                                                                                       |
| `model_time` | \`float | None\`                                       | Optional logical clock timestamp used instead of wall-clock time to determine which tokens are available. |

Returns:

| Type          | Description                             |
| ------------- | --------------------------------------- |
| `list[Token]` | List of retrieved tokens in FIFO order. |

Raises:

| Type         | Description                                                                                  |
| ------------ | -------------------------------------------------------------------------------------------- |
| `ValueError` | If the threshold is not yet met, with a message showing current depth vs required threshold. |
| `ValueError` | If the threshold is met but fewer than count tokens are available.                           |

Source code in `src/cpnx/places.py`

```
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
        if self._store.has_available(t_limit, max(self.threshold, count)):
            return self._store.take_available(count, t_limit)
        available_count = self._store.count_available(t_limit)
        if available_count < self.threshold:
            raise ValueError(
                f"ThresholdPlace '{self.name}': threshold of {self.threshold} not met "
                f"({available_count} token(s) available — need {self.threshold - available_count} more)."
            )
        raise ValueError(
            f"ThresholdPlace '{self.name}': threshold met but only {available_count} "
            f"token(s) available, {count} requested."
        )
```

### retrieve_all

```
retrieve_all(
    model_time: float | None = None,
) -> list[Token]
```

Remove and return every available token, but only if the threshold has been met.

Differs from Place.retrieve_all: raises instead of returning an empty list when fewer than `threshold` tokens are available.

Parameters:

| Name         | Type    | Description | Default                                                                                                   |
| ------------ | ------- | ----------- | --------------------------------------------------------------------------------------------------------- |
| `model_time` | \`float | None\`      | Optional logical clock timestamp used instead of wall-clock time to determine which tokens are available. |

Returns:

| Type          | Description                         |
| ------------- | ----------------------------------- |
| `list[Token]` | All available tokens in FIFO order. |

Raises:

| Type         | Description                                                                                  |
| ------------ | -------------------------------------------------------------------------------------------- |
| `ValueError` | If the threshold is not yet met, with a message showing current depth vs required threshold. |

Source code in `src/cpnx/places.py`

```
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
        if self._store.has_available(t_limit, self.threshold):
            return self._store.take_available(len(self._store), t_limit)
        available_count = self._store.count_available(t_limit)
        raise ValueError(
            f"ThresholdPlace '{self.name}': threshold of {self.threshold} not met "
            f"({available_count} token(s) available — need {self.threshold - available_count} more)."
        )
```

## cpnx.SinkPlace

Bases: `Place`

A terminal place that counts and optionally samples tokens but never retains them for retrieval.

Deposited tokens are absorbed: their colour and cumulative counts are recorded, and up to `keep_last` of the most recent tokens are kept in a ring buffer purely for inspection via `tokens`/`stats`/`drain_stats` — but they can never be consumed onward by a transition. Useful for streaming pipelines (e.g. logging sinks, dead-letter/error places) to avoid accumulating memory indefinitely while still exposing aggregate statistics.

Note

A sink is a terminal ring buffer, not a retrievable queue, so it does NOT use `_TokenStore` internally — it keeps its own bounded `collections.deque(maxlen=keep_last)` and overrides every method that would otherwise touch the base class's store.

Warning

Avoid setting a restrictive `color_set` if this place is used as an `error_place`. Dead-lettered tokens preserve their original colours, and depositing a rejected colour will raise a `TypeError` inside the locked transition failure branch, causing the token to be lost rather than successfully dead-lettered.

Source code in `src/cpnx/places.py`

````
class SinkPlace(Place):
    """A terminal place that counts and optionally samples tokens but never retains them for retrieval.

    Deposited tokens are absorbed: their colour and cumulative counts are recorded, and up
    to `keep_last` of the most recent tokens are kept in a ring buffer purely for inspection
    via `tokens`/`stats`/`drain_stats` — but they can never be consumed onward by a transition.
    Useful for streaming pipelines (e.g. logging sinks, dead-letter/error places) to avoid
    accumulating memory indefinitely while still exposing aggregate statistics.

    Note:
        A sink is a terminal ring buffer, not a retrievable queue, so it does NOT use
        `_TokenStore` internally — it keeps its own bounded
        `collections.deque(maxlen=keep_last)` and overrides every method that would
        otherwise touch the base class's store.

    Warning:
        Avoid setting a restrictive `color_set` if this place is used as an `error_place`.
        Dead-lettered tokens preserve their original colours, and depositing a rejected
        colour will raise a `TypeError` inside the locked transition failure branch,
        causing the token to be lost rather than successfully dead-lettered.
    """

    _restorable: ClassVar[bool] = False

    def __init__(
        self,
        name: str,
        *,
        keep_last: int = 0,
        color_set: set[str] | None = None,
        schema: type | Callable[[Any], bool] | None = None,
    ) -> None:
        """Create a new SinkPlace.

        Args:
            name: Unique identifier for this place within a [`PetriNet`][cpnx.PetriNet].
            keep_last: Number of most recent tokens to keep in a ring buffer for inspection.
                       Default is 0 (retain nothing beyond the aggregate counters).
            color_set: Set of accepted token colours. ``None`` (default) accepts any colour.
                       Do not use a restrictive color_set if used as an error_place.
            schema: Optional schema or type constraint for token payloads. Resource
                    permit tokens are exempt (they carry no payload). See [`Place`][cpnx.Place].
        """
        super().__init__(name, bound=None, color_set=color_set, schema=schema)
        self.keep_last = keep_last
        self._kept: deque[Token] = deque(maxlen=keep_last)
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
            TypeError: If `color_set` is set and *token*'s colour is not in it,
                       or if `schema` is set and *token*'s payload fails validation.
        """
        with self._lock:
            if self.color_set is not None and token.color not in self.color_set:
                raise TypeError(
                    f"Place '{self.name}' has color_set {self.color_set!r} — "
                    f"cannot deposit token with color {token.color!r}."
                )
            self._enforce_schema(token)
            self._kept.append(token)
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

    @property
    def tokens(self) -> tuple[Token, ...]:
        """Snapshot of the ring buffer's currently-kept tokens (most recent `keep_last`), as a tuple.

        Differs from [`Place.tokens`][cpnx.Place.tokens]: reads the sink's own bounded
        `deque` ring buffer rather than a `_TokenStore` (a sink never routes deposits
        through the store).
        """
        with self._lock:
            return tuple(self._kept)

    def __len__(self) -> int:
        """Return the number of tokens currently kept in the ring buffer (at most `keep_last`).

        Differs from [`Place.__len__`][cpnx.Place.__len__]: reflects the ring buffer's
        size, not a `_TokenStore`'s — a sink's "length" is its sample size, not its
        (unbounded) absorbed count (see `stats()["absorbed"]` for that).
        """
        with self._lock:
            return len(self._kept)

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
                "kept": len(self._kept),
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
                "kept": len(self._kept),
                "first_deposit_time": self._first_deposit_time,
                "last_deposit_time": self.last_deposit_time,
            }
            self._absorbed = 0
            self._by_color = {}
            self._first_deposit_time = None
            return snapshot
````

### tokens

```
tokens: tuple[Token, ...]
```

Snapshot of the ring buffer's currently-kept tokens (most recent `keep_last`), as a tuple.

Differs from Place.tokens: reads the sink's own bounded `deque` ring buffer rather than a `_TokenStore` (a sink never routes deposits through the store).

### __init__

```
__init__(
    name: str,
    *,
    keep_last: int = 0,
    color_set: set[str] | None = None,
    schema: type | Callable[[Any], bool] | None = None,
) -> None
```

Create a new SinkPlace.

Parameters:

| Name        | Type       | Description                                                                                                                        | Default                                                                                                                         |
| ----------- | ---------- | ---------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `name`      | `str`      | Unique identifier for this place within a PetriNet.                                                                                | *required*                                                                                                                      |
| `keep_last` | `int`      | Number of most recent tokens to keep in a ring buffer for inspection. Default is 0 (retain nothing beyond the aggregate counters). | `0`                                                                                                                             |
| `color_set` | \`set[str] | None\`                                                                                                                             | Set of accepted token colours. None (default) accepts any colour. Do not use a restrictive color_set if used as an error_place. |
| `schema`    | \`type     | Callable\[[Any], bool\]                                                                                                            | None\`                                                                                                                          |

Source code in `src/cpnx/places.py`

```
def __init__(
    self,
    name: str,
    *,
    keep_last: int = 0,
    color_set: set[str] | None = None,
    schema: type | Callable[[Any], bool] | None = None,
) -> None:
    """Create a new SinkPlace.

    Args:
        name: Unique identifier for this place within a [`PetriNet`][cpnx.PetriNet].
        keep_last: Number of most recent tokens to keep in a ring buffer for inspection.
                   Default is 0 (retain nothing beyond the aggregate counters).
        color_set: Set of accepted token colours. ``None`` (default) accepts any colour.
                   Do not use a restrictive color_set if used as an error_place.
        schema: Optional schema or type constraint for token payloads. Resource
                permit tokens are exempt (they carry no payload). See [`Place`][cpnx.Place].
    """
    super().__init__(name, bound=None, color_set=color_set, schema=schema)
    self.keep_last = keep_last
    self._kept: deque[Token] = deque(maxlen=keep_last)
    self._absorbed = 0
    self._by_color: dict[str | None, int] = {}
    self._first_deposit_time: float | None = None
```

### deposit

```
deposit(
    token: Token, model_time: float | None = None
) -> None
```

Absorb *token*: append it to the ring buffer and update cumulative counters and timestamps.

Differs from Place.deposit: the internal deque has `maxlen=keep_last`, so once full, appending silently evicts the oldest kept token — this is a sampling buffer, not the full token history. Also increments the `_absorbed` count and the per-colour tally, and records `_first_deposit_time` on the very first deposit.

Parameters:

| Name         | Type    | Description          | Default                                                               |
| ------------ | ------- | -------------------- | --------------------------------------------------------------------- |
| `token`      | `Token` | The token to absorb. | *required*                                                            |
| `model_time` | \`float | None\`               | Optional logical clock timestamp recorded in last_deposit_time_model. |

Raises:

| Type        | Description                                                                                                    |
| ----------- | -------------------------------------------------------------------------------------------------------------- |
| `TypeError` | If color_set is set and token's colour is not in it, or if schema is set and token's payload fails validation. |

Source code in `src/cpnx/places.py`

```
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
        TypeError: If `color_set` is set and *token*'s colour is not in it,
                   or if `schema` is set and *token*'s payload fails validation.
    """
    with self._lock:
        if self.color_set is not None and token.color not in self.color_set:
            raise TypeError(
                f"Place '{self.name}' has color_set {self.color_set!r} — "
                f"cannot deposit token with color {token.color!r}."
            )
        self._enforce_schema(token)
        self._kept.append(token)
        now = time.monotonic()
        self.last_deposit_time = now
        if self._first_deposit_time is None:
            self._first_deposit_time = now
        if model_time is not None:
            self.last_deposit_time_model = model_time

        self._absorbed += 1
        self._by_color[token.color] = self._by_color.get(token.color, 0) + 1

        self._on_deposit(token)
```

### can_retrieve

```
can_retrieve(
    count: int = 1, model_time: float | None = None
) -> bool
```

Always return `False`: a sink is a terminal place, so nothing is ever retrievable.

Differs from Place.can_retrieve: arriving tokens are absorbed for inspection/counting only and can never be consumed onward by a transition, so this unconditionally reports nothing is retrievable. *count* and *model_time* are accepted for interface compatibility but ignored.

Parameters:

| Name         | Type    | Description | Default  |
| ------------ | ------- | ----------- | -------- |
| `count`      | `int`   | Ignored.    | `1`      |
| `model_time` | \`float | None\`      | Ignored. |

Returns:

| Type   | Description    |
| ------ | -------------- |
| `bool` | False, always. |

Source code in `src/cpnx/places.py`

```
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
```

### retrieve

```
retrieve(
    count: int = 1, model_time: float | None = None
) -> list[Token]
```

Always raise: a sink is terminal, so tokens cannot be retrieved by any means.

Differs from Place.retrieve: never returns tokens, since absorbed tokens are only for inspection, not downstream consumption. *count* and *model_time* are accepted for interface compatibility but ignored.

Parameters:

| Name         | Type    | Description | Default  |
| ------------ | ------- | ----------- | -------- |
| `count`      | `int`   | Ignored.    | `1`      |
| `model_time` | \`float | None\`      | Ignored. |

Raises:

| Type         | Description                                                                          |
| ------------ | ------------------------------------------------------------------------------------ |
| `ValueError` | Always, with message "SinkPlace is terminal — tokens are absorbed, not retrievable". |

Source code in `src/cpnx/places.py`

```
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
```

### retrieve_specific

```
retrieve_specific(
    tokens: list[Token], model_time: float | None = None
) -> list[Token]
```

Always raise: a sink is terminal, so no tokens — specific or otherwise — can be retrieved.

Differs from Place.retrieve_specific: never returns tokens, since absorbed tokens are only for inspection. *tokens* and *model_time* are accepted for interface compatibility but ignored.

Parameters:

| Name         | Type          | Description | Default    |
| ------------ | ------------- | ----------- | ---------- |
| `tokens`     | `list[Token]` | Ignored.    | *required* |
| `model_time` | \`float       | None\`      | Ignored.   |

Raises:

| Type         | Description                                                                          |
| ------------ | ------------------------------------------------------------------------------------ |
| `ValueError` | Always, with message "SinkPlace is terminal — tokens are absorbed, not retrievable". |

Source code in `src/cpnx/places.py`

```
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
```

### retrieve_all

```
retrieve_all(
    model_time: float | None = None,
) -> list[Token]
```

Always raise: a sink is terminal, so its absorbed tokens can never be drained via retrieval.

Differs from Place.retrieve_all: never returns tokens. Use `drain_stats` to reset the aggregate counters instead. *model_time* is accepted for interface compatibility but ignored.

Parameters:

| Name         | Type    | Description | Default  |
| ------------ | ------- | ----------- | -------- |
| `model_time` | \`float | None\`      | Ignored. |

Raises:

| Type         | Description                                                                          |
| ------------ | ------------------------------------------------------------------------------------ |
| `ValueError` | Always, with message "SinkPlace is terminal — tokens are absorbed, not retrievable". |

Source code in `src/cpnx/places.py`

```
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
```

### peek

```
peek(
    count: int = 1, model_time: float | None = None
) -> list[Token]
```

Always raise: use the `tokens` property to inspect a sink's ring buffer instead.

Differs from Place.peek: rather than returning a possibly-empty list, this raises, since a sink's kept tokens are only meant to be read via `tokens` or `stats`. *count* and *model_time* are accepted for interface compatibility but ignored.

Parameters:

| Name         | Type    | Description | Default  |
| ------------ | ------- | ----------- | -------- |
| `count`      | `int`   | Ignored.    | `1`      |
| `model_time` | \`float | None\`      | Ignored. |

Raises:

| Type         | Description                                                                          |
| ------------ | ------------------------------------------------------------------------------------ |
| `ValueError` | Always, with message "SinkPlace is terminal — tokens are absorbed, not retrievable". |

Source code in `src/cpnx/places.py`

```
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
```

### can_deposit

```
can_deposit(count: int = 1) -> bool
```

Always return `True`: a sink has unbounded capacity and absorbs every token offered.

Differs from Place.can_deposit: ignores `bound` entirely (a `SinkPlace` is constructed with `bound=None` and never rejects on capacity grounds — only `color_set` can reject a deposit).

Parameters:

| Name    | Type  | Description | Default |
| ------- | ----- | ----------- | ------- |
| `count` | `int` | Ignored.    | `1`     |

Returns:

| Type   | Description   |
| ------ | ------------- |
| `bool` | True, always. |

Source code in `src/cpnx/places.py`

```
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
```

### __len__

```
__len__() -> int
```

Return the number of tokens currently kept in the ring buffer (at most `keep_last`).

Differs from Place.__len__: reflects the ring buffer's size, not a `_TokenStore`'s — a sink's "length" is its sample size, not its (unbounded) absorbed count (see `stats()["absorbed"]` for that).

Source code in `src/cpnx/places.py`

```
def __len__(self) -> int:
    """Return the number of tokens currently kept in the ring buffer (at most `keep_last`).

    Differs from [`Place.__len__`][cpnx.Place.__len__]: reflects the ring buffer's
    size, not a `_TokenStore`'s — a sink's "length" is its sample size, not its
    (unbounded) absorbed count (see `stats()["absorbed"]` for that).
    """
    with self._lock:
        return len(self._kept)
```

### stats

```
stats() -> dict
```

Return a snapshot of cumulative statistics of absorbed tokens, without resetting any counters.

Example

```
sink = SinkPlace("errors", keep_last=10)
sink.deposit(Token(color="error"))
sink.stats()
# {"name": "errors", "absorbed": 1, "by_color": {"error": 1}, "kept": 1,
#  "first_deposit_time": ..., "last_deposit_time": ...}
```

Returns:

| Type   | Description                                                             |
| ------ | ----------------------------------------------------------------------- |
| `dict` | Dictionary with keys name (str), absorbed (total tokens ever deposited, |
| `dict` | int), by_color (dict mapping colour to count), kept (number of tokens   |
| `dict` | currently in the ring buffer), first_deposit_time (float or None if     |
| `dict` | nothing has been deposited), and last_deposit_time (float, 0.0 if       |
| `dict` | nothing has been deposited).                                            |

Source code in `src/cpnx/places.py`

````
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
            "kept": len(self._kept),
            "first_deposit_time": self._first_deposit_time,
            "last_deposit_time": self.last_deposit_time,
        }
````

### drain_stats

```
drain_stats() -> dict
```

Atomically return the current stats snapshot and reset the cumulative counters to zero.

Differs from `stats`: after returning the snapshot, resets `_absorbed` to 0, `_by_color` to an empty dict, and `_first_deposit_time` to `None` (the ring buffer of kept tokens and `last_deposit_time` are left untouched). Useful for periodic reporting where each report should cover only the interval since the last drain.

Returns:

| Type   | Description                                                                 |
| ------ | --------------------------------------------------------------------------- |
| `dict` | The stats dictionary as it was immediately before the reset — same shape as |
| `dict` | stats (name, absorbed, by_color, kept, first_deposit_time,                  |
| `dict` | last_deposit_time).                                                         |

Source code in `src/cpnx/places.py`

```
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
            "kept": len(self._kept),
            "first_deposit_time": self._first_deposit_time,
            "last_deposit_time": self.last_deposit_time,
        }
        self._absorbed = 0
        self._by_color = {}
        self._first_deposit_time = None
        return snapshot
```
