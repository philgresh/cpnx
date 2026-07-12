# Transitions

Transitions fire work over the thread pool; arcs connect them to places.

## cpnx.Transition

A CPN transition that fires when all its input places are enabled.

In CPN formalism a transition has:

- **Input arcs** with arc expressions determining which tokens are consumed
- **Output arcs** with arc expressions determining which tokens are produced
- A **guard** — a boolean predicate over the binding that must hold for the transition to be enabled (evaluated before the action runs)

Attributes:

| Name                   | Type                                   | Description                                                                                                   |
| ---------------------- | -------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `name`                 | `str`                                  | Unique identifier within a PetriNet.                                                                          |
| `inputs`               | `list[InputArc]`                       | Input arcs (place to transition).                                                                             |
| `outputs`              | `list[OutputArc]`                      | Output arcs (transition to place).                                                                            |
| `action`               | `Callable[[list[Token]], list[Token]]` | Callable consuming input tokens and returning output tokens. Runs on the thread pool outside the engine lock. |
| `guard`                | \`Callable\[\[list[Token]\], bool\]    | str                                                                                                           |
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

    - **Input arcs** with arc expressions determining which tokens are consumed
    - **Output arcs** with arc expressions determining which tokens are produced
    - A **guard** — a boolean predicate over the binding that must hold for the
      transition to be enabled (evaluated before the action runs)

    Attributes:
        name: Unique identifier within a [`PetriNet`][cpnx.PetriNet].
        inputs: Input arcs (place to transition).
        outputs: Output arcs (transition to place).
        action: Callable consuming input tokens and returning output tokens.
                Runs on the thread pool outside the engine lock.
        guard: CPN transition guard — `Callable[[list[Token]], bool]` or expression string.
               Evaluated while holding the engine lock; return `False` to block firing.
               `None` (default) means always enabled.
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
    guard: Callable[[list[Token]], bool] | str | None = None
    priority: int = 10
    action_timeout_secs: float | None = None
    max_retries: int | None = 5
    binding_policy: BindingPolicy | None = None
    binding_priority_key: Callable[[list[Token]], object] | None = None

    def __setattr__(self, name, value):
        # Keep the pre-compiled guard in sync with ``guard``, including
        # post-construction reassignment. Actions are explicitly allowed side effects
        # (DB writes, API calls, I/O), so guards and the PRIORITY sort key — both evaluated
        # under the engine lock — are purity-verified; actions are not.
        if name == "guard":
            compiled = SandboxEvaluator.maybe_compile(value)
            if callable(value):
                verify_callable_purity(value)
            super().__setattr__("_compiled_guard", compiled)
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
        # Guard compilation/purity is handled by Transition.__setattr__ during field
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

In CPN formalism an input arc carries an *arc expression* — a function that, given the current tokens in the place, determines which tokens are consumed and in what order. `expression` is that function.

Attributes:

| Name          | Type                                       | Description                                                                                 |
| ------------- | ------------------------------------------ | ------------------------------------------------------------------------------------------- |
| `place`       | `str`                                      | Name of the source place.                                                                   |
| `count`       | `int`                                      | Number of tokens to consume. Ignored when consume_all=True. Defaults to 1.                  |
| `consume_all` | `bool`                                     | Drain the entire place atomically. Defaults to False.                                       |
| `settle_secs` | `float`                                    | Wait for no new arrivals for this many seconds before consuming. Defaults to 0.0 (no wait). |
| `expression`  | \`Callable\[\[list[Token]\], list[Token]\] | str                                                                                         |

Source code in `src/cpnx/transitions.py`

```
@dataclass
class InputArc:
    """Describe how a transition consumes tokens from one input place.

    In CPN formalism an input arc carries an *arc expression* — a function
    that, given the current tokens in the place, determines which tokens are
    consumed and in what order. `expression` is that function.

    Attributes:
        place: Name of the source place.
        count: Number of tokens to consume. Ignored when `consume_all=True`. Defaults to 1.
        consume_all: Drain the entire place atomically. Defaults to `False`.
        settle_secs: Wait for no new arrivals for this many seconds before
                     consuming. Defaults to `0.0` (no wait).
        expression: CPN input arc expression. Receives all tokens currently
                    in the place; returns them in desired consumption order.
                    Can be a pure Callable or a sandboxed expression string.
                    The engine consumes the first `count` tokens from the
                    result. `None` (default) means FIFO order.
    """

    place: str
    count: int = 1
    consume_all: bool = False
    settle_secs: float = 0.0
    expression: Callable[[list[Token]], list[Token]] | str | None = field(default=None, compare=False)

    def __setattr__(self, name, value):
        # Keep the pre-compiled code object in sync with ``expression``, including
        # post-construction reassignment. Compile/verify before mutating so a bad
        # value leaves the arc in its previous valid state.
        if name == "expression":
            compiled = SandboxEvaluator.maybe_compile(value)
            if callable(value):
                verify_callable_purity(value)
            super().__setattr__("_compiled_expression", compiled)
        super().__setattr__(name, value)
