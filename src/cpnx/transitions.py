import weakref
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, ClassVar

from cpnx.sandbox import SandboxEvaluator, verify_callable_purity
from cpnx.tokens import Token

if TYPE_CHECKING:
    from cpnx.engine import PetriNet


@dataclass
class InputArc:
    """An arc from a place to a transition (consumes tokens).

    In CPN formalism an input arc carries an *arc expression* — a function
    that, given the current tokens in the place, determines which tokens are
    consumed and in what order. :attr:`expression` is that function.

    Attributes:
        place: Name of the source place.
        count: Number of tokens to consume. Ignored when ``consume_all=True``.
        consume_all: Drain the entire place atomically.
        settle_secs: Wait for no new arrivals for this many seconds before
                     consuming.
        expression: CPN input arc expression. Receives all tokens currently
                    in the place; returns them in desired consumption order.
                    Can be a pure Callable or a sandboxed expression string.
                    The engine consumes the first ``count`` tokens from the
                    result. ``None`` (default) = FIFO order.
    """

    place: str
    count: int = 1
    consume_all: bool = False
    settle_secs: float = 0.0
    expression: Callable[[list[Token]], list[Token]] | str | None = field(default=None, compare=False)

    def __post_init__(self):
        #: Pre-compiled code object for a string ``expression`` (``None`` for callables).
        self._compiled_expression = (
            SandboxEvaluator.compile_expression(self.expression) if isinstance(self.expression, str) else None
        )
        if callable(self.expression):
            verify_callable_purity(self.expression)


@dataclass
class OutputArc:
    """An arc from a transition to a place (produces tokens).

    In CPN formalism an output arc carries an *arc expression* — a function
    evaluated against the transition's output tokens that determines whether
    tokens flow along this arc. :attr:`expression` is that predicate.

    Attributes:
        place: Name of the target place.
        count: Number of tokens to deposit.
        expression: CPN output arc expression. Receives the list of non-resource
                    output tokens returned by the action; the arc is *skipped*
                    (no tokens deposited) when it returns ``False``. ``None``
                    (default) means the arc always fires.
                    Can be a pure Callable or a sandboxed expression string.
    """

    place: str
    count: int = 1
    expression: Callable[[list[Token]], bool] | str | None = field(default=None, compare=False)

    def __post_init__(self):
        #: Pre-compiled code object for a string ``expression`` (``None`` for callables).
        self._compiled_expression = (
            SandboxEvaluator.compile_expression(self.expression) if isinstance(self.expression, str) else None
        )
        if callable(self.expression):
            verify_callable_purity(self.expression)

    @classmethod
    def on_color(cls, color: str, place: str, count: int = 1) -> "OutputArc":
        """Create an OutputArc that only fires if the first returned token has the given color."""
        expr_str = f"bool(tokens and tokens[0].color == {color!r})"
        return cls(place=place, count=count, expression=expr_str)


@dataclass
class Transition:
    """A transition that fires when all its input places are enabled.

    In CPN formalism a transition has:

    - **Input arcs** with arc expressions determining which tokens are consumed
    - **Output arcs** with arc expressions determining which tokens are produced
    - A **guard** — a boolean predicate over the binding that must hold for the
      transition to be enabled (evaluated before the action runs)

    Attributes:
        name: Unique identifier within a :class:`PetriNet`.
        inputs: Input arcs (place → transition).
        outputs: Output arcs (transition → place).
        action: Callable consuming input tokens and returning output tokens.
                Runs on the thread pool outside the engine lock.
        guard: CPN transition guard — ``Callable[[list[Token]], bool]`` or expression string.
               Evaluated while holding the engine lock; return ``False`` to block firing.
               ``None`` (default) = always enabled.
        priority: Lower value fires first when multiple transitions are enabled.
        action_timeout_secs: Maximum wall-clock seconds the action may run. When ``None``
               (default), the action runs without a deadline — fully backward compatible.
               When set, the engine triggers atomic rollback if the action does not return
               within this duration: all consumed tokens are returned to their source places
               (data tokens with a 1-second ``available_at`` delay to prevent livelock), and
               the ``on_error`` callback fires with a ``RuntimeError`` that names the
               transition and the elapsed timeout.

               **This does not kill the underlying OS thread.** The timed-out action
               continues running in the background until it completes or the process exits;
               its return value is silently discarded. Callers must apply native I/O
               timeouts inside their actions (e.g. ``requests.get(timeout=...)``,
               ``httpx`` client timeouts) to prevent zombie thread accumulation.
               ``action_timeout_secs`` is defense-in-depth, not a substitute for proper
               I/O timeout discipline.
        max_retries: Maximum number of times to retry the transition action on failure
               before dead-lettering the data tokens.
               ``None`` -> infinite retry (today's behavior; forfeits the quiescence guarantee).
               ``0`` -> route data token(s) to ``error_place`` on the first failure.
               ``N > 0`` -> retry up to ``N`` times, then route to ``error_place``. Default is 5.
    """

    name: str
    inputs: list[InputArc]
    outputs: list[OutputArc]
    action: Callable[[list[Token]], list[Token]]
    guard: Callable[[list[Token]], bool] | str | None = None
    priority: int = 10
    action_timeout_secs: float | None = None
    max_retries: int | None = 5

    def __post_init__(self):
        # Actions are explicitly allowed side effects (e.g., database writes,
        # API calls, I/O), so they are not subject to purity verification.
        #: Pre-compiled code object for a string ``guard`` (``None`` for callables).
        self._compiled_guard = (
            SandboxEvaluator.compile_expression(self.guard) if isinstance(self.guard, str) else None
        )
        if callable(self.guard):
            verify_callable_purity(self.guard)


@dataclass
class SubstitutionTransition(Transition):
    """A transition that encapsulates an entire sub-PetriNet (Hierarchical CPN).

    In CPNs, a substitution transition abstracts a subnet. The child subnet
    is fully insulated from the parent net — it carries no reference to its
    parent. Communication occurs strictly through the ``port_socket_map``:
    named port places in the subnet are bound to named socket places in the
    parent at construction time.

    A subnet instance may only be wrapped by one ``SubstitutionTransition`` at a
    time. Attempting to wrap the same subnet twice raises :exc:`ValueError`.
    """

    _mapped_subnets: ClassVar[weakref.WeakSet] = weakref.WeakSet()

    subnet: "PetriNet" = field(default=None)  # type: ignore[assignment]
    port_socket_map: dict[str, str] = field(default_factory=dict)
    subnet_deadline_secs: float = 30.0

    def __post_init__(self):
        super().__post_init__()
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
