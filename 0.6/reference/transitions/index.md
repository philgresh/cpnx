# Transitions

Transitions fire work over the thread pool; arcs connect them to places.

## cpnx.Transition

A CPN transition that fires when all its input places are enabled.

In CPN formalism a transition has:

- **Input arcs** whose `key`/`filter` determine which tokens are consumed
- **Output arcs** whose `condition` determines whether tokens are produced
- A **guard** — a boolean predicate over the binding that must hold for the transition to be enabled (evaluated before the action runs)

Attributes:

| Name                    | Type                                   | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| ----------------------- | -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `name`                  | `str`                                  | Unique identifier within a PetriNet.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `inputs`                | `list[InputArc]`                       | Input arcs (place to transition).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `outputs`               | `list[OutputArc]`                      | Output arcs (transition to place).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `action`                | `Callable[[list[Token]], list[Token]]` | Callable consuming input tokens and returning output tokens. Runs on the thread pool outside the engine lock.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `guard`                 | \`Callable\[\[list[Token]\], bool\]    | None\`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `priority`              | `int`                                  | Lower value fires first when multiple transitions are enabled. Defaults to 10.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `action_timeout_secs`   | \`float                                | None\`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `max_retries`           | \`int                                  | None\`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `binding_policy`        | \`BindingPolicy                        | None\`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `binding_priority_key`  | \`Callable\[\[list[Token]\], object\]  | None\`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `impacts_colors`        | \`frozenset[str]                       | None\`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `auto_return_resources` | `bool`                                 | When True (default), the net synthesizes a return arc at validate for any resource-pool InputArc this transition borrows from without a matching OutputArc, so the permit's return is expressed as a real arc (an explicit self-loop) rather than the invisible implicit leftover-return. This is behaviour-preserving — resource permits are already always returned on success — but makes the flow legible to to_dot, trace_impact, and structural invariants. Declaring your own OutputArc(pool) already suppresses synthesis for that pool; set this False to suppress it entirely and keep the raw implicit return. |

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
               every lambda) are accepted unchecked. **Prefer a closed-world (certified)
               guard**: an uncertified one pays a thread round-trip per *candidate binding*
               on every enabling check (bounded by `binding_search_limit`, so less severe
               than an uncertified arc `key`/`filter`, which is per *token* and unbounded —
               see [`InputArc`][cpnx.InputArc]). Certify it the same way: close over
               immutable values read at construction, never mutable module or instance
               state.
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
               [`PetriNet`][cpnx.PetriNet] (itself `BindingPolicy.FIRST`), which
               resolves head-only for a guard-free transition and searches only when a guard
               is present. Set `BindingPolicy.RANDOM` or `BindingPolicy.PRIORITY` to select
               *among* the satisfying bindings rather than taking the first, for this
               transition only. Setting the deprecated `BindingPolicy.LEGACY` warns
               ([`CpnxDeprecationWarning`][cpnx.CpnxDeprecationWarning]) and stops working in
               0.7.0.
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
        impacts_colors: Optional **declarative** annotation naming the token colour
               domain(s) this transition mutates or side-effects — e.g.
               `impacts_colors={"order", "refund"}`. Purely a hint for
               [`trace_impact`][cpnx.trace_impact] / [`PetriNet.risk_report`][cpnx.PetriNet.risk_report];
               it is **never evaluated at run time** and does not affect firing. In the
               spirit of high-level Petri net arc inscriptions (ISO/IEC 15909-1:2019),
               which route tokens by colour, it lets the blast-radius tracer prune
               downstream places whose `color_set` cannot carry a declared colour
               (cone-of-influence slicing). Accepts any iterable of colour-name strings
               (coerced to a `frozenset`); a **bare string raises `TypeError`** (pass
               `{"order"}`, not `"order"`). `None` (default) means *undeclared* — the
               tracer then treats the transition as impacting **every** colour (the
               conservative over-approximation; an empty set, by contrast, is a
               *narrowing* declaration). Cheap to add incrementally: it changes no
               existing behaviour.
        auto_return_resources: When `True` (default), the net synthesizes a return
               arc at [`validate`][cpnx.PetriNet.validate] for any resource-pool
               `InputArc` this transition borrows from without a matching
               `OutputArc`, so the permit's return is expressed as a real arc (an
               explicit self-loop) rather than the invisible implicit
               leftover-return. This is behaviour-preserving — resource permits are
               already always returned on success — but makes the flow legible to
               [`to_dot`][cpnx.PetriNet.to_dot], [`trace_impact`][cpnx.trace_impact],
               and structural invariants. Declaring your own `OutputArc(pool)`
               already suppresses synthesis for that pool; set this `False` to
               suppress it entirely and keep the raw implicit return.
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
    impacts_colors: frozenset[str] | None = None
    auto_return_resources: bool = True

    def __setattr__(self, name, value):
        # Keep the inline-safe flag in sync with ``guard``, including post-construction
        # reassignment. Actions are explicitly allowed side effects (DB writes, API calls,
        # I/O), so guards and the PRIORITY sort key — both evaluated under the engine lock —
        # are purity-verified; actions are not.
        if name == "guard":
            _reject_string_expression("guard", value)
            if callable(value):
                verify_callable_purity(value)
                lint_and_warn(value, "guard")
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
            lint_and_warn(value, "binding_priority_key")
        elif name == "binding_policy" and value is not None:
            # Warns only for a deprecated policy, and from both paths into this method: the
            # generated dataclass ``__init__`` as well as post-construction reassignment.
            # ``name`` is declared before ``binding_policy`` so it is already assigned, but
            # read it defensively so a field reorder degrades the message rather than raising.
            owner = getattr(self, "name", "<unnamed>")
            _warn_deprecated_policy(value, source=f"Transition(name={owner!r})")
        elif name == "impacts_colors":
            # Declarative colour-domain annotation (not a callable): normalise any
            # iterable of colour names to a frozenset, reject a bare string / non-str
            # member. Consumed by the blast-radius tracer, never evaluated at run time.
            value = coerce_color_domain(value, field="Transition.impacts_colors")
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

Warning

**On a deep place, make your `key`/`filter` closed-world (certified) — or pay a quadratic drain.** A *certified* selection callable is served from a persistent `(key, seq)` index maintained on the place, so the drain stays roughly linear in place depth. An *uncertified* one cannot be indexed — keying happens on the `deposit()` path, which cannot host an unbounded callable — so every enabling check re-reads and re-sorts the whole available pool **and** dispatches the callable through the timeout-bounded executor once per token. Both costs are per-token-per-firing, so draining a deep place is **O(N²)**: worst-case lock-hold for the arc is `len(place) * expr_timeout_secs`, and `benchmarks/bench_station_costs.py` measures an uncertified `key` at over **100×** a certified key computing the *identical* order at depth 2000 — a gap that widens with depth. A `filter` is worse to get wrong than a `key`: a single uncertified `filter` disqualifies the whole arc from indexing *even when the `key` is certified*, because applying an uncertified predicate after a capped index read could silently under-select.

**To certify, close over immutable values — never read mutable state at call time.** A callable certifies (see \[`cpnx.certification`\]) only when it draws on a fixed vocabulary and closes over nothing mutable. In practice: read configuration at construction and capture the *value*. A callable that reads a module global, an instance attribute, or any `list`/`dict`/`set` on each call does **not** certify — that is exactly the pattern that lands on the O(N²) path. Captured values must be immutable leaves (numbers, strings, `bytes`, `bool`, `None`) or `tuple`/`frozenset` thereof — a `frozenset` certifies, a `set` does not::

```
cutoff = config["vip_cutoff"]                        # an int, read once at construction
key = lambda t: (t.payload["spend"] < cutoff, t.created_at)   # closes over an int -> certifies
```

If the ordering *genuinely* depends on state that changes at run time — a live priority a human retunes mid-run — it cannot certify by construction, and the quadratic cost is **intrinsic, not a bug**: an index needs a key it can compute once, at deposit, and a value that changes afterward would sort already-deposited tokens wrong. There is no engine fix for that case. Restructure so the key is closed-world (snapshot the state into an immutable at construction and rebuild the arc when it changes), or accept the O(N²) on a shallow place only.

Note

Both callables are purity-verified at assignment, and each carries its own inline-safe flag: a **certified** callable runs inline under the engine lock, an uncertified one on the timeout-bounded expression pool (the source of the cost in the warning above).

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
             **Make it closed-world (certified) or a deep place drains in O(N²)** — see the
             performance warning below.
        filter: Per-token eligibility predicate — a pure `Callable[[Token], bool]`. A token
                is a candidate for this arc only when the predicate returns `True`; the
                rest stay in the place. `None` (default) means every available token is
                eligible. If annotated, the return type is checked to be `bool` at
                construction (a non-`bool` annotation raises `TypeError`); unannotated
                callables (including every lambda) are accepted unchecked. A filter that
                raises makes the arc unsatisfiable rather than firing on a partial pool.
                **Same performance warning as `key`** — an uncertified filter is quadratic
                on a deep place, and (unlike `key`) a certified `key` cannot rescue it.

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

    Warning:
        **On a deep place, make your `key`/`filter` closed-world (certified) — or pay a
        quadratic drain.** A *certified* selection callable is served from a persistent
        `(key, seq)` index maintained on the place, so the drain stays roughly linear in
        place depth. An *uncertified* one cannot be indexed — keying happens on the
        `deposit()` path, which cannot host an unbounded callable — so every enabling
        check re-reads and re-sorts the whole available pool **and** dispatches the
        callable through the timeout-bounded executor once per token. Both costs are
        per-token-per-firing, so draining a deep place is **O(N²)**: worst-case lock-hold
        for the arc is `len(place) * expr_timeout_secs`, and `benchmarks/bench_station_costs.py`
        measures an uncertified `key` at over **100×** a certified key computing the
        *identical* order at depth 2000 — a gap that widens with depth. A `filter` is
        worse to get wrong than a `key`: a single uncertified `filter` disqualifies the
        whole arc from indexing *even when the `key` is certified*, because applying an
        uncertified predicate after a capped index read could silently under-select.

        **To certify, close over immutable values — never read mutable state at call
        time.** A callable certifies (see [`cpnx.certification`]) only when it draws on a
        fixed vocabulary and closes over nothing mutable. In practice: read configuration
        at construction and capture the *value*. A callable that reads a module global, an
        instance attribute, or any `list`/`dict`/`set` on each call does **not** certify —
        that is exactly the pattern that lands on the O(N²) path. Captured values must be
        immutable leaves (numbers, strings, `bytes`, `bool`, `None`) or `tuple`/`frozenset`
        thereof — a `frozenset` certifies, a `set` does not::

            cutoff = config["vip_cutoff"]                        # an int, read once at construction
            key = lambda t: (t.payload["spend"] < cutoff, t.created_at)   # closes over an int -> certifies

        If the ordering *genuinely* depends on state that changes at run time — a live
        priority a human retunes mid-run — it cannot certify by construction, and the
        quadratic cost is **intrinsic, not a bug**: an index needs a key it can compute
        once, at deposit, and a value that changes afterward would sort already-deposited
        tokens wrong. There is no engine fix for that case. Restructure so the key is
        closed-world (snapshot the state into an immutable at construction and rebuild the
        arc when it changes), or accept the O(N²) on a shallow place only.

    Note:
        Both callables are purity-verified at assignment, and each carries its own
        inline-safe flag: a **certified** callable runs inline under the engine lock, an
        uncertified one on the timeout-bounded expression pool (the source of the cost in
        the warning above).

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
                # Best-effort advisory scan for side-effecting / non-deterministic
                # trouble spots (see :mod:`cpnx.linting`); warns, or raises in strict mode.
                lint_and_warn(value, f"InputArc.{name}")
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

| Name          | Type                                | Description                                                                                                                                                                                                                                                                                                        |
| ------------- | ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `place`       | `str`                               | Name of the target place.                                                                                                                                                                                                                                                                                          |
| `count`       | `int`                               | Number of tokens to deposit. Defaults to 1.                                                                                                                                                                                                                                                                        |
| `condition`   | \`Callable\[\[list[Token]\], bool\] | None\`                                                                                                                                                                                                                                                                                                             |
| `synthesized` | `bool`                              | True if the engine auto-added this arc rather than the author declaring it. The net synthesizes a return arc for a resource-pool InputArc that has no matching OutputArc, so the permit's return is expressed structurally (see PetriNet.validate). Excluded from equality; author-declared arcs default to False. |

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
        synthesized: `True` if the engine auto-added this arc rather than the
                     author declaring it. The net synthesizes a return arc for a
                     resource-pool `InputArc` that has no matching `OutputArc`, so
                     the permit's return is expressed structurally (see
                     `PetriNet.validate`). Excluded from equality; author-declared
                     arcs default to `False`.
    """

    place: str
    count: int = 1
    condition: Callable[[list[Token]], bool] | None = field(default=None, compare=False)
    synthesized: bool = field(default=False, compare=False)

    def __setattr__(self, name, value):
        # Keep the inline-safe flag in sync with ``condition``, including
        # post-construction reassignment. Reject/verify before mutating so a bad
        # value leaves the arc in its previous valid state.
        if name == "condition":
            _reject_string_expression("OutputArc.condition", value)
            if callable(value):
                verify_callable_purity(value)
                lint_and_warn(value, "OutputArc.condition")
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

`BindingPolicy` selects how a transition resolves which input tokens bind it — the deterministic-complete binding search (`FIRST`, the default), a selection *among* the satisfying bindings (`RANDOM`, `PRIORITY`), or the deprecated head-only check (`LEGACY`, which warns and is removed in 0.7.0).

## cpnx.CpnxDeprecationWarning

Bases: `UserWarning`

Warn that a deprecated cpnx API was used, naming the release that removes it.

A dedicated category so users can `warnings.filterwarnings` it independently — silence it during a staged migration, or escalate it to an error for CI via `filterwarnings("error", category=CpnxDeprecationWarning)` — without touching cpnx's other warnings.

Note

This deliberately subclasses `UserWarning`, **not** `DeprecationWarning`, so it is **visible by default**. Python's default filters ignore `DeprecationWarning` outside `__main__`, which would hide it from exactly the library users a deprecation is meant to reach. cpnx has been bitten by a silent deprecation once already: `LEGACY` was announced as deprecated in the 0.5.0 docs with no runtime signal whatsoever, so a net relying on it had nothing to notice short of reading the changelog.

Source code in `src/cpnx/transitions.py`

```
class CpnxDeprecationWarning(UserWarning):
    """Warn that a deprecated cpnx API was used, naming the release that removes it.

    A dedicated category so users can `warnings.filterwarnings` it independently —
    silence it during a staged migration, or escalate it to an error for CI via
    `filterwarnings("error", category=CpnxDeprecationWarning)` — without touching cpnx's
    other warnings.

    Note:
        This deliberately subclasses `UserWarning`, **not** `DeprecationWarning`, so it is
        **visible by default**. Python's default filters ignore `DeprecationWarning` outside
        `__main__`, which would hide it from exactly the library users a deprecation is meant
        to reach. cpnx has been bitten by a silent deprecation once already: `LEGACY` was
        announced as deprecated in the 0.5.0 docs with no runtime signal whatsoever, so a net
        relying on it had nothing to notice short of reading the changelog.
    """