```

## cpnx.OutputArc

Describe how a transition deposits tokens into one output place.

In CPN formalism an output arc carries an *arc expression* — a function evaluated against the transition's output tokens that determines whether tokens flow along this arc. `expression` is that predicate.

Attributes:

| Name         | Type                                | Description                                 |
| ------------ | ----------------------------------- | ------------------------------------------- |
| `place`      | `str`                               | Name of the target place.                   |
| `count`      | `int`                               | Number of tokens to deposit. Defaults to 1. |
| `expression` | \`Callable\[\[list[Token]\], bool\] | str                                         |

Source code in `src/cpnx/transitions.py`

````
@dataclass
class OutputArc:
    """Describe how a transition deposits tokens into one output place.

    In CPN formalism an output arc carries an *arc expression* — a function
    evaluated against the transition's output tokens that determines whether
    tokens flow along this arc. `expression` is that predicate.

    Attributes:
        place: Name of the target place.
        count: Number of tokens to deposit. Defaults to 1.
        expression: CPN output arc expression. Receives the list of non-resource
                    output tokens returned by the action; the arc is *skipped*
                    (no tokens deposited) when it returns `False`. `None`
                    (default) means the arc always fires.
                    Can be a pure Callable or a sandboxed expression string.
    """

    place: str
    count: int = 1
    expression: Callable[[list[Token]], bool] | str | None = field(default=None, compare=False)

    def __setattr__(self, name, value):
        # Keep the pre-compiled code object in sync with ``expression``, including
        # post-construction reassignment. Compile/verify before mutating so a bad
        # value leaves the arc in its previous valid state.
        if name == "expression":
            compiled = SandboxEvaluator.maybe_compile(value)
            if callable(value):
                verify_callable_purity(value)
            super().__setattr__("_compiled_expression", compiled)
        super().__setattr__(name, value)

    @classmethod
    def on_color(cls, color: str, place: str, count: int = 1) -> "OutputArc":
        """Build an [`OutputArc`][cpnx.OutputArc] that only fires for a matching first token color.

        The returned arc's `expression` is a sandboxed expression string that checks whether
        the action's output tokens are non-empty and the first token's color equals `color`.

        Args:
            color: The `Token` color to match against the first output token.
            place: Name of the target place.
            count: Number of tokens to deposit when the arc fires. Defaults to 1.

        Returns:
            A new `OutputArc` bound to `place` whose expression evaluates to `True` only
            when the first token in the action's output has color `color`.

        Example:
            ```python
            OutputArc.on_color("success", place="done", count=1)
            ```
        """
        expr_str = f"bool(tokens and tokens[0].color == {color!r})"
        return cls(place=place, count=count, expression=expr_str)
````

### on_color

```
on_color(
    color: str, place: str, count: int = 1
) -> OutputArc
```

Build an OutputArc that only fires for a matching first token color.

The returned arc's `expression` is a sandboxed expression string that checks whether the action's output tokens are non-empty and the first token's color equals `color`.

Parameters:

| Name    | Type  | Description                                                    | Default    |
| ------- | ----- | -------------------------------------------------------------- | ---------- |
| `color` | `str` | The Token color to match against the first output token.       | *required* |
| `place` | `str` | Name of the target place.                                      | *required* |
| `count` | `int` | Number of tokens to deposit when the arc fires. Defaults to 1. | `1`        |

Returns:

| Type        | Description                                                            |
| ----------- | ---------------------------------------------------------------------- |
| `OutputArc` | A new OutputArc bound to place whose expression evaluates to True only |
| `OutputArc` | when the first token in the action's output has color color.           |

Example

```
OutputArc.on_color("success", place="done", count=1)
```

Source code in `src/cpnx/transitions.py`

````
@classmethod
def on_color(cls, color: str, place: str, count: int = 1) -> "OutputArc":
    """Build an [`OutputArc`][cpnx.OutputArc] that only fires for a matching first token color.

    The returned arc's `expression` is a sandboxed expression string that checks whether
    the action's output tokens are non-empty and the first token's color equals `color`.

    Args:
        color: The `Token` color to match against the first output token.
        place: Name of the target place.
        count: Number of tokens to deposit when the arc fires. Defaults to 1.

    Returns:
        A new `OutputArc` bound to `place` whose expression evaluates to `True` only
        when the first token in the action's output has color `color`.

    Example:
        ```python
        OutputArc.on_color("success", place="done", count=1)
        ```
    """
    expr_str = f"bool(tokens and tokens[0].color == {color!r})"
    return cls(place=place, count=count, expression=expr_str)
````

`BindingPolicy` selects how a transition resolves which input tokens bind it — the legacy leading-token check, or a deterministic-complete binding search.

## cpnx.BindingPolicy

Bases: `Enum`

Strategy for choosing which input tokens bind a transition when it is enabled.

In CPN theory a transition is enabled if *any* assignment of place tokens (a *binding*) satisfies its guard. cpnx historically tested only the head of each input place (FIFO, or reordered by InputArc.expression), which causes **head-of-line blocking**: a place holding `[A, B]` whose guard wants `B` reports the transition disabled, because only `A` is ever tested. `BindingPolicy` selects how the engine resolves that binding. See the design record in `docs/adr/0001-combinatorial-binding-search.md`.

Attributes:

| Name       | Type | Description                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ---------- | ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LEGACY`   |      | Test only the first count tokens of each input place (FIFO, or the leading tokens of the InputArc.expression ordering) and evaluate the guard once against that single candidate set. This is the historical behavior and the default — reproducible, but subject to head-of-line blocking. Choose this to preserve pre-0.3.1 semantics exactly.                                                                                                  |
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
    input place (FIFO, or reordered by [`InputArc.expression`][cpnx.InputArc]), which
    causes **head-of-line blocking**: a place holding `[A, B]` whose guard wants `B`
    reports the transition disabled, because only `A` is ever tested. `BindingPolicy`
    selects how the engine resolves that binding. See the design record in
    `docs/adr/0001-combinatorial-binding-search.md`.

    Attributes:
        LEGACY: Test only the first `count` tokens of each input place (FIFO, or the
            leading tokens of the [`InputArc.expression`][cpnx.InputArc] ordering) and
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
