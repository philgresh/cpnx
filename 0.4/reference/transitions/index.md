# Transitions

Transitions fire work over the thread pool; arcs connect them to places.

## cpnx.Transition

A CPN transition that fires when all its input places are enabled.

In CPN formalism a transition has:

- **Input arcs** whose `key`/`filter` determine which tokens are consumed
- **Output arcs** whose `condition` determines whether tokens are produced
- A **guard** — a boolean predicate over the binding that must hold for the transition to be enabled (evaluated before the action runs)

Attributes:

| Name                   | Type                                   | Description                                                                                                   |
| ---------------------- | -------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `name`                 | `str`                                  | Unique identifier within a PetriNet.                                                                          |
| `inputs`               | `list[InputArc]`                       | Input arcs (place to transition).                                                                             |
| `outputs`              | `list[OutputArc]`                      | Output arcs (transition to place).                                                                            |
| `action`               | `Callable[[list[Token]], list[Token]]` | Callable consuming input tokens and returning output tokens. Runs on the thread pool outside the engine lock. |
| `guard`                | \`Callable\[\[list[Token]\], bool\]    | None\`                                                                                                        |
| `priority`             | `int`                                  | Lower value fires first when multiple transitions are enabled. Defaults to 10.                                |
| `action_timeout_secs`  | \`float                                | None\`                                                                                                        |
| `max_retries`          | \`int                                  | None\`                                                                                                        |
| `binding_policy`       | \`BindingPolicy                        | None\`                                                                                                        |
| `binding_priority_key` | \`Callable\[\[list[Token]\], object\]  | None\`                                                                                                        |

Source code in `src/cpnx/transitions.py`

```
@dataclass
class Transition:
    """A CPN transition that fires when all its input places are enabled.

    In CPN formalism a transition has:

    - **Input arcs** whose `key`/`filter` determine which tokens are consumed
    - **Output arcs** whose `condition` determines whether tokens are produced
    - A **guard** — a boolean predicate over the binding that must hold for the
      transition to be enabled (evaluated before the action runs)

    Attributes:
        name: Unique identifier within a [`PetriNet`][cpnx.PetriNet].
        inputs: Input arcs (place to transition).
        outputs: Output arcs (transition to place).
        action: Callable consuming input tokens and returning output tokens.
                Runs on the thread pool outside the engine lock.
        guard: CPN transition guard — a `Callable[[list[Token]], bool]` (a string raises
               `TypeError`). Evaluated while holding the engine lock; return `False` to
               block firing. `None` (default) means always enabled. A certified guard
               runs inline; an uncertified one runs on the timeout-bounded pool. Enforces
               the CPN guard contract `Type[G(t)] = Bool`: if the callable is annotated,
               its return type is checked to be `bool` at construction and a non-`bool`
               annotation (e.g. `-> int`) raises `TypeError`; unannotated guards (including
               every lambda) are accepted unchecked.
        priority: Lower value fires first when multiple transitions are enabled. Defaults to 10.
        action_timeout_secs: Maximum wall-clock seconds the action may run. When `None`
               (default), the action runs without a deadline — fully backward compatible.
               When set, the engine triggers atomic rollback if the action does not return
               within this duration: all consumed tokens are returned to their source places
               (data tokens with a 1-second `available_at` delay to prevent livelock), and
               the `on_error` callback fires with a `RuntimeError` that names the
               transition and the elapsed timeout.

               **This does not kill the underlying OS thread.** The timed-out action
               continues running in the background until it completes or the process exits;
               its return value is silently discarded. Callers must apply native I/O
               timeouts inside their actions (e.g. `requests.get(timeout=...)`,
               `httpx` client timeouts) to prevent zombie thread accumulation.
               `action_timeout_secs` is defense-in-depth, not a substitute for proper
               I/O timeout discipline.
        max_retries: Maximum number of times to retry the transition action on failure
               before dead-lettering the data tokens.
               `None` means infinite retry (today's behavior; forfeits the quiescence guarantee).
               `0` means route data token(s) to `error_place` on the first failure.
               `N > 0` means retry up to `N` times, then route to `error_place`. Default is 5.
        binding_policy: How the engine resolves which input tokens bind this transition
               when checking enablement — see [`BindingPolicy`][cpnx.BindingPolicy].
               `None` (default) inherits the net-wide default set on
               [`PetriNet`][cpnx.PetriNet] (itself `BindingPolicy.LEGACY` by default), so
               an unset transition behaves exactly as before. Set
               `BindingPolicy.FIRST` to enable deterministic-complete binding search
               (fixing head-of-line blocking) for this transition only.
        binding_priority_key: Sort key used only under `BindingPolicy.PRIORITY`. A pure
               `Callable[[list[Token]], object]` (must be callable or `None` — string
               expressions are not supported and raise `TypeError` at assignment) mapping a
               candidate binding (the flat list of tokens it would consume) to a comparable
               value; the binding with the **minimum** key is selected, ties broken by
               insertion order. `None` (default) means oldest-first — the minimum
               `Token.created_at` across the binding's **data** tokens (resource tokens are
               excluded, since a permit created at net construction is older than any data
               token and would otherwise tie every candidate and collapse selection to
               insertion order; a resource-only binding falls back to all tokens). The key must
               return values that are totally ordered *with each other*; a candidate whose key
               raises or is incomparable with the running best is skipped. If **every**
               candidate is skipped, the first satisfying binding is used *and* `on_error` fires
               (once per enabling pass, off the lock) so a wholly-broken key is not silent.

               **Performance / lock discipline:** unlike a callable `guard` (which runs on
               the expression thread pool under `expr_timeout_secs`), the key is invoked
               **inline while holding the engine lock, with no timeout**, once per candidate
               — up to `binding_search_limit` times per resolution. It is purity-verified
               (no I/O) but *not* time-bounded, so it must be trivially cheap; an expensive
               key stalls every concurrent `deposit`/`step`/probe on the net.
    """

    name: str
    inputs: list[InputArc]
    outputs: list[OutputArc]
    action: Callable[[list[Token]], list[Token]]
    guard: Callable[[list[Token]], bool] | None = None
    priority: int = 10
    action_timeout_secs: float | None = None
    max_retries: int | None = 5
    binding_policy: BindingPolicy | None = None
    binding_priority_key: Callable[[list[Token]], object] | None = None

    def __setattr__(self, name, value):
        # Keep the inline-safe flag in sync with ``guard``, including post-construction
        # reassignment. Actions are explicitly allowed side effects (DB writes, API calls,
        # I/O), so guards and the PRIORITY sort key — both evaluated under the engine lock —
        # are purity-verified; actions are not.
        if name == "guard":
            _reject_string_expression("guard", value)
            if callable(value):
                verify_callable_purity(value)
                _reject_non_bool_return("guard", value)
            super().__setattr__("_inline_safe", _inline_safe_for(value))
        elif name == "binding_priority_key" and value is not None:
            # String-expression keys are deferred (see ADR 0001), so reject non-callables
            # loudly rather than letting a truthy string be used as a key and raise on every
            # candidate at run time (which would silently livelock the engine).
            if not callable(value):
                raise TypeError(
                    "binding_priority_key must be a callable or None; "
                    f"got {type(value).__name__}. String-expression keys are not supported."
                )
            verify_callable_purity(value)
        super().__setattr__(name, value)
