from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from petriq.sandbox import verify_callable_purity
from petriq.tokens import Token

if TYPE_CHECKING:
    from petriq.engine import PetriNet


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
        if callable(self.expression):
            verify_callable_purity(self.expression)


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
    """

    name: str
    inputs: list[InputArc]
    outputs: list[OutputArc]
    action: Callable[[list[Token]], list[Token]]
    guard: Callable[[list[Token]], bool] | str | None = None
    priority: int = 10

    def __post_init__(self):
        # Actions are explicitly allowed side effects (e.g., database writes,
        # API calls, I/O), so they are not subject to purity verification.
        if callable(self.guard):
            verify_callable_purity(self.guard)


@dataclass
class SubstitutionTransition(Transition):
    """A transition that encapsulates an entire sub-PetriNet (Hierarchical CPN).

    In CPNs, a substitution transition abstracts a subnet. The child subnet
    is insulated from the parent net. Communication occurs strictly via Ports in
    the subnet mapped to Sockets in the parent net.
    """

    subnet: "PetriNet" = field(default=None)  # type: ignore[assignment]
    port_socket_map: dict[str, str] = field(default_factory=dict)

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

        if getattr(self.subnet, "_parent_transition", None) is not None:
            raise ValueError(
                f"SubstitutionTransition '{self.name}': child subnet is already mapped to "
                f"transition '{self.subnet._parent_transition}'."
            )
        self.subnet._parent_transition = self.name
