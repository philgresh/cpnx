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
| `retry_policy`          | \`RetryPolicy                          | None\`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `binding_policy`        | \`BindingPolicy                        | None\`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `binding_priority_key`  | \`Callable\[\[list[Token]\], object\]  | None\`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `impacts_colors`        | \`frozenset[str]                       | None\`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `auto_return_resources` | `bool`                                 | When True (default), the net synthesizes a return arc at validate for any resource-pool InputArc this transition borrows from without a matching OutputArc, so the permit's return is expressed as a real arc (an explicit self-loop) rather than the invisible implicit leftover-return. This is behaviour-preserving — resource permits are already always returned on success — but makes the flow legible to to_dot, trace_impact, and structural invariants. Declaring your own OutputArc(pool) already suppresses synthesis for that pool; set this False to suppress it entirely and keep the raw implicit return. |
| `binding_selector`      | \`BindingSelector                      | None\`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |

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
               How *long* each of those retries waits is `retry_policy`.

               The budget is counted on `Token.attempts`, which travels **with the token**
               across transitions (`Token.evolve` carries it forward), so a token that
               already failed twice upstream arrives with two of its `N` retries spent.
               Have the action return a token with `attempts=0` once it has made progress
               if each stage should get a fresh budget.
        retry_policy: How long a rolled-back data token waits before each retry — capped
               exponential backoff with optional full jitter; see
               [`RetryPolicy`][cpnx.RetryPolicy]. Set it per transition because retry
               behaviour belongs to the **dependency** this transition talks to, not to the
               net: a throttled third-party API wants `RetryPolicy(delay=0.5, backoff=2.0,
               max_delay=30.0, jitter=True)` while a local write wants
               `RetryPolicy(delay=0.05)`. `None` (default) inherits the net-wide default
               ([`PetriNet.retry_policy`][cpnx.PetriNet], itself a flat 1-second delay), so
               an unset transition behaves exactly as before. A policy is an immutable
               value, so one instance can be shared by every transition that talks to the
               same dependency. Assigning anything but a `RetryPolicy` or `None` raises
               `TypeError`. Note the schedule is indexed by `Token.attempts`, which counts
               failures across the **whole net**, so an inherited count starts a token
               further along this transition's schedule — see
               [`RetryPolicy`][cpnx.RetryPolicy].
        binding_policy: How the engine resolves which input tokens bind this transition
               when checking enablement — see [`BindingPolicy`][cpnx.BindingPolicy].
               `None` (default) inherits the net-wide default set on
               [`PetriNet`][cpnx.PetriNet] (itself `BindingPolicy.FIRST`), which
               resolves head-only for a guard-free transition and searches only when a guard
               is present. Set `BindingPolicy.RANDOM` or `BindingPolicy.PRIORITY` to select
               *among* the satisfying bindings rather than taking the first, for this
               transition only.
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
        binding_selector: Let outside logic choose the tokens this transition binds, in place
               of the product search and `binding_policy`. A
               `Callable[[BindingView], choice | None]` that receives a read-only
               [`BindingView`][cpnx.BindingView] of the tokens each input arc could take now,
               and returns `None` (nothing to fire) or one choice per input arc, as a sequence
               in `inputs` order or a mapping from arc index: a `Token` for a `count=1` arc, or
               a sequence of exactly `count` tokens. The engine checks that every chosen token
               is available in its place, that none is chosen twice and that the `guard`
               holds, then fires exactly those tokens. A choice that fails a check, or a
               selector that raises, is reported through `on_error` and treated as "not
               enabled". Made for dispatchers whose policy (shortest queue, skill match,
               random among eligible) is the thing a simulation studies.

               **Contract.** The selector runs under the engine lock, possibly several times
               per firing (each enablement check asks it), so it must be cheap and must not
               call back into the net (doing so raises `RuntimeError`, which is reported like
               any raising selector). Unlike an uncertified guard, and like
               `binding_priority_key`, it runs inline with no `expr_timeout_secs` limit, and so
               do the keys it passes to `view.groups`: one that blocks holds the engine lock.
               It must be a pure function of the view:
               draw randomness only from `view.random`, which is seeded from the net's `seed`,
               the transition and its firing count, so the same seed and selector give the same
               firings across processes. Use [`BindingView.groups`][cpnx.BindingView.groups]
               with a certified key for indexed lookups that stay O(keys) as places deepen.
               Arcs that read one place each see all of that place's tokens and must choose
               disjoint ones; the selector is asked only when the place holds enough for all of
               them. Returning `None` means "not enabled", including for quiescence: a
               selector that waits for a model time by testing `view.model_time` reads as
               quiescent, so `run()` exits and `drive_to_quiescence` stops. Express such a wait
               as a token's `available_at` or an arc's `settle_secs` instead.
               Input arcs of a selector transition may not set `key`, `filter`, `join_key` or
               `consume_all`, and `binding_priority_key` must be unset: assigning the selector,
               or `binding_priority_key` afterwards, raises `ValueError`. A conflict introduced
               later through the arcs (setting one of those on an arc, or reassigning `inputs`)
               cannot be caught at assignment, so it is reported through `on_error` and the
               transition reads as not enabled until it is resolved. See ADR 0013.
    """

    name: str
    inputs: list[InputArc]
    outputs: list[OutputArc]
    action: Callable[[list[Token]], list[Token]]
    guard: Callable[[list[Token]], bool] | None = None
    priority: int = 10
    action_timeout_secs: float | None = None
    max_retries: int | None = 5
    retry_policy: RetryPolicy | None = None
    binding_policy: BindingPolicy | None = None
    binding_priority_key: Callable[[list[Token]], object] | None = None
    impacts_colors: frozenset[str] | None = None
    auto_return_resources: bool = True
    binding_selector: "BindingSelector | None" = None

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
        elif name in ("binding_priority_key", "binding_selector"):
            _check_binding_chooser(self, name, value)
        elif name == "binding_policy":
            _reject_non_binding_policy(value, name="Transition.binding_policy", allow_none=True)
        elif name == "retry_policy":
            _reject_non_retry_policy(value)
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

Unlike a plain `Transition`, whose `action` is an arbitrary callable, a `SubstitutionTransition` delegates firing to a nested PetriNet (the `subnet`): named port places inside the subnet are bound to named socket places in the parent net via `port_socket_map`, and `subnet_deadline_secs` bounds how long the subnet is given to reach quiescence. Tokens move strictly through the `port_socket_map`. This is a sequential handoff, not an unfolding into one flat net: the parent deposits into the ports, the subnet runs to quiescence, and only then do its outputs come back, so subnet transitions never interleave with the parent's. Check the subnet's own liveness separately: the parent sees only whether each firing finishes. The subnet holds one weak reference to its parent net, which `add_transition` sets. It is used to stop the subnet's work when an enclosing firing fails, to tell the parent when a failed firing has settled, and to reject a subnet that would enclose its own parent or belong to two nets.

A firing is all-or-nothing as far as the subnet is concerned. Firings of one `SubstitutionTransition` run one at a time, so each collects only its own outputs. A firing selected while another is in progress waits for it on a worker thread. The firing fails if a port rejects a deposited token, the subnet raises, the subnet produces more outputs than the active output arcs take (dropping them would lose tokens), its pool ports do not hand back exactly the permit copies they were lent, the subnet has not finished by its bound (`subnet_deadline_secs` on the wall clock, the parent's drive_to_quiescence `max_ticks` on the logical clock, applied to each firing), or the parent cannot commit the outputs. The subnet has finished once it is quiescent, or once nothing can fire at the current time or any finite later time (a token parked at `available_at = inf` never becomes available). The parent's consumed tokens are then rolled back as for any transition, and every subnet place except SinkPlaces is restored to its marking from before the firing. The rollback reaches nested subnets: a `SubstitutionTransition` inside the subnet that committed during the failed firing has its own subnet restored too. A resource pool gets back only its permit count. The firings that borrowed permits return them, so each pool keeps any cooldown the failed firing started, the restore drops the parent's permit copy from a `ResourcePlace` mapped as a port, and a permit one transition moved into another pool, for a later transition to return, goes back to where it started. A firing that commits leaves the subnet as it reached it, so a subnet may keep state across firings.

A port and its socket are one compound place, so a pool socket needs a pool port and the reverse, and a resource pool mapped as a port must have `capacity=0`: the parent owns the permits, and the subnet holds one only while a firing has borrowed it. Every pool port is emptied after the firing, and the firing fails if the subnet keeps a borrowed permit or returns one it was never lent, because the parent returns its own permits either way. A pool the subnet owns stays out of `port_socket_map`, and is limited by its own capacity on top of the parent's.

Callbacks on the subnet (`on_transition_fired`, `on_token_deposited`) report its internal firings as they commit, and a later restore does not retract them: treat them as provisional until the enclosing firing commits.

The restore happens only once the subnet has settled. An action of the failed firing still running cannot be stopped: it finishes and commits through its own arcs inside the subnet, and only then is the subnet restored, so nothing it holds (a permit, a recipe card, a one-token machine) is ever put back while in use. Work the failed firing had queued, in the subnet or in any subnet below it, is not started: it hands its tokens back without retrying, dead-lettering or reporting an error. Until the subnet settles, the parent is not quiescent, and the next firing waits up to `subnet_deadline_secs` for it, then fails without depositing anything. That wait runs from the failure or from the firing's arrival, whichever is later, so firings queued together share one wait.

A subnet instance may only be wrapped by one `SubstitutionTransition` at a time; this is tracked process-wide via a weak set. Attempting to wrap the same subnet twice raises `ValueError`, and reassigning `subnet` raises `AttributeError`. Constructing with a non-dict `port_socket_map`, or with non-string port/socket names, raises `TypeError`. Referencing a port place that does not exist in the subnet raises `ValueError`. So does a port and socket that both declare a colour set, if the two differ, since they are one compound place: add_transition, add_place and validate check it. An undeclared colour set matches anything. A schema can still be stricter on one side; a token the port's schema rejects fails the firing.

Attributes:

| Name                   | Type             | Description                                                                                                                                                              |
| ---------------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `subnet`               | `PetriNet`       | The nested PetriNet this transition wraps and fires as a unit.                                                                                                           |
| `port_socket_map`      | `dict[str, str]` | Mapping of port place names (in subnet) to socket place names (in the parent net). Every key must name a place that already exists in subnet. Defaults to an empty dict. |
| `subnet_deadline_secs` | \`float          | None\`                                                                                                                                                                   |

Source code in `src/cpnx/transitions.py`

```
@dataclass
class SubstitutionTransition(Transition):
    """A [`Transition`][cpnx.Transition] that encapsulates an entire sub-PetriNet (Hierarchical CPN).

    Unlike a plain `Transition`, whose `action` is an arbitrary callable, a
    `SubstitutionTransition` delegates firing to a nested [`PetriNet`][cpnx.PetriNet] (the
    `subnet`): named port places inside the subnet are bound to named socket places in the
    parent net via `port_socket_map`, and `subnet_deadline_secs` bounds how long the subnet is
    given to reach quiescence. Tokens move strictly through the `port_socket_map`. This is a
    sequential handoff, not an unfolding into one flat net: the parent deposits into the ports,
    the subnet runs to quiescence, and only then do its outputs come back, so subnet transitions
    never interleave with the parent's. Check the subnet's own liveness separately: the parent
    sees only whether each firing finishes. The subnet holds one weak reference to its parent
    net, which `add_transition` sets. It is used to stop the subnet's work when an enclosing
    firing fails, to tell the parent when a failed firing has settled, and to reject a subnet
    that would enclose its own parent or belong to two nets.

    A firing is all-or-nothing as far as the subnet is concerned. Firings of one
    `SubstitutionTransition` run one at a time, so each collects only its own outputs. A firing
    selected while another is in progress waits for it on a worker thread. The
    firing fails if a port rejects a deposited token, the subnet raises, the subnet produces
    more outputs than the active output arcs take (dropping them would lose tokens), its pool
    ports do not hand back exactly the permit copies they were lent, the subnet has not
    finished by its bound (`subnet_deadline_secs` on the wall clock,
    the parent's [`drive_to_quiescence`][cpnx.PetriNet.drive_to_quiescence] `max_ticks` on the
    logical clock, applied to each firing), or the parent cannot commit the outputs. The subnet has finished once it is
    quiescent, or once nothing can fire at the current time or any finite later time (a
    token parked at `available_at = inf` never becomes available). The parent's consumed tokens are then
    rolled back as for any transition, and every subnet place except
    [`SinkPlace`][cpnx.SinkPlace]s is restored to its marking from before the firing. The
    rollback reaches nested subnets: a `SubstitutionTransition` inside the subnet that
    committed during the failed firing has its own subnet restored too.
    A resource pool gets back only its permit count. The firings that borrowed permits return
    them, so each pool keeps any cooldown the failed firing started, the restore drops the
    parent's permit copy from a `ResourcePlace` mapped as a port, and a permit one transition
    moved into another pool, for a later transition to return, goes back to where it started.
    A firing that commits leaves the subnet as it reached it, so a subnet may keep state across
    firings.

    A port and its socket are one compound place, so a pool socket needs a pool port and the
    reverse, and a resource pool mapped as a port must have `capacity=0`: the parent owns the
    permits, and the subnet holds one only while a firing has borrowed it. Every pool port is
    emptied after the firing, and the firing fails if the subnet keeps a borrowed permit or returns
    one it was never lent, because the parent returns its own permits either way. A pool the subnet
    owns stays out of `port_socket_map`, and is limited by its own capacity on top of the parent's.

    Callbacks on the subnet (`on_transition_fired`, `on_token_deposited`) report its internal
    firings as they commit, and a later restore does not retract them: treat them as provisional
    until the enclosing firing commits.

    The restore happens only once the subnet has settled. An action of the failed firing
    still running cannot be stopped: it finishes and commits through its own arcs inside the
    subnet, and only then is the subnet restored, so nothing it holds (a permit, a recipe
    card, a one-token machine) is ever put back while in use. Work the failed firing had
    queued, in the subnet or in any subnet below it, is not started: it hands its tokens back
    without retrying, dead-lettering or reporting an error. Until the subnet settles, the parent is not
    quiescent, and the next firing waits up to `subnet_deadline_secs` for it, then fails
    without depositing anything. That wait runs from the failure or from the firing's arrival,
    whichever is later, so firings queued together share one wait.

    A subnet instance may only be wrapped by one `SubstitutionTransition` at a
    time; this is tracked process-wide via a weak set. Attempting to wrap the same subnet
    twice raises `ValueError`, and reassigning `subnet` raises `AttributeError`. Constructing
    with a non-dict `port_socket_map`, or with non-string port/socket names, raises `TypeError`.
    Referencing a port place that does not exist in the subnet raises `ValueError`. So does
    a port and socket that both declare a colour set, if the two differ, since they are one
    compound place: [`add_transition`][cpnx.PetriNet.add_transition],
    [`add_place`][cpnx.PetriNet.add_place] and [`validate`][cpnx.PetriNet.validate] check it.
    An undeclared colour set matches anything. A schema can still be stricter on one side; a
    token the port's schema rejects fails the firing.

    Attributes:
        subnet: The nested `PetriNet` this transition wraps and fires as a unit.
        port_socket_map: Mapping of port place names (in `subnet`) to socket place names
                         (in the parent net). Every key must name a place that already
                         exists in `subnet`. Defaults to an empty dict.
        subnet_deadline_secs: Maximum wall-clock seconds allowed for the subnet to reach
                         quiescence when this transition fires. A subnet still short of
                         quiescence at the deadline fails the firing. A firing first waits up
                         to the same bound for the subnet to settle from an earlier failed
                         firing, on either clock; firings queued together share that wait.
                         It bounds the firing, not the parent's
                         `run()`, which waits for a failed firing's still-running action;
                         set `action_timeout_secs` on subnet actions that may hang. `None`
                         removes both bounds, so a hung subnet action then holds this
                         transition, and the parent's shutdown, indefinitely. Must be a
                         positive number or `None`, checked on assignment too, and is stored
                         as a float. Defaults to `30.0`.
    """

    _mapped_subnets: ClassVar[weakref.WeakSet] = weakref.WeakSet()

    subnet: "PetriNet" = field(default=None)  # type: ignore[assignment]
    port_socket_map: dict[str, str] = field(default_factory=dict)
    subnet_deadline_secs: float | None = 30.0
    _firing_lock: "threading.Lock" = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

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

    def __setattr__(self, name, value):
        if name == "subnet" and "subnet" in self.__dict__:
            raise AttributeError(
                f"SubstitutionTransition '{self.name}': subnet cannot be reassigned; wrap the new subnet in a "
                "new SubstitutionTransition."
            )
        if name == "subnet_deadline_secs":
            value = _check_subnet_deadline(self.name, value)
        super().__setattr__(name, value)