```

## cpnx.BindingPolicy

Bases: `Enum`

Strategy for choosing which input tokens bind a transition when it is enabled.

In CPN theory a transition is enabled if *any* assignment of place tokens (a *binding*) satisfies its guard. cpnx before 0.3.1 tested only the head of each input place (FIFO, or reordered by InputArc.key), which causes **head-of-line blocking**: a place holding `[A, B]` whose guard wants `B` reports the transition disabled, because only `A` is ever tested. `BindingPolicy` selects how the engine resolves that binding. See the design record in `docs/adr/0001-combinatorial-binding-search.md`.

Attributes:

| Name       | Type | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| ---------- | ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `FIRST`    |      | Search input-token combinations in a stable insertion order and select the first combination whose guard is satisfied. Complete (finds a valid binding if one exists anywhere in the place, fixing head-of-line blocking) and deterministic (the same marking always yields the same binding). This is the default. When the transition has no guard, the first candidate is the head and trivially satisfies, so resolution reduces to the O(1) head selection and incurs no search cost.                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `LEGACY`   |      | Test only the first count tokens of each input place (FIFO, or the leading tokens of the InputArc.key ordering) and evaluate the guard once against that single candidate set. Reproducible, but subject to head-of-line blocking. Deprecated — selecting it warns at run time (CpnxDeprecationWarning) and it will be removed in 0.7.0. It was the default through 0.5.0; the default is now FIRST, with LEGACY kept selectable for one release as the opt-out, so a net that relied on head-only resolution has a cycle in which to migrate (ADR 0001 Phases 3–4). FIRST is a strict superset — identical head selection whenever the head already satisfies the guard, plus the head-of-line fix when it doesn't, and no search cost at all without a guard — so there is no remaining reason to choose LEGACY. Migrate by deleting the argument to inherit the FIRST default, or passing binding_policy=BindingPolicy.FIRST explicitly. |
| `RANDOM`   |      | Enumerate the satisfying combinations and select one uniformly at random. Reproducible when the owning PetriNet is constructed with a seed (and max_workers=1); otherwise it varies run to run. Unlike FIRST, a guard-free RANDOM transition still selects among all eligible token groups (not just the head), so it must enumerate — there is no guard-free fast path. Intended for simulation, fairness testing, and CPN-flavored exploration.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `PRIORITY` |      | Enumerate the satisfying combinations and select the one minimizing Transition.binding_priority_key (default: oldest-first, i.e. the minimum Token.created_at across the binding). Ties fall to insertion order, so the choice is deterministic. Like RANDOM, it enumerates even without a guard.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |

Note

The search enumerates the Cartesian product of each input arc's `count`-sized token combinations, varying the **last** arc in `Transition.inputs` fastest. Consequences for tuning:

- **Resource arcs inflate the space.** A `ResourcePlace`/`PacedResourcePlace` permit arc contributes `C(capacity, count)` interchangeable options that usually give the guard the same answer, so they can consume `binding_search_limit` on redundant permutations. List resource arcs **before** data arcs in `Transition.inputs` so the data dimension (the one that actually changes the guard result) varies first, and/or raise `binding_search_limit`.
- For `FIRST` the first binding yielded is exactly `LEGACY`'s head selection, so `FIRST` is a strict superset of `LEGACY` and a guard the head already satisfies costs no search at all.
- `RANDOM`/`PRIORITY` must scan the whole (bounded) candidate set, so they do not short-circuit and are typically costlier than `FIRST`. If the candidate space exceeds `binding_search_limit`, they select over the first `limit` candidates only (a truncated prefix), firing `on_binding_search_exhausted`. If that prefix contains no satisfying binding, the transition is treated as disabled for that check — it can stall exactly like `FIRST`, not just fire over a smaller set.

Source code in `src/cpnx/transitions.py`

```
class BindingPolicy(enum.Enum):
    """Strategy for choosing which input tokens bind a transition when it is enabled.

    In CPN theory a transition is enabled if *any* assignment of place tokens (a
    *binding*) satisfies its guard. cpnx before 0.3.1 tested only the head of each
    input place (FIFO, or reordered by [`InputArc.key`][cpnx.InputArc]), which
    causes **head-of-line blocking**: a place holding `[A, B]` whose guard wants `B`
    reports the transition disabled, because only `A` is ever tested. `BindingPolicy`
    selects how the engine resolves that binding. See the design record in
    `docs/adr/0001-combinatorial-binding-search.md`.

    Attributes:
        FIRST: Search input-token combinations in a stable insertion order and select
            the **first** combination whose guard is satisfied. Complete (finds a valid
            binding if one exists anywhere in the place, fixing head-of-line blocking)
            **and** deterministic (the same marking always yields the same binding).
            This is the default. When the transition has no guard, the first
            candidate is the head and trivially satisfies, so resolution reduces to the
            O(1) head selection and incurs no search cost.
        LEGACY: Test only the first `count` tokens of each input place (FIFO, or the
            leading tokens of the [`InputArc.key`][cpnx.InputArc] ordering) and evaluate
            the guard once against that single candidate set. Reproducible, but subject to
            head-of-line blocking.

            **Deprecated — selecting it warns at run time
            ([`CpnxDeprecationWarning`][cpnx.CpnxDeprecationWarning]) and it will be
            removed in 0.7.0.** It was the default through 0.5.0; the default is now
            `FIRST`, with `LEGACY` kept selectable for one release as the opt-out, so a net
            that relied on head-only resolution has a cycle in which to migrate (ADR 0001
            Phases 3–4). `FIRST` is a strict superset — identical head selection whenever
            the head already satisfies the guard, plus the head-of-line fix when it doesn't,
            and no search cost at all without a guard — so there is no remaining reason to
            choose `LEGACY`. Migrate by deleting the argument to inherit the `FIRST` default,
            or passing `binding_policy=BindingPolicy.FIRST` explicitly.
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
          `FIRST` is a strict superset of `LEGACY` and a guard the head already satisfies
          costs no search at all.
        - `RANDOM`/`PRIORITY` must scan the whole (bounded) candidate set, so they do not
          short-circuit and are typically costlier than `FIRST`. If the candidate space
          exceeds `binding_search_limit`, they select over the first `limit` candidates only
          (a truncated prefix), firing `on_binding_search_exhausted`. If that prefix contains
          no satisfying binding, the transition is treated as disabled for that check — it can
          stall exactly like `FIRST`, not just fire over a smaller set.
    """

    FIRST = "first"
    LEGACY = "legacy"
    RANDOM = "random"
    PRIORITY = "priority"
