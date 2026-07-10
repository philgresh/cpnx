import weakref
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, ClassVar

from cpnx.sandbox import SandboxEvaluator, verify_callable_purity
from cpnx.tokens import Token

if TYPE_CHECKING:
    from cpnx.engine import PetriNet


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
    """

    name: str
    inputs: list[InputArc]
    outputs: list[OutputArc]
    action: Callable[[list[Token]], list[Token]]
    guard: Callable[[list[Token]], bool] | str | None = None
    priority: int = 10
    action_timeout_secs: float | None = None
    max_retries: int | None = 5

    def __setattr__(self, name, value):
        # Keep the pre-compiled guard in sync with ``guard``, including
        # post-construction reassignment. Actions are explicitly allowed side effects
        # (DB writes, API calls, I/O), so only guards are purity-verified.
        if name == "guard":
            compiled = SandboxEvaluator.maybe_compile(value)
            if callable(value):
                verify_callable_purity(value)
            super().__setattr__("_compiled_guard", compiled)
        super().__setattr__(name, value)


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