```

## cpnx.InputArc

Describe how a transition consumes tokens from one input place.

In CPN formalism an input arc carries an *arc expression* — a function that, given the current tokens in the place, determines which tokens are consumed and in what order. cpnx splits that inscription into its two honest halves, both **per token**:

- `filter` — *eligibility*: which tokens this arc may consume at all;
- `key` — *order*: a total order over the eligible tokens, min-first.

The engine applies `filter`, orders what survives by `key` (ascending, ties broken by insertion order), and consumes the first `count`. With neither set the arc is plain FIFO. Per-token parameters are what make the selection *indexable*, which an opaque `list[Token] -> list[Token]` transform can never be — see `docs/adr/0004-arc-selection-key-filter.md`.

Attributes:

| Name          | Type                          | Description                                                                                                                                                                                                                                                                                                                                                                                                                             |
| ------------- | ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `place`       | `str`                         | Name of the source place.                                                                                                                                                                                                                                                                                                                                                                                                               |
| `count`       | `int`                         | Number of tokens to consume. Ignored when consume_all=True. Defaults to 1. Two arcs on the same place take different tokens, so [InputArc("p"), InputArc("p")] needs two tokens in p, as InputArc("p", count=2) does.                                                                                                                                                                                                                   |
| `consume_all` | `bool`                        | Drain the entire place atomically. Defaults to False. Overrides key and filter — see the warning below. Beside another input arc on its place it takes every token that arc leaves, which may be none, whatever the arc order: [InputArc("batch", filter=is_flush), InputArc("batch", consume_all=True)] takes the marker and drains the rest. Two consume_all arcs on one place are never enabled, and adding such a transition warns. |
| `settle_secs` | `float`                       | Wait for no new arrivals for this many seconds before consuming. Defaults to 0.0 (no wait).                                                                                                                                                                                                                                                                                                                                             |
| `key`         | \`Callable\[[Token], object\] | None\`                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `filter`      | \`Callable\[[Token], bool\]   | None\`                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `join_key`    | \`Callable\[[Token], object\] | None\`                                                                                                                                                                                                                                                                                                                                                                                                                                  |

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

# A job rejoins the worker it was assigned to, matched on `wid`.
wid = lambda t: t.payload["wid"]
Transition(
    name="resume",
    inputs=[InputArc("rejoin", join_key=wid), InputArc("idle", join_key=wid)],
    outputs=[OutputArc("busy")],
)
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
               Two arcs on the same place take different tokens, so `[InputArc("p"),
               InputArc("p")]` needs two tokens in `p`, as `InputArc("p", count=2)` does.
        consume_all: Drain the entire place atomically. Defaults to `False`. **Overrides
                     `key` and `filter`** — see the warning below. Beside another input arc
                     on its place it takes every token that arc leaves, which may be none,
                     whatever the arc order: `[InputArc("batch", filter=is_flush),
                     InputArc("batch", consume_all=True)]` takes the marker and drains the
                     rest. Two `consume_all` arcs on one place are never enabled, and adding
                     such a transition warns.
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
        join_key: Per-token join key — a pure `Callable[[Token], object]` returning a hashable
                  value. Every input arc of a transition that sets one joins on it: a binding
                  is considered only when all of those arcs' tokens have equal join keys, and
                  the transition's guard (if any) is then evaluated as usual. A transition with
                  a `join_key` arc fires the same bindings as one whose guard tests the same
                  equality, under every [`BindingPolicy`][cpnx.BindingPolicy], but a
                  *certified* join key is served from a per-place index, so a join with no
                  match costs a few lookups instead of a walk over every candidate. Requires
                  `count == 1` and no `key`, `filter` or `consume_all` (`ValueError`
                  otherwise). A join key that raises or returns an unhashable value makes the
                  arc unsatisfiable. `None` (default) means the arc takes no part in a join.
                  Arcs that read **one place** and are meant to join on the same key (a
                  self-join) must share **one function object**: functions are told apart
                  by identity, so two equal lambdas count as two different keys. The
                  bindings are the same, but the place's index can no longer tell which
                  keys hold a token per arc, and a backlog of one-token keys makes each
                  probe linear in keys. Every extra distinct function on a place also
                  doubles the work of checking that each arc can get its own token.
                  **Make it closed-world (certified), or it can be far slower than the guard
                  it replaces:** an *uncertified* join key is never indexed, so every
                  resolution reads each join place's whole available pool (no
                  `binding_search_limit` cap) and dispatches the key through the
                  timeout-bounded executor once per token. A miss over 4 000 jobs then costs
                  about 90× the equivalent equality guard. The same holds on a place subclass
                  (`ResourcePlace`, `PacedResourcePlace`, `ThresholdPlace`), which is never
                  indexed either. See `docs/adr/0010-equi-join-index.md`.

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

        # A job rejoins the worker it was assigned to, matched on `wid`.
        wid = lambda t: t.payload["wid"]
        Transition(
            name="resume",
            inputs=[InputArc("rejoin", join_key=wid), InputArc("idle", join_key=wid)],
            outputs=[OutputArc("busy")],
        )
        ```
    """

    place: str
    count: int = 1
    consume_all: bool = False
    settle_secs: float = 0.0
    key: Callable[[Token], object] | None = field(default=None, compare=False, kw_only=True)
    filter: Callable[[Token], bool] | None = field(default=None, compare=False, kw_only=True)
    join_key: Callable[[Token], object] | None = field(default=None, compare=False, kw_only=True)

    def __post_init__(self):
        self._check_join_shape({})
        # Marks construction complete, so `__setattr__` can tell a field assignment made by
        # the generated ``__init__`` (all of which this hook already covers, once) from a
        # later reassignment by the caller (which must warn on its own).
        self._constructed = True
        self._warn_if_drain_ignores_selection(stacklevel=4)

    def __setattr__(self, name, value):
        if name in _JOIN_SHAPE_FIELDS and getattr(self, "_constructed", False):
            self._check_join_shape({name: value})
        # Keep each selection callable's inline-safe flag in sync, including
        # post-construction reassignment. Reject/verify before mutating so a bad
        # value leaves the arc in its previous valid state.
        if name in ("key", "filter", "join_key"):
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

    def _check_join_shape(self, overrides: dict[str, object]) -> None:
        """Raise `ValueError` if this arc, with *overrides* applied, sets `join_key` on an unsupported shape.

        A join arc contributes exactly one token, chosen by place order among the tokens whose
        join key matches. `count`, `consume_all`, `key` and `filter` would each change which
        token that is, and the join index does not model them yet.
        """

        def value(name: str) -> object:
            return overrides[name] if name in overrides else getattr(self, name, None)

        if value("join_key") is None:
            return
        conflicts = [name for name in ("key", "filter") if value(name) is not None]
        if value("consume_all"):
            conflicts.append("consume_all")
        if value("count") != 1:
            conflicts.append("count != 1")
        if conflicts:
            raise ValueError(
                f"InputArc(place={self.place!r}): join_key cannot be combined with {', '.join(conflicts)}. "
                "A join arc consumes one token in FIFO order among those whose join key matches; "
                "move any other condition into the transition's guard."
            )

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

`BindingPolicy` selects how a transition resolves which input tokens bind it — the deterministic-complete binding search (`FIRST`, the default), or a selection *among* the satisfying bindings (`RANDOM`, `PRIORITY`). The head-only `LEGACY` check was removed in 0.7.0.

## cpnx.CpnxDeprecationWarning

Bases: `UserWarning`

Warn that a deprecated cpnx API was used, naming the release that removes it.

A dedicated category so users can `warnings.filterwarnings` it independently — silence it during a staged migration, or escalate it to an error for CI via `filterwarnings("error", category=CpnxDeprecationWarning)` — without touching cpnx's other warnings.

Note

This deliberately subclasses `UserWarning`, **not** `DeprecationWarning`, so it is **visible by default**. Python's default filters ignore `DeprecationWarning` outside `__main__`, which would hide it from exactly the library users a deprecation is meant to reach. cpnx has been bitten by a silent deprecation once already: `BindingPolicy.LEGACY` was announced as deprecated in the 0.5.0 docs with no runtime signal whatsoever, so a net relying on it had nothing to notice short of reading the changelog. It gained this warning in 0.6.0 and was removed in 0.7.0.

Note

No cpnx API currently emits this. It is retained as the project's deprecation category — the contract above (its own filterable category, visible by default) is public API that the next deprecation inherits, and removing it would break the `filterwarnings` entries users were told to add.

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
        to reach. cpnx has been bitten by a silent deprecation once already:
        `BindingPolicy.LEGACY` was announced as deprecated in the 0.5.0 docs with no runtime
        signal whatsoever, so a net relying on it had nothing to notice short of reading the
        changelog. It gained this warning in 0.6.0 and was removed in 0.7.0.

    Note:
        No cpnx API currently emits this. It is retained as the project's deprecation
        category — the contract above (its own filterable category, visible by default) is
        public API that the next deprecation inherits, and removing it would break the
        `filterwarnings` entries users were told to add.
    """