```

## cpnx.SubstitutionTransition

Bases: `Transition`

A Transition that encapsulates an entire sub-PetriNet (Hierarchical CPN).

Unlike a plain `Transition`, whose `action` is an arbitrary callable, a `SubstitutionTransition` delegates firing to a nested PetriNet (the `subnet`): named port places inside the subnet are bound to named socket places in the parent net via `port_socket_map`, and `subnet_deadline_secs` bounds how long the subnet is given to reach quiescence. The child subnet is fully insulated from the parent net — it carries no reference to its parent. Communication occurs strictly through the `port_socket_map`.

A subnet instance may only be wrapped by one `SubstitutionTransition` at a time; this is tracked process-wide via a weak set. Attempting to wrap the same subnet twice raises `ValueError`. Constructing with a non-dict `port_socket_map`, or with non-string port/socket names, raises `TypeError`. Referencing a port place that does not exist in the subnet raises `ValueError`.

Attributes:

| Name                   | Type             | Description                                                                                                                                                              |
| ---------------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `subnet`               | `PetriNet`       | The nested PetriNet this transition wraps and fires as a unit.                                                                                                           |
| `port_socket_map`      | `dict[str, str]` | Mapping of port place names (in subnet) to socket place names (in the parent net). Every key must name a place that already exists in subnet. Defaults to an empty dict. |
| `subnet_deadline_secs` | `float`          | Maximum wall-clock seconds allowed for the subnet to reach quiescence when this transition fires. Defaults to 30.0.                                                      |

Source code in `src/cpnx/transitions.py`

```
@dataclass
class SubstitutionTransition(Transition):
    """A [`Transition`][cpnx.Transition] that encapsulates an entire sub-PetriNet (Hierarchical CPN).

    Unlike a plain `Transition`, whose `action` is an arbitrary callable, a
    `SubstitutionTransition` delegates firing to a nested [`PetriNet`][cpnx.PetriNet] (the
    `subnet`): named port places inside the subnet are bound to named socket places in the
    parent net via `port_socket_map`, and `subnet_deadline_secs` bounds how long the subnet
    is given to reach quiescence. The child subnet is fully insulated from the parent net —
    it carries no reference to its parent. Communication occurs strictly through the
    `port_socket_map`.

    A subnet instance may only be wrapped by one `SubstitutionTransition` at a
    time; this is tracked process-wide via a weak set. Attempting to wrap the same subnet
    twice raises `ValueError`. Constructing with a non-dict `port_socket_map`, or with
    non-string port/socket names, raises `TypeError`. Referencing a port place that does not
    exist in the subnet raises `ValueError`.

    Attributes:
        subnet: The nested `PetriNet` this transition wraps and fires as a unit.
        port_socket_map: Mapping of port place names (in `subnet`) to socket place names
                         (in the parent net). Every key must name a place that already
                         exists in `subnet`. Defaults to an empty dict.
        subnet_deadline_secs: Maximum wall-clock seconds allowed for the subnet to reach
                         quiescence when this transition fires. Defaults to `30.0`.
    """

    _mapped_subnets: ClassVar[weakref.WeakSet] = weakref.WeakSet()

    subnet: "PetriNet" = field(default=None)  # type: ignore[assignment]
    port_socket_map: dict[str, str] = field(default_factory=dict)
    subnet_deadline_secs: float = 30.0

    def __post_init__(self):
        # Guard purity/inline-safety is handled by Transition.__setattr__ during field
        # assignment; here we only validate the substitution-specific structure.
        # Validate that the ports and sockets are structurally valid mapping names
        if not isinstance(self.port_socket_map, dict):
            raise TypeError("port_socket_map must be a dictionary.")
        # Ensure context isolation: child subnet cannot refer directly to parent places
        # that are not formally mapped.
        for port, socket in self.port_socket_map.items():
            if not isinstance(port, str) or not isinstance(socket, str):
                raise TypeError("Port and socket mapping names must be strings.")

        missing = [p for p in self.port_socket_map if p not in self.subnet.places]
        if missing:
            raise ValueError(
                f"SubstitutionTransition '{self.name}': subnet has no places for ports {missing}. "
                "Pre-declare port places in the subnet before wrapping it."
            )

        if self.subnet in SubstitutionTransition._mapped_subnets:
            raise ValueError(
                f"SubstitutionTransition '{self.name}': child subnet is already mapped to another transition."
            )
        SubstitutionTransition._mapped_subnets.add(self.subnet)