```

## Side-effect linting

Guards, arc `key`/`filter` selectors, and output-arc conditions should be deterministic and side-effect-free — they are evaluated to decide *enabling*, so network I/O, database reads, or clock/randomness use in them make the net's behaviour non-reproducible and undermine static analysability (see [ADR 0007](https://github.com/philgresh/cpnx/blob/main/docs/adr/0007-best-effort-side-effect-linting.md)). `cpnx` runs a best-effort AST linter at construction time that **warns** (CpnxLintWarning) when it detects these trouble spots. Set `CPNX_LINT_STRICT=1` or call set_strict to promote warnings to errors for CI gating, or call lint_callable directly to inspect a callable programmatically.

## cpnx.lint_callable

```
lint_callable(func: Callable) -> list[LintFinding]
```

Scan *func*'s body for network / database / clock / randomness trouble spots.

Pure inspection: never warns, never raises, never mutates. Returns findings in source order, de-duplicated by `(category, symbol, lineno)`. Returns `[]` when *func* is not callable or its source cannot be recovered (a compiled builtin, a REPL lambda) — an unseeable body is reported as "nothing found", never as a false positive, consistent with the best-effort contract.

Source code in `src/cpnx/linting.py`

```
def lint_callable(func: Callable) -> list[LintFinding]:
    """Scan *func*'s body for network / database / clock / randomness trouble spots.

    Pure inspection: never warns, never raises, never mutates. Returns findings in
    source order, de-duplicated by ``(category, symbol, lineno)``. Returns ``[]``
    when *func* is not callable or its source cannot be recovered (a compiled
    builtin, a REPL lambda) — an unseeable body is reported as "nothing found",
    never as a false positive, consistent with the best-effort contract.
    """
    if not callable(func):
        return []
    node = _get_cached_ast_node(func)
    if node is None:
        return []

    findings: list[LintFinding] = []
    seen: set[tuple[str, str, int]] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        hit = _classify_call(child, func)
        if hit is None:
            continue
        category, symbol = hit
        lineno = getattr(child, "lineno", 0)
        key = (category, symbol, lineno)
        if key in seen:
            continue
        seen.add(key)
        findings.append(
            LintFinding(
                category=category,
                symbol=symbol,
                lineno=lineno,
                message=f"'{symbol}' looks like {category} ({_WHY[category]})",
            )
        )
    return findings