```

## cpnx.BindingPolicy

Bases: `Enum`

Strategy for choosing which input tokens bind a transition when it is enabled.

In CPN theory a transition is enabled if *any* assignment of place tokens (a *binding*) satisfies its guard. cpnx before 0.3.1 tested only the head of each input place (FIFO, or reordered by InputArc.key), which causes **head-of-line blocking**: a place holding `[A, B]` whose guard wants `B` reports the transition disabled, because only `A` is ever tested. `BindingPolicy` selects how the engine resolves that binding. See the design record in `docs/adr/0001-combinatorial-binding-search.md`.

Attributes:

| Name       | Type | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| ---------- | ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `FIRST`    |      | Search input-token combinations in a stable insertion order and select the first combination whose guard is satisfied. Complete (finds a valid binding if one exists anywhere in the place, fixing head-of-line blocking) and deterministic (the same marking always yields the same binding). This is the default. When the transition has no guard, the first candidate is the head and trivially satisfies, so resolution reduces to the O(1) head selection and incurs no search cost. |
| `RANDOM`   |      | Enumerate the satisfying combinations and select one uniformly at random. Reproducible when the owning PetriNet is constructed with a seed (and max_workers=1); otherwise it varies run to run. Unlike FIRST, a guard-free RANDOM transition still selects among all eligible token groups (not just the head), so it must enumerate — there is no guard-free fast path. Intended for simulation, fairness testing, and CPN-flavored exploration.                                          |
| `PRIORITY` |      | Enumerate the satisfying combinations and select the one minimizing Transition.binding_priority_key (default: oldest-first, i.e. the minimum Token.created_at across the binding). Ties fall to insertion order, so the choice is deterministic. Like RANDOM, it enumerates even without a guard.                                                                                                                                                                                          |

Note

The search enumerates the Cartesian product of each input arc's `count`-sized token combinations, varying the **last** arc in `Transition.inputs` fastest. Consequences for tuning:

- **Resource arcs inflate the space.** A `ResourcePlace`/`PacedResourcePlace` permit arc contributes `C(capacity, count)` interchangeable options that usually give the guard the same answer, so they can consume `binding_search_limit` on redundant permutations. List resource arcs **before** data arcs in `Transition.inputs` so the data dimension (the one that actually changes the guard result) varies first, and/or raise `binding_search_limit`.
- For `FIRST` the first binding yielded is the head selection, so a guard the head already satisfies costs no search at all.
- `RANDOM`/`PRIORITY` must scan the whole (bounded) candidate set, so they do not short-circuit and are typically costlier than `FIRST`. If the candidate space exceeds `binding_search_limit`, they select over the first `limit` candidates only (a truncated prefix), firing `on_binding_search_exhausted`. If that prefix contains no satisfying binding, the transition is treated as disabled for that check — it can stall exactly like `FIRST`, not just fire over a smaller set.
- **That prefix is the first candidates in product order, so `RANDOM` is uniform over it, not over every satisfying binding.** With 2 000 waiting jobs, 100 servers and an always-true guard, a limit of 1 000 covers jobs 0–9 only, so `RANDOM` never picks a later job until those are gone. For "random among all eligible", set a binding_selector that draws over indexed groups instead.

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
        - For `FIRST` the first binding yielded is the head selection, so a guard the head
          already satisfies costs no search at all.
        - `RANDOM`/`PRIORITY` must scan the whole (bounded) candidate set, so they do not
          short-circuit and are typically costlier than `FIRST`. If the candidate space
          exceeds `binding_search_limit`, they select over the first `limit` candidates only
          (a truncated prefix), firing `on_binding_search_exhausted`. If that prefix contains
          no satisfying binding, the transition is treated as disabled for that check — it can
          stall exactly like `FIRST`, not just fire over a smaller set.
        - **That prefix is the first candidates in product order, so `RANDOM` is uniform over
          it, not over every satisfying binding.** With 2 000 waiting jobs, 100 servers and an
          always-true guard, a limit of 1 000 covers jobs 0–9 only, so `RANDOM` never picks a
          later job until those are gone. For "random among all eligible", set a
          [`binding_selector`][cpnx.Transition] that draws over indexed groups instead.
    """

    FIRST = "first"
    RANDOM = "random"
    PRIORITY = "priority"
```