```

## cpnx.InputArc

Describe how a transition consumes tokens from one input place.

In CPN formalism an input arc carries an *arc expression* — a function that, given the current tokens in the place, determines which tokens are consumed and in what order. cpnx splits that inscription into its two honest halves, both **per token**:

- `filter` — *eligibility*: which tokens this arc may consume at all;
- `key` — *order*: a total order over the eligible tokens, min-first.

The engine applies `filter`, orders what survives by `key` (ascending, ties broken by insertion order), and consumes the first `count`. With neither set the arc is plain FIFO. Per-token parameters are what make the selection *indexable*, which an opaque `list[Token] -> list[Token]` transform can never be — see `docs/adr/0004-arc-selection-key-filter.md`.

Attributes:

| Name          | Type                          | Description                                                                                             |
| ------------- | ----------------------------- | ------------------------------------------------------------------------------------------------------- |
| `place`       | `str`                         | Name of the source place.                                                                               |
| `count`       | `int`                         | Number of tokens to consume. Ignored when consume_all=True. Defaults to 1.                              |
| `consume_all` | `bool`                        | Drain the entire place atomically. Defaults to False. Overrides key and filter — see the warning below. |
| `settle_secs` | `float`                       | Wait for no new arrivals for this many seconds before consuming. Defaults to 0.0 (no wait).             |
| `key`         | \`Callable\[[Token], object\] | None\`                                                                                                  |
| `filter`      | \`Callable\[[Token], bool\]   | None\`                                                                                                  |

Warning

**`consume_all=True` ignores both `key` and `filter`.** A draining arc takes every available token, in FIFO order, whatever the selection callables say — a token the `filter` rejects *is still consumed*. This preserves the pre-`key`/`filter` behavior of the arc inscription it replaced, but it is a genuine footgun: `filter` reads as a declaration of eligibility, and under `consume_all` it is not one. Do not combine them expecting "drain everything eligible"; that pattern is not supported today. To drain only eligible tokens, use a large `count` rather than `consume_all`.

Because that combination is always a mistake rather than a style choice, constructing (or reassigning into) such an arc emits a `UserWarning` naming the ignored parameters. Silence it with `warnings.filterwarnings("ignore", message="consume_all=True ignores")` if you genuinely mean "drain everything, selection notwithstanding".

Note

Both callables are purity-verified at assignment, and each carries its own inline-safe flag: a **certified** callable (see \[`cpnx.certification`\]) runs inline under the engine lock, an uncertified one on the timeout-bounded expression pool.

Uncertified `key`/`filter` are allowed, but on a **deep place they are the one thing worth certifying.** Both are evaluated *per token* over the whole available pool, so an uncertified one costs a thread round-trip per token per enabling check — where a guard costs one per *candidate binding*, bounded by `binding_search_limit`. Nothing bounds the per-token count: worst-case lock-hold is `len(place) * expr_timeout_secs` for that arc. A certified callable skips the executor entirely and the concern disappears.

Separately, `expr_timeout_secs` bounds a `key`'s *extraction*, not the **comparisons** between the values it returns — the sort runs inline under the engine lock. Return plain comparables (numbers, strings, tuples thereof); a value with a slow or diverging `__lt__` can hold the lock past any timeout.

Example

```
InputArc("inbox", count=2, key=lambda t: t.payload["priority"], filter=lambda t: not t.payload["held"])
```

Source code in `src/cpnx/transitions.py`

````
@dataclass
class InputArc:
    """Describe how a transition consumes tokens from one input place.

    In CPN formalism an input arc carries an *arc expression* — a function that, given
    the current tokens in the place, determines which tokens are consumed and in what
    order. cpnx splits that inscription into its two honest halves, both **per token**:

    - `filter` — *eligibility*: which tokens this arc may consume at all;
    - `key` — *order*: a total order over the eligible tokens, min-first.

    The engine applies `filter`, orders what survives by `key` (ascending, ties broken by
    insertion order), and consumes the first `count`. With neither set the arc is plain
    FIFO. Per-token parameters are what make the selection *indexable*, which an opaque
    `list[Token] -> list[Token]` transform can never be — see
    `docs/adr/0004-arc-selection-key-filter.md`.

    Attributes:
        place: Name of the source place.
        count: Number of tokens to consume. Ignored when `consume_all=True`. Defaults to 1.
        consume_all: Drain the entire place atomically. Defaults to `False`. **Overrides
                     `key` and `filter`** — see the warning below.
        settle_secs: Wait for no new arrivals for this many seconds before
                     consuming. Defaults to `0.0` (no wait).
        key: Per-token sort key — a pure `Callable[[Token], object]` mapping one token to a
             comparable value. Eligible tokens are consumed in **ascending** key order
             (min-first, mirroring [`Transition.binding_priority_key`][cpnx.Transition]),
             ties broken by insertion order so the selection is deterministic. For
             descending order, negate the key. The key must return values totally ordered
             *with each other*; if it raises, or the ordering is undefined, the arc yields
             no tokens and the transition is not enabled. `None` (default) means FIFO.
        filter: Per-token eligibility predicate — a pure `Callable[[Token], bool]`. A token
                is a candidate for this arc only when the predicate returns `True`; the
                rest stay in the place. `None` (default) means every available token is
                eligible. If annotated, the return type is checked to be `bool` at
                construction (a non-`bool` annotation raises `TypeError`); unannotated
                callables (including every lambda) are accepted unchecked. A filter that
                raises makes the arc unsatisfiable rather than firing on a partial pool.

    Warning:
        **`consume_all=True` ignores both `key` and `filter`.** A draining arc takes every
        available token, in FIFO order, whatever the selection callables say — a token the
        `filter` rejects *is still consumed*. This preserves the pre-`key`/`filter`
        behavior of the arc inscription it replaced, but it is a genuine footgun: `filter`
        reads as a declaration of eligibility, and under `consume_all` it is not one. Do
        not combine them expecting "drain everything eligible"; that pattern is not
        supported today. To drain only eligible tokens, use a large `count` rather than
        `consume_all`.

        Because that combination is always a mistake rather than a style choice,
        constructing (or reassigning into) such an arc emits a `UserWarning` naming the
        ignored parameters. Silence it with
        `warnings.filterwarnings("ignore", message="consume_all=True ignores")` if you
        genuinely mean "drain everything, selection notwithstanding".

    Note:
        Both callables are purity-verified at assignment, and each carries its own
        inline-safe flag: a **certified** callable (see [`cpnx.certification`]) runs
        inline under the engine lock, an uncertified one on the timeout-bounded
        expression pool.

        Uncertified `key`/`filter` are allowed, but on a **deep place they are the one
        thing worth certifying.** Both are evaluated *per token* over the whole available
        pool, so an uncertified one costs a thread round-trip per token per enabling
        check — where a guard costs one per *candidate binding*, bounded by
        `binding_search_limit`. Nothing bounds the per-token count: worst-case lock-hold is
        `len(place) * expr_timeout_secs` for that arc. A certified callable skips the
        executor entirely and the concern disappears.

        Separately, `expr_timeout_secs` bounds a `key`'s *extraction*, not the
        **comparisons** between the values it returns — the sort runs inline under the
        engine lock. Return plain comparables (numbers, strings, tuples thereof); a value
        with a slow or diverging `__lt__` can hold the lock past any timeout.

    Example:
        ```python
        InputArc("inbox", count=2, key=lambda t: t.payload["priority"], filter=lambda t: not t.payload["held"])
        ```
    """

    place: str
    count: int = 1
    consume_all: bool = False
    settle_secs: float = 0.0
    key: Callable[[Token], object] | None = field(default=None, compare=False, kw_only=True)
    filter: Callable[[Token], bool] | None = field(default=None, compare=False, kw_only=True)

    def __post_init__(self):
        # Marks construction complete, so `__setattr__` can tell a field assignment made by
        # the generated ``__init__`` (all of which this hook already covers, once) from a
        # later reassignment by the caller (which must warn on its own).
        self._constructed = True
        self._warn_if_drain_ignores_selection(stacklevel=4)

    def __setattr__(self, name, value):
        # Keep each selection callable's inline-safe flag in sync, including
        # post-construction reassignment. Reject/verify before mutating so a bad
        # value leaves the arc in its previous valid state.
        if name in ("key", "filter"):
            _reject_string_expression(f"InputArc.{name}", value)
            if callable(value):
                verify_callable_purity(value)
                if name == "filter":
                    _reject_non_bool_return("InputArc.filter", value)
            super().__setattr__(f"_{name}_inline_safe", _inline_safe_for(value))
        super().__setattr__(name, value)
        # Re-check the drain/selection conflict after the value lands, so the warning
        # reflects the arc as it now is. Skipped during ``__init__`` — `__post_init__`
        # reports the finished arc once, rather than once per conflicting field.
        if name in ("consume_all", "key", "filter") and getattr(self, "_constructed", False):
            self._warn_if_drain_ignores_selection(stacklevel=3)

    def _warn_if_drain_ignores_selection(self, *, stacklevel: int) -> None:
        """Warn when this arc sets `key`/`filter` that `consume_all` will silently ignore.

        The combination is never meaningful — a draining arc consumes the whole available
        pool in FIFO order regardless — so it is far more likely to be a misreading of
        `filter` as "drain everything eligible" than a deliberate choice. Engine behavior
        is unaffected; this only makes the documented bypass audible at the call site.

        `stacklevel` is passed in because the two callers sit at different depths: the
        `__post_init__` path is reached through the generated `__init__`, the
        `__setattr__` path directly from user code. Both aim the warning at the caller's
        own line.
        """
        if not self.consume_all:
            return
        ignored = [name for name in ("key", "filter") if getattr(self, name, None) is not None]
        if not ignored:
            return
        named = " and ".join(f"`{name}`" for name in ignored)
        warnings.warn(
            f"InputArc(place={self.place!r}): consume_all=True ignores {named} — a draining "
            "arc consumes every available token in FIFO order, including tokens the filter "
            "rejects. To drain only eligible tokens, use a large `count` instead of "
            "`consume_all`; to drain unconditionally, drop the ignored parameter(s).",
            UserWarning,
            stacklevel=stacklevel,
        )
````

## cpnx.OutputArc

Describe how a transition deposits tokens into one output place.

In CPN formalism an output arc carries an *arc expression* — a function evaluated against the transition's output tokens that determines whether tokens flow along this arc. On the output side that inscription is, in practice, always a boolean *activation* predicate, so cpnx names it `condition` — a different mechanism from the input side's token selection (see InputArc and `docs/adr/0004-arc-selection-key-filter.md`).

Attributes:

| Name        | Type                                | Description                                 |
| ----------- | ----------------------------------- | ------------------------------------------- |
| `place`     | `str`                               | Name of the target place.                   |
| `count`     | `int`                               | Number of tokens to deposit. Defaults to 1. |
| `condition` | \`Callable\[\[list[Token]\], bool\] | None\`                                      |

Source code in `src/cpnx/transitions.py`

````
@dataclass
class OutputArc:
    """Describe how a transition deposits tokens into one output place.

    In CPN formalism an output arc carries an *arc expression* — a function evaluated
    against the transition's output tokens that determines whether tokens flow along this
    arc. On the output side that inscription is, in practice, always a boolean *activation*
    predicate, so cpnx names it `condition` — a different mechanism from the input side's
    token selection (see [`InputArc`][cpnx.InputArc] and
    `docs/adr/0004-arc-selection-key-filter.md`).

    Attributes:
        place: Name of the target place.
        count: Number of tokens to deposit. Defaults to 1.
        condition: Arc activation predicate. Receives the list of non-resource
                   output tokens returned by the action; the arc is *skipped*
                   (no tokens deposited) when it returns `False`. `None`
                   (default) means the arc always fires. A pure `Callable`;
                   certified callables run inline (see [`cpnx.certification`]).
                   A boolean predicate: if annotated, its return type is checked to
                   be `bool` at construction (a non-`bool` annotation raises
                   `TypeError`); unannotated callables are accepted unchecked.
    """

    place: str
    count: int = 1
    condition: Callable[[list[Token]], bool] | None = field(default=None, compare=False)

    def __setattr__(self, name, value):
        # Keep the inline-safe flag in sync with ``condition``, including
        # post-construction reassignment. Reject/verify before mutating so a bad
        # value leaves the arc in its previous valid state.
        if name == "condition":
            _reject_string_expression("OutputArc.condition", value)
            if callable(value):
                verify_callable_purity(value)
                _reject_non_bool_return("OutputArc.condition", value)
            super().__setattr__("_inline_safe", _inline_safe_for(value))
        super().__setattr__(name, value)

    @classmethod
    def on_color(cls, color: str, place: str, count: int = 1) -> "OutputArc":
        """Build an [`OutputArc`][cpnx.OutputArc] that only fires for a matching first token color.

        The returned arc's `condition` is a callable that checks whether the action's
        output tokens are non-empty and the first token's color equals `color`. It closes
        over `color` (an immutable string), so it certifies for inline evaluation.

        Args:
            color: The `Token` color to match against the first output token.
            place: Name of the target place.
            count: Number of tokens to deposit when the arc fires. Defaults to 1.

        Returns:
            A new `OutputArc` bound to `place` whose condition evaluates to `True` only
            when the first token in the action's output has color `color`.

        Example:
            ```python
            OutputArc.on_color("success", place="done", count=1)
            ```
        """
        return cls(place=place, count=count, condition=lambda tokens: bool(tokens and tokens[0].color == color))
````

### on_color

```
on_color(
    color: str, place: str, count: int = 1
) -> OutputArc
```

Build an OutputArc that only fires for a matching first token color.

The returned arc's `condition` is a callable that checks whether the action's output tokens are non-empty and the first token's color equals `color`. It closes over `color` (an immutable string), so it certifies for inline evaluation.

Parameters:

| Name    | Type  | Description                                                    | Default    |
| ------- | ----- | -------------------------------------------------------------- | ---------- |
| `color` | `str` | The Token color to match against the first output token.       | *required* |
| `place` | `str` | Name of the target place.                                      | *required* |
| `count` | `int` | Number of tokens to deposit when the arc fires. Defaults to 1. | `1`        |

Returns:

| Type        | Description                                                           |
| ----------- | --------------------------------------------------------------------- |
| `OutputArc` | A new OutputArc bound to place whose condition evaluates to True only |
| `OutputArc` | when the first token in the action's output has color color.          |

Example

```
OutputArc.on_color("success", place="done", count=1)
```

Source code in `src/cpnx/transitions.py`

````
@classmethod
def on_color(cls, color: str, place: str, count: int = 1) -> "OutputArc":
    """Build an [`OutputArc`][cpnx.OutputArc] that only fires for a matching first token color.

    The returned arc's `condition` is a callable that checks whether the action's
    output tokens are non-empty and the first token's color equals `color`. It closes
    over `color` (an immutable string), so it certifies for inline evaluation.

    Args:
        color: The `Token` color to match against the first output token.
        place: Name of the target place.
        count: Number of tokens to deposit when the arc fires. Defaults to 1.

    Returns:
        A new `OutputArc` bound to `place` whose condition evaluates to `True` only
        when the first token in the action's output has color `color`.

    Example:
        ```python
        OutputArc.on_color("success", place="done", count=1)
        ```
    """
    return cls(place=place, count=count, condition=lambda tokens: bool(tokens and tokens[0].color == color))
````

`BindingPolicy` selects how a transition resolves which input tokens bind it — the legacy leading-token check, or a deterministic-complete binding search.

## cpnx.BindingPolicy

Bases: `Enum`

Strategy for choosing which input tokens bind a transition when it is enabled.

In CPN theory a transition is enabled if *any* assignment of place tokens (a *binding*) satisfies its guard. cpnx historically tested only the head of each input place (FIFO, or reordered by InputArc.key), which causes **head-of-line blocking**: a place holding `[A, B]` whose guard wants `B` reports the transition disabled, because only `A` is ever tested. `BindingPolicy` selects how the engine resolves that binding. See the design record in `docs/adr/0001-combinatorial-binding-search.md`.

Attributes:

| Name       | Type | Description                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ---------- | ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LEGACY`   |      | Test only the first count tokens of each input place (FIFO, or the leading tokens of the InputArc.key ordering) and evaluate the guard once against that single candidate set. This is the historical behavior and the default — reproducible, but subject to head-of-line blocking. Choose this to preserve pre-0.3.1 semantics exactly.                                                                                                         |
| `FIRST`    |      | Search input-token combinations in a stable insertion order and select the first combination whose guard is satisfied. Complete (finds a valid binding if one exists anywhere in the place, fixing head-of-line blocking) and deterministic (the same marking always yields the same binding). When the transition has no guard, this is identical to LEGACY and incurs no search cost.                                                           |
| `RANDOM`   |      | Enumerate the satisfying combinations and select one uniformly at random. Reproducible when the owning PetriNet is constructed with a seed (and max_workers=1); otherwise it varies run to run. Unlike FIRST, a guard-free RANDOM transition still selects among all eligible token groups (not just the head), so it must enumerate — there is no guard-free fast path. Intended for simulation, fairness testing, and CPN-flavored exploration. |
| `PRIORITY` |      | Enumerate the satisfying combinations and select the one minimizing Transition.binding_priority_key (default: oldest-first, i.e. the minimum Token.created_at across the binding). Ties fall to insertion order, so the choice is deterministic. Like RANDOM, it enumerates even without a guard.                                                                                                                                                 |