```

## cpnx.LintFinding

One flagged trouble spot in a callable's body.

`category` is one of :data:`NETWORK`/:data:`DATABASE`/:data:`NONDETERMINISM`; `symbol` is the offending call as written (e.g. `"requests.get"`, `"datetime.now"`); `lineno` is the source line; `message` is the actionable, human-readable explanation.

Source code in `src/cpnx/linting.py`

```
@dataclass(frozen=True)
class LintFinding:
    """One flagged trouble spot in a callable's body.

    ``category`` is one of :data:`NETWORK`/:data:`DATABASE`/:data:`NONDETERMINISM`;
    ``symbol`` is the offending call as written (e.g. ``"requests.get"``,
    ``"datetime.now"``); ``lineno`` is the source line; ``message`` is the
    actionable, human-readable explanation.
    """

    category: str
    symbol: str
    lineno: int
    message: str
```

## cpnx.CpnxLintWarning

Bases: `UserWarning`

Advisory warning that a guard/key/filter contains a decidability trouble spot.

A dedicated category so users can `warnings.filterwarnings` it independently — silence it, or escalate it to an error via `filterwarnings("error", ...)` for CI — without touching cpnx's other warnings.

Source code in `src/cpnx/linting.py`

```
class CpnxLintWarning(UserWarning):
    """Advisory warning that a guard/key/filter contains a decidability trouble spot.

    A dedicated category so users can ``warnings.filterwarnings`` it independently —
    silence it, or escalate it to an error via ``filterwarnings("error", ...)`` for
    CI — without touching cpnx's other warnings.
    """
```

## cpnx.set_strict

```
set_strict(strict: bool) -> None
```

Promote lint findings from warnings to :class:`SideEffectLintError` (or back).

Off by default (the honest severity for a best-effort heuristic — see the module docstring). Opt in per process for CI gating, or set `CPNX_LINT_STRICT=1`.

Source code in `src/cpnx/linting.py`

```
def set_strict(strict: bool) -> None:
    """Promote lint findings from warnings to :class:`SideEffectLintError` (or back).

    Off by default (the honest severity for a best-effort heuristic — see the module
    docstring). Opt in per process for CI gating, or set ``CPNX_LINT_STRICT=1``.
    """
    global _STRICT
    _STRICT = strict
```