## External binding selection

A binding_selector lets outside logic choose the tokens a transition binds, in place of the product search and `BindingPolicy`: a dispatcher's policy (shortest queue, skill match, random among eligible servers) is often what a simulation studies, and it rarely fits a single sort key. The selector receives a read-only `BindingView` of the tokens each input arc could take, draws any randomness from `view.random`, and returns one choice per arc; the engine checks it and fires exactly those tokens. See [ADR 0013](https://github.com/philgresh/cpnx/blob/main/docs/adr/0013-binding-selector.md).

## cpnx.BindingView

What a binding_selector sees: the tokens each input arc could take.

Read-only, and valid only for the duration of the selector call: it reads the places directly, under the engine lock, without copying them. Arcs that read one place see the same tokens through `available`, `has_available` and `groups`; choose disjoint ones for them.

Attributes:

| Name         | Type | Description                                                                         |
| ------------ | ---- | ----------------------------------------------------------------------------------- |
| `transition` |      | The name of the transition being resolved.                                          |
| `arcs`       |      | The transition's input arcs, in order. A selector's choice is indexed the same way. |
| `model_time` |      | The net's current time.                                                             |

Source code in `src/cpnx/selection.py`

```
class BindingView:
    """What a [`binding_selector`][cpnx.Transition] sees: the tokens each input arc could take.

    Read-only, and valid only for the duration of the selector call: it reads the places
    directly, under the engine lock, without copying them. Arcs that read one place see the same
    tokens through `available`, `has_available` and `groups`; choose disjoint ones for them.

    Attributes:
        transition: The name of the transition being resolved.
        arcs: The transition's input arcs, in order. A selector's choice is indexed the same way.
        model_time: The net's current time.
    """

    __slots__ = ("transition", "arcs", "model_time", "_places", "_t_limit", "_seed", "_random", "_groups")

    def __init__(
        self,
        transition: str,
        arcs: "Sequence[InputArc]",
        places: Sequence[Place],
        model_time: float,
        t_limit: float,
        seed: str,
        groups: Callable[[int, Callable[[Token], object], Iterable[object] | None], Mapping[object, object]],
    ) -> None:
        self.transition = transition
        self.arcs = tuple(arcs)
        self.model_time = model_time
        self._places = places
        self._t_limit = t_limit
        self._seed = seed
        self._random: random.Random | None = None
        self._groups = groups

    @property
    def random(self) -> random.Random:
        """A random stream for this choice, seeded from the net's seed, the transition and its firing count.

        The same seed gives the same stream for the same firing, however often the engine asks
        the selector between firings, so a selector that draws only from here stays
        reproducible. It never touches the net's own streams.
        """
        if self._random is None:
            self._random = random.Random(self._seed)
        return self._random

    def available(self, arc: int) -> Iterator[Token]:
        """Yield the tokens input arc *arc* could take now, in its place's read order (lazily)."""
        place = self._places[arc]
        size = 64
        seen = 0
        while True:
            chunk = place.peek(size, model_time=self._t_limit)
            yield from chunk[seen:]
            if len(chunk) < size:
                return
            seen = len(chunk)
            size *= 2

    def has_available(self, arc: int, count: int = 1) -> bool:
        """Whether input arc *arc*'s place holds at least *count* tokens available now."""
        return self._places[arc].can_retrieve(count, model_time=self._t_limit)

    def groups(self, arc: int, key: Callable[[Token], object]) -> Mapping[object, TokenGroup]:
        """Group the tokens input arc *arc* could take by `key(token)`; keys with none are left out.

        When *key* certifies (see [`cpnx.certification`]) and the same function object is passed
        again, the place keeps an index for it from then on, so later calls cost O(keys) however
        deep the place is. Otherwise the tokens are keyed on each call, with no time limit, under
        the engine lock. Pass the same function object each time (a module-level function, not a
        lambda or bound method built per call), so the index is built once and reused. Keys come
        back in the order their first available token is read, whether or not an index served
        the call, so iterating them is as reproducible as the place itself.
        """
        return {k: TokenGroup(g) for k, g in self._groups(arc, key, None).items()}

    def group(self, arc: int, key: Callable[[Token], object], value: object) -> TokenGroup:
        """The tokens input arc *arc* could take whose `key(token) == value`; O(1) when indexed."""
        found = self._groups(arc, key, (value,)).get(value)
        return _EMPTY_GROUP if found is None else TokenGroup(found)
```