Note

The search enumerates the Cartesian product of each input arc's `count`-sized token combinations, varying the **last** arc in `Transition.inputs` fastest. Consequences for tuning:

- **Resource arcs inflate the space.** A `ResourcePlace`/`PacedResourcePlace` permit arc contributes `C(capacity, count)` interchangeable options that usually give the guard the same answer, so they can consume `binding_search_limit` on redundant permutations. List resource arcs **before** data arcs in `Transition.inputs` so the data dimension (the one that actually changes the guard result) varies first, and/or raise `binding_search_limit`.
- For `FIRST` the first binding yielded is exactly `LEGACY`'s head selection, so `FIRST` is a strict superset of `LEGACY`.
- `RANDOM`/`PRIORITY` must scan the whole (bounded) candidate set, so they do not short-circuit and are typically costlier than `FIRST`. If the candidate space exceeds `binding_search_limit`, they select over the first `limit` candidates only (a truncated prefix), firing `on_binding_search_exhausted`. If that prefix contains no satisfying binding, the transition is treated as disabled for that check — it can stall exactly like `FIRST`, not just fire over a smaller set.

Source code in `src/cpnx/transitions.py`

```
class BindingPolicy(enum.Enum):
    """Strategy for choosing which input tokens bind a transition when it is enabled.

    In CPN theory a transition is enabled if *any* assignment of place tokens (a
    *binding*) satisfies its guard. cpnx historically tested only the head of each
    input place (FIFO, or reordered by [`InputArc.key`][cpnx.InputArc]), which
    causes **head-of-line blocking**: a place holding `[A, B]` whose guard wants `B`
    reports the transition disabled, because only `A` is ever tested. `BindingPolicy`
    selects how the engine resolves that binding. See the design record in
    `docs/adr/0001-combinatorial-binding-search.md`.

    Attributes:
        LEGACY: Test only the first `count` tokens of each input place (FIFO, or the
            leading tokens of the [`InputArc.key`][cpnx.InputArc] ordering) and
            evaluate the guard once against that single candidate set. This is the
            historical behavior and the default — reproducible, but subject to
            head-of-line blocking. Choose this to preserve pre-0.3.1 semantics exactly.
        FIRST: Search input-token combinations in a stable insertion order and select
            the **first** combination whose guard is satisfied. Complete (finds a valid
            binding if one exists anywhere in the place, fixing head-of-line blocking)
            **and** deterministic (the same marking always yields the same binding). When
            the transition has no guard, this is identical to `LEGACY` and incurs no
            search cost.
        RANDOM: Enumerate the satisfying combinations and select one **uniformly at
            random**. Reproducible when the owning [`PetriNet`][cpnx.PetriNet] is
            constructed with a `seed` (and `max_workers=1`); otherwise it varies run to
            run. Unlike `FIRST`, a guard-free `RANDOM` transition still selects among *all*
            eligible token groups (not just the head), so it must enumerate — there is no
            guard-free fast path. Intended for simulation, fairness testing, and
            CPN-flavored exploration.
        PRIORITY: Enumerate the satisfying combinations and select the one **minimizing**
            `Transition.binding_priority_key` (default: oldest-first, i.e. the minimum
            `Token.created_at` across the binding). Ties fall to insertion order, so the
            choice is deterministic. Like `RANDOM`, it enumerates even without a guard.

    Note:
        The search enumerates the Cartesian product of each input arc's `count`-sized token
        combinations, varying the **last** arc in `Transition.inputs` fastest. Consequences
        for tuning:

        - **Resource arcs inflate the space.** A `ResourcePlace`/`PacedResourcePlace` permit
          arc contributes `C(capacity, count)` interchangeable options that usually give the
          guard the same answer, so they can consume `binding_search_limit` on redundant
          permutations. List resource arcs **before** data arcs in `Transition.inputs` so the
          data dimension (the one that actually changes the guard result) varies first, and/or
          raise `binding_search_limit`.
        - For `FIRST` the first binding yielded is exactly `LEGACY`'s head selection, so
          `FIRST` is a strict superset of `LEGACY`.
        - `RANDOM`/`PRIORITY` must scan the whole (bounded) candidate set, so they do not
          short-circuit and are typically costlier than `FIRST`. If the candidate space
          exceeds `binding_search_limit`, they select over the first `limit` candidates only
          (a truncated prefix), firing `on_binding_search_exhausted`. If that prefix contains
          no satisfying binding, the transition is treated as disabled for that check — it can
          stall exactly like `FIRST`, not just fire over a smaller set.
    """

    LEGACY = "legacy"
    FIRST = "first"
    RANDOM = "random"
    PRIORITY = "priority"
```