### random

```
random: Random
```

A random stream for this choice, seeded from the net's seed, the transition and its firing count.

The same seed gives the same stream for the same firing, however often the engine asks the selector between firings, so a selector that draws only from here stays reproducible. It never touches the net's own streams.

### available

```
available(arc: int) -> Iterator[Token]
```

Yield the tokens input arc *arc* could take now, in its place's read order (lazily).

Source code in `src/cpnx/selection.py`

```
def available(self, arc: int) -> Iterator[Token]:
    """Yield the tokens input arc *arc* could take now, in its place's read order (lazily)."""
    place = self._places[arc]
    size = 64
    seen = 0
    while True:
        chunk = place.peek(size, model_time=self._t_limit)
        yield from chunk[seen:]
        if len(chunk) < size:
            return
        seen = len(chunk)
        size *= 2
```

### has_available

```
has_available(arc: int, count: int = 1) -> bool
```

Whether input arc *arc*'s place holds at least *count* tokens available now.

Source code in `src/cpnx/selection.py`

```
def has_available(self, arc: int, count: int = 1) -> bool:
    """Whether input arc *arc*'s place holds at least *count* tokens available now."""
    return self._places[arc].can_retrieve(count, model_time=self._t_limit)
```

### groups

```
groups(
    arc: int, key: Callable[[Token], object]
) -> Mapping[object, TokenGroup]
```

Group the tokens input arc *arc* could take by `key(token)`; keys with none are left out.

When *key* certifies (see \[`cpnx.certification`\]) and the same function object is passed again, the place keeps an index for it from then on, so later calls cost O(keys) however deep the place is. Otherwise the tokens are keyed on each call, with no time limit, under the engine lock. Pass the same function object each time (a module-level function, not a lambda or bound method built per call), so the index is built once and reused. Keys come back in the order their first available token is read, whether or not an index served the call, so iterating them is as reproducible as the place itself.

Source code in `src/cpnx/selection.py`

```
def groups(self, arc: int, key: Callable[[Token], object]) -> Mapping[object, TokenGroup]:
    """Group the tokens input arc *arc* could take by `key(token)`; keys with none are left out.

    When *key* certifies (see [`cpnx.certification`]) and the same function object is passed
    again, the place keeps an index for it from then on, so later calls cost O(keys) however
    deep the place is. Otherwise the tokens are keyed on each call, with no time limit, under
    the engine lock. Pass the same function object each time (a module-level function, not a
    lambda or bound method built per call), so the index is built once and reused. Keys come
    back in the order their first available token is read, whether or not an index served
    the call, so iterating them is as reproducible as the place itself.
    """
    return {k: TokenGroup(g) for k, g in self._groups(arc, key, None).items()}
```

### group

```
group(
    arc: int, key: Callable[[Token], object], value: object
) -> TokenGroup
```

The tokens input arc *arc* could take whose `key(token) == value`; O(1) when indexed.

Source code in `src/cpnx/selection.py`

```
def group(self, arc: int, key: Callable[[Token], object], value: object) -> TokenGroup:
    """The tokens input arc *arc* could take whose `key(token) == value`; O(1) when indexed."""
    found = self._groups(arc, key, (value,)).get(value)
    return _EMPTY_GROUP if found is None else TokenGroup(found)
```

## cpnx.TokenGroup

Bases: `Sequence[Token]`

The available tokens of one input arc that share a key, in the place's read order.

A sized, indexable view, not a copy: `len(group)` and `group[i]` cost O(1) when the group is served from a place index. Valid only inside the selector call that received it.

Source code in `src/cpnx/selection.py`

```
class TokenGroup(Sequence[Token]):
    """The available tokens of one input arc that share a key, in the place's read order.

    A sized, indexable view, not a copy: `len(group)` and `group[i]` cost O(1) when the group
    is served from a place index. Valid only inside the selector call that received it.
    """

    __slots__ = ("_source",)

    def __init__(self, source) -> None:
        self._source = source

    def __len__(self) -> int:
        return len(self._source)

    def __getitem__(self, position):  # type: ignore[override]
        if isinstance(position, slice):
            return [self[i] for i in range(*position.indices(len(self)))]
        size = len(self)
        if position < 0:
            position += size
        if not 0 <= position < size:
            raise IndexError("TokenGroup index out of range")
        source = self._source
        if hasattr(source, "token_at"):
            return source.token_at(position)
        return source[position][1]

    def __iter__(self) -> Iterator[Token]:
        for _entry, token in self._source:
            yield token
```

## cpnx.BindingSelector

```
BindingSelector = Callable[
    ["BindingView"],
    "Sequence[Sequence[Token] | Token] | Mapping[int, Sequence[Token] | Token] | None",
]
```

A transition's binding selector: a view in, one token choice per input arc (or `None`) out.

## cpnx.InvalidSelection

Bases: `ValueError`

A binding selector returned a choice the engine cannot fire.

The `__cause__` of the error `on_error` receives for a rejected choice, which tells a bad choice apart from a selector that raised.

Source code in `src/cpnx/selection.py`

```
class InvalidSelection(ValueError):
    """A binding selector returned a choice the engine cannot fire.

    The `__cause__` of the error `on_error` receives for a rejected choice, which tells a bad
    choice apart from a selector that raised.
    """
```

## Retry scheduling

`RetryPolicy` says how long a rolled-back data token waits before each retry: capped exponential backoff with optional full jitter, the shape recommended by the AWS Builders' Library's [Timeouts, retries, and backoff with jitter](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/). Attach one per transition (Transition.retry_policy) — retry behaviour belongs to the dependency a transition talks to, not to the net — and a transition that declares none inherits PetriNet.retry_policy. How *many* times it retries before dead-lettering stays Transition.max_retries.

## cpnx.RetryPolicy

How long a rolled-back data token waits before each retry.

The delay for a 0-based `attempt` — the token's `Token.attempts` count at the moment it failed, so its first failure is attempt 0 — is

```
delay = min(delay * backoff ** attempt, max_delay)
if jitter:
    delay = uniform(0, delay)
```

Attach one per transition (Transition.retry_policy) to pace each dependency on its own terms; a transition that declares none inherits the net's default (PetriNet.retry_policy). How *many* times a transition retries before its tokens are dead-lettered stays `Transition.max_retries`.

**The attempt index is per token, not per transition.** `Token.attempts` counts "failed firings this token has been rolled back from" anywhere in the net, and `Token.evolve` carries it forward, so a token that failed three times upstream arrives already at attempt 3: under `RetryPolicy(delay=0.5, backoff=2.0)` its *first* retry at the next transition waits 4 s, not 0.5 s. `Transition.max_retries` reads the same counter and so has the same property — that token also has three of its retries already spent. This is the documented meaning of `attempts` rather than an artefact of backoff, but backoff makes it far more visible, so: on a net where tokens cross several fallible transitions, either keep `backoff=1.0` (flat, no accumulation to notice), set a `max_delay` low enough to bound the inherited growth, or have the action return a token with `attempts=0` once it has made progress, which resets the budget for the stages ahead.

Immutable, comparable and cheap, so one policy can be shared by many transitions. Fields are validated on construction — a schedule that could only stall or hot-loop is rejected here, where it is written, rather than inside the rollback that would use it.

Attributes:

| Name        | Type    | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ----------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `delay`     | `float` | Base delay in seconds — the wait before the first retry. Must be a non-negative, finite number.                                                                                                                                                                                                                                                                                                                                                                                                               |
| `backoff`   | `float` | Multiplier applied once per failed attempt, so attempt n waits delay * backoff \*\* n. Must be >= 1.0: a multiplier below 1 shrinks the delay on every attempt, turning a persistent failure into a retry storm. 1.0 (default) is a flat schedule — every retry waits delay.                                                                                                                                                                                                                                  |
| `max_delay` | \`float | None\`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `jitter`    | `bool`  | When True, the delay is a uniform draw in [0, capped delay] — the "full jitter" shape from the AWS Architecture Blog's Exponential Backoff And Jitter. This de-correlates retries, so a batch of tokens that failed together (a downstream outage, a throttled API) does not re-fire in lockstep. It can draw a near-zero delay, so it bounds the aggregate retry rate rather than any one token's wait. The draws come from the net's jitter stream (PetriNet(retry_rng=...), seeded by PetriNet(seed=...)). |

Raises:

| Type         | Description                                                                                                                        |
| ------------ | ---------------------------------------------------------------------------------------------------------------------------------- |
| `TypeError`  | If delay, backoff, or max_delay is not a number, or jitter is not a bool.                                                          |
| `ValueError` | If delay or max_delay is negative, backoff < 1.0, any is NaN, delay/backoff is infinite, or any is an int too large to be a float. |

Note

Numeric fields are stored as `float`, so `RetryPolicy(delay=1, backoff=2).delay` reads back as `1.0`. The coercion is load-bearing, not cosmetic: `int ** int` is arbitrary-precision and never overflows, so an `int` backoff would grow without bound instead of saturating at the ceiling described under `max_delay`.

Example

```
from cpnx import RetryPolicy, Transition

# Pace a throttled API: 0.5s, 1s, 2s, 4s, ... capped at 30s, jittered.
flaky_api = RetryPolicy(delay=0.5, backoff=2.0, max_delay=30.0, jitter=True)

Transition(
    name="charge_card",
    inputs=[InputArc("pending")],
    outputs=[OutputArc("charged")],
    action=charge,
    retry_policy=flaky_api,
    max_retries=8,
)
```

Source code in `src/cpnx/retries.py`

````
@dataclass(frozen=True)
class RetryPolicy:
    """How long a rolled-back data token waits before each retry.

    The delay for a 0-based `attempt` — the token's `Token.attempts` count at the moment it
    failed, so its first failure is attempt 0 — is

    ```python
    delay = min(delay * backoff ** attempt, max_delay)
    if jitter:
        delay = uniform(0, delay)
    ```

    Attach one per transition ([`Transition.retry_policy`][cpnx.Transition]) to pace each
    dependency on its own terms; a transition that declares none inherits the net's default
    ([`PetriNet.retry_policy`][cpnx.PetriNet]). How *many* times a transition retries before
    its tokens are dead-lettered stays `Transition.max_retries`.

    **The attempt index is per token, not per transition.** `Token.attempts` counts "failed
    firings this token has been rolled back from" anywhere in the net, and `Token.evolve`
    carries it forward, so a token that failed three times upstream arrives already at
    attempt 3: under `RetryPolicy(delay=0.5, backoff=2.0)` its *first* retry at the next
    transition waits 4 s, not 0.5 s. `Transition.max_retries` reads the same counter and so
    has the same property — that token also has three of its retries already spent. This is
    the documented meaning of `attempts` rather than an artefact of backoff, but backoff
    makes it far more visible, so: on a net where tokens cross several fallible transitions,
    either keep `backoff=1.0` (flat, no accumulation to notice), set a `max_delay` low
    enough to bound the inherited growth, or have the action return a token with
    `attempts=0` once it has made progress, which resets the budget for the stages ahead.

    Immutable, comparable and cheap, so one policy can be shared by many transitions.
    Fields are validated on construction — a schedule that could only stall or hot-loop is
    rejected here, where it is written, rather than inside the rollback that would use it.

    Attributes:
        delay: Base delay in seconds — the wait before the first retry. Must be a
            non-negative, finite number.
        backoff: Multiplier applied once per failed attempt, so attempt *n* waits
            `delay * backoff ** n`. Must be `>= 1.0`: a multiplier below 1 shrinks the delay
            on every attempt, turning a persistent failure into a retry storm. `1.0`
            (default) is a flat schedule — every retry waits `delay`.
        max_delay: Ceiling in seconds on a single delay, applied **before** jitter. `None`
            (default) means uncapped, which is only sensible with `backoff=1.0` or a small
            `Transition.max_retries` — uncapped growth parks a token for longer and longer,
            and under `max_retries=None` the exponent eventually runs past the float range
            (saturating at infinity, i.e. parking the token for good). `math.inf` is
            accepted as an explicit spelling of uncapped.
        jitter: When `True`, the delay is a uniform draw in `[0, capped delay]` — the "full
            jitter" shape from the AWS Architecture Blog's
            [Exponential Backoff And Jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/).
            This de-correlates retries, so a batch of tokens that failed together (a
            downstream outage, a throttled API) does not re-fire in lockstep. It can draw a
            near-zero delay, so it bounds the *aggregate* retry rate rather than any one
            token's wait. The draws come from the net's jitter stream
            (`PetriNet(retry_rng=...)`, seeded by `PetriNet(seed=...)`).

    Raises:
        TypeError: If `delay`, `backoff`, or `max_delay` is not a number, or `jitter` is not
            a `bool`.
        ValueError: If `delay` or `max_delay` is negative, `backoff < 1.0`, any is `NaN`,
            `delay`/`backoff` is infinite, or any is an `int` too large to be a `float`.

    Note:
        Numeric fields are stored as `float`, so `RetryPolicy(delay=1, backoff=2).delay`
        reads back as `1.0`. The coercion is load-bearing, not cosmetic: `int ** int` is
        arbitrary-precision and never overflows, so an `int` backoff would grow without
        bound instead of saturating at the ceiling described under `max_delay`.

    Example:
        ```python
        from cpnx import RetryPolicy, Transition

        # Pace a throttled API: 0.5s, 1s, 2s, 4s, ... capped at 30s, jittered.
        flaky_api = RetryPolicy(delay=0.5, backoff=2.0, max_delay=30.0, jitter=True)

        Transition(
            name="charge_card",
            inputs=[InputArc("pending")],
            outputs=[OutputArc("charged")],
            action=charge,
            retry_policy=flaky_api,
            max_retries=8,
        )
        ```
    """

    delay: float = 1.0
    backoff: float = 1.0
    max_delay: float | None = None
    jitter: bool = False

    def __post_init__(self) -> None:
        """Reject a schedule that could not retry sanely, and store the fields as `float`.

        See `_validated_retry_number`. Keeping the *coerced* value matters as much as the
        check: an `int` field would make `backoff ** attempt` arbitrary-precision integer
        arithmetic, which never raises `OverflowError`, so `delay_for` would hand the
        rollback a 1000-bit `int` instead of saturating — and `ref_time + delay` would then
        raise `OverflowError` outside `_RetrySchedule.delay_for`'s guard, stranding every
        token not yet re-deposited. `float ** int` saturates where this class says it does.
        """
        if not isinstance(self.jitter, bool):
            # Checked because a truthy non-bool (`jitter="false"`) would silently turn jitter
            # *on*, which is the opposite of what it reads as.
            raise TypeError(f"RetryPolicy.jitter must be a bool, got {type(self.jitter).__name__}: {self.jitter!r}.")
        object.__setattr__(self, "delay", _validated_retry_number("RetryPolicy.delay", self.delay, minimum=0.0))
        object.__setattr__(
            self,
            "backoff",
            _validated_retry_number(
                "RetryPolicy.backoff",
                self.backoff,
                minimum=1.0,
                hint=(
                    " A multiplier below 1 shrinks the delay on every attempt, which turns a "
                    "persistent failure into a retry storm."
                ),
            ),
        )
        object.__setattr__(
            self,
            "max_delay",
            _validated_retry_number(
                "RetryPolicy.max_delay", self.max_delay, minimum=0.0, allow_none=True, allow_infinite=True
            ),
        )

    def delay_for(self, attempt: int, rng: random.Random | None = None) -> float:
        """Return the delay in seconds to wait before retry number `attempt` (0-based).

        Args:
            attempt: The token's `Token.attempts` count at the moment it failed, so its
                first failure *anywhere in the net* is `0`. The counter travels with the
                token, so a token carrying failures from an upstream transition starts
                further along this schedule — see the class docstring.
            rng: Jitter source, used only when `jitter` is set. The engine always supplies
                one; called directly without it, the delay is the unjittered ceiling.

        Returns:
            The delay in seconds, `min(delay * backoff ** attempt, max_delay)`, drawn
            uniformly from `[0, that]` when `jitter` is set and `rng` is given.

        Raises:
            ValueError: If `attempt` is negative. A negative attempt silently returned
                `delay` (the `attempt > 0` branch is skipped), hiding the caller's bug.
            TypeError: If `rng.uniform` returns anything but a non-negative finite number.
        """
        if attempt < 0:
            raise ValueError(f"RetryPolicy.delay_for: attempt must be >= 0 (0 is the first retry), got {attempt}.")
        delay = self._capped(self._grown(attempt))
        if self.jitter and rng is not None and math.isfinite(delay):
            # Full jitter: uniform over the whole capped window. `uniform(0, inf)` can
            # produce a NaN, so an infinite window is left unjittered (it is already a park).
            delay = _validated_jitter_sample(rng.uniform(0.0, delay), delay)
        return delay

    def _capped(self, delay: float) -> float:
        """Clamp `delay` to `max_delay`, which is applied before jitter."""
        if self.max_delay is not None and delay > self.max_delay:
            return self.max_delay
        return delay

    def _grown(self, attempt: int) -> float:
        """Return `delay * backoff ** attempt`, saturating at infinity rather than raising.

        Two short-circuits come first, for the cases where the exponent cannot change the
        answer. A zero base stays zero for every attempt and every backoff — without this the
        saturation below would turn `0 * huge` (still 0) into an infinite park. And a base
        already at or above the cap can only grow into it, so the result is `max_delay`
        whatever the exponent — worth skipping, because `backoff ** attempt` is real work at a
        large `attempt`.
        """
        if not self.delay or self.backoff == 1.0 or attempt == 0:
            return self.delay
        if self.max_delay is not None and self.delay >= self.max_delay:
            return self.max_delay
        try:
            return self.delay * self.backoff**attempt
        except OverflowError:
            pass
        # `backoff ** attempt` overflowed, but that is the *intermediate* term: with a base
        # below 1.0 the true product can still be finite (`0.1 * 2**1024` is 1.8e307). Redo it
        # in log space, where the base pulls the exponent back down, before concluding that an
        # unbounded (`max_retries=None`) net has genuinely run past the float range. An
        # uncapped policy that really does overflow parks the token, which is why `max_delay`
        # is advised.
        try:
            return math.exp(math.log(self.delay) + attempt * math.log(self.backoff))
        except (OverflowError, ValueError):
            return math.inf
````

### __post_init__

```
__post_init__() -> None
```

Reject a schedule that could not retry sanely, and store the fields as `float`.

See `_validated_retry_number`. Keeping the *coerced* value matters as much as the check: an `int` field would make `backoff ** attempt` arbitrary-precision integer arithmetic, which never raises `OverflowError`, so `delay_for` would hand the rollback a 1000-bit `int` instead of saturating — and `ref_time + delay` would then raise `OverflowError` outside `_RetrySchedule.delay_for`'s guard, stranding every token not yet re-deposited. `float ** int` saturates where this class says it does.

Source code in `src/cpnx/retries.py`

```
def __post_init__(self) -> None:
    """Reject a schedule that could not retry sanely, and store the fields as `float`.

    See `_validated_retry_number`. Keeping the *coerced* value matters as much as the
    check: an `int` field would make `backoff ** attempt` arbitrary-precision integer
    arithmetic, which never raises `OverflowError`, so `delay_for` would hand the
    rollback a 1000-bit `int` instead of saturating — and `ref_time + delay` would then
    raise `OverflowError` outside `_RetrySchedule.delay_for`'s guard, stranding every
    token not yet re-deposited. `float ** int` saturates where this class says it does.
    """
    if not isinstance(self.jitter, bool):
        # Checked because a truthy non-bool (`jitter="false"`) would silently turn jitter
        # *on*, which is the opposite of what it reads as.
        raise TypeError(f"RetryPolicy.jitter must be a bool, got {type(self.jitter).__name__}: {self.jitter!r}.")
    object.__setattr__(self, "delay", _validated_retry_number("RetryPolicy.delay", self.delay, minimum=0.0))
    object.__setattr__(
        self,
        "backoff",
        _validated_retry_number(
            "RetryPolicy.backoff",
            self.backoff,
            minimum=1.0,
            hint=(
                " A multiplier below 1 shrinks the delay on every attempt, which turns a "
                "persistent failure into a retry storm."
            ),
        ),
    )
    object.__setattr__(
        self,
        "max_delay",
        _validated_retry_number(
            "RetryPolicy.max_delay", self.max_delay, minimum=0.0, allow_none=True, allow_infinite=True
        ),
    )
```

### delay_for

```
delay_for(attempt: int, rng: Random | None = None) -> float
```

Return the delay in seconds to wait before retry number `attempt` (0-based).

Parameters:

| Name      | Type     | Description                                                                                                                                                                                                                                                         | Default                                                                                                                                       |
| --------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `attempt` | `int`    | The token's Token.attempts count at the moment it failed, so its first failure anywhere in the net is 0. The counter travels with the token, so a token carrying failures from an upstream transition starts further along this schedule — see the class docstring. | *required*                                                                                                                                    |
| `rng`     | \`Random | None\`                                                                                                                                                                                                                                                              | Jitter source, used only when jitter is set. The engine always supplies one; called directly without it, the delay is the unjittered ceiling. |

Returns:

| Type    | Description                                                               |
| ------- | ------------------------------------------------------------------------- |
| `float` | The delay in seconds, min(delay * backoff \*\* attempt, max_delay), drawn |
| `float` | uniformly from [0, that] when jitter is set and rng is given.             |

Raises:

| Type         | Description                                                                                                                      |
| ------------ | -------------------------------------------------------------------------------------------------------------------------------- |
| `ValueError` | If attempt is negative. A negative attempt silently returned delay (the attempt > 0 branch is skipped), hiding the caller's bug. |
| `TypeError`  | If rng.uniform returns anything but a non-negative finite number.                                                                |

Source code in `src/cpnx/retries.py`

```
def delay_for(self, attempt: int, rng: random.Random | None = None) -> float:
    """Return the delay in seconds to wait before retry number `attempt` (0-based).

    Args:
        attempt: The token's `Token.attempts` count at the moment it failed, so its
            first failure *anywhere in the net* is `0`. The counter travels with the
            token, so a token carrying failures from an upstream transition starts
            further along this schedule — see the class docstring.
        rng: Jitter source, used only when `jitter` is set. The engine always supplies
            one; called directly without it, the delay is the unjittered ceiling.

    Returns:
        The delay in seconds, `min(delay * backoff ** attempt, max_delay)`, drawn
        uniformly from `[0, that]` when `jitter` is set and `rng` is given.

    Raises:
        ValueError: If `attempt` is negative. A negative attempt silently returned
            `delay` (the `attempt > 0` branch is skipped), hiding the caller's bug.
        TypeError: If `rng.uniform` returns anything but a non-negative finite number.
    """
    if attempt < 0:
        raise ValueError(f"RetryPolicy.delay_for: attempt must be >= 0 (0 is the first retry), got {attempt}.")
    delay = self._capped(self._grown(attempt))
    if self.jitter and rng is not None and math.isfinite(delay):
        # Full jitter: uniform over the whole capped window. `uniform(0, inf)` can
        # produce a NaN, so an infinite window is left unjittered (it is already a park).
        delay = _validated_jitter_sample(rng.uniform(0.0, delay), delay)
    return delay
```

## Side-effect linting

Guards, arc `key`/`filter` selectors, and output-arc conditions should be deterministic and side-effect-free — they are evaluated to decide *enabling*, so network I/O, database reads, or clock/randomness use in them make the net's behaviour non-reproducible and undermine static analysability (see [ADR 0007](https://github.com/philgresh/cpnx/blob/main/docs/adr/0007-best-effort-side-effect-linting.md)). `cpnx` runs a best-effort AST linter at construction time that **warns** (CpnxLintWarning) when it detects these trouble spots. Set `CPNX_LINT_STRICT=1` or call set_strict to promote warnings to errors for CI gating, or call lint_callable directly to inspect a callable programmatically.

## cpnx.lint_callable

```
lint_callable(func: Callable) -> list[LintFinding]
```

Scan *func*'s body for network / database / clock / randomness trouble spots.

Pure inspection: never warns, never raises, never mutates. Returns findings in source order, de-duplicated by `(category, symbol, lineno)`. Returns `[]` when *func* is not callable or its source cannot be recovered (a compiled builtin, a REPL lambda) — an unseeable body is reported as "nothing found", never as a false positive, consistent with the best-effort contract.

Before Python 3.11 (and under `-X no_debug_ranges`) the interpreter records no column positions, so two lambdas that start on one source line and use the same names cannot be told apart: each is then reported with the findings of both, and each message says so. Put them on separate lines to lint each alone.

Source code in `src/cpnx/linting.py`

```
def lint_callable(func: Callable) -> list[LintFinding]:
    """Scan *func*'s body for network / database / clock / randomness trouble spots.

    Pure inspection: never warns, never raises, never mutates. Returns findings in
    source order, de-duplicated by ``(category, symbol, lineno)``. Returns ``[]``
    when *func* is not callable or its source cannot be recovered (a compiled
    builtin, a REPL lambda) — an unseeable body is reported as "nothing found",
    never as a false positive, consistent with the best-effort contract.

    Before Python 3.11 (and under ``-X no_debug_ranges``) the interpreter records no column
    positions, so two lambdas that start on one source line and use the same names cannot be
    told apart: each is then reported with the findings of both, and each message says so.
    Put them on separate lines to lint each alone.
    """
    if not callable(func):
        return []
    nodes = _callable_nodes_or_empty(func)
    note = _alike_note(nodes)
    findings: list[LintFinding] = []
    seen: set[tuple[str, str, int]] = set()
    for child in (child for node in nodes for child in ast.walk(node)):
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
                message=f"'{symbol}' looks like {category} ({_WHY[category]}){note}",
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
