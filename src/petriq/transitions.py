from dataclasses import dataclass, field
from typing import Callable

from petriq.tokens import Token


@dataclass
class InputArc:
    """An arc from a place to a transition (consumes tokens).

    In CPN formalism an input arc carries an *arc expression* — a function
    that, given the current tokens in the place, determines which tokens are
    consumed and in what order. :attr:`expression` is that function.

    Attributes:
        place: Name of the source place.
        count: Number of tokens to consume. Ignored when ``consume_all=True``.
        consume_all: Drain the entire place atomically (CPN equivalent: arc
                     expression binding a variable to the full multiset).
                     Pragmatic extension — no direct CPN counterpart.
        settle_secs: Wait for no new arrivals for this many seconds before
                     consuming (batch-settle window). Pragmatic extension —
                     Timed CPNs put timestamps on tokens, not settle windows
                     on arcs.
        expression: CPN input arc expression. Receives all tokens currently
                    in the place; returns them in desired consumption order.
                    The engine consumes the first ``count`` tokens from the
                    result. ``None`` (default) = FIFO order.

    Example — consume the highest-scored lead first::

        InputArc("leads", count=1,
                 expression=lambda tokens: sorted(tokens,
                                                  key=lambda t: -t.payload.get("score", 0)))
    """

    place: str
    count: int = 1
    consume_all: bool = False
    settle_secs: float = 0.0
    expression: Callable[[list[Token]], list[Token]] | None = field(default=None, compare=False)


@dataclass
class OutputArc:
    """An arc from a transition to a place (produces tokens).

    In CPN formalism an output arc carries an *arc expression* — a function
    evaluated against the transition's output tokens that determines whether
    tokens flow along this arc. :attr:`expression` is that predicate.

    Note:
        ``expression`` belongs on the arc, not the transition. This differs from
        :attr:`Transition.guard`, which is a transition-level boolean predicate
        evaluated *before* the action runs (CPN guard semantics).

    Attributes:
        place: Name of the target place.
        count: Number of tokens to deposit.
        expression: CPN output arc expression. Receives the list of non-resource
                    output tokens returned by the action; the arc is *skipped*
                    (no tokens deposited) when it returns ``False``. ``None``
                    (default) means the arc always fires.

    Warning:
        Do not set ``expression`` on arcs targeting
        :class:`~petriq.places.ResourcePlace` or
        :class:`~petriq.places.PacedResourcePlace` — resource tokens must always
        return to a place; expressions on resource arcs are ignored by the engine.

    Example — route to "fast" only when score > 0.8::

        OutputArc("fast", expression=lambda tokens: tokens[0].payload.get("score", 0) > 0.8)
    """

    place: str
    count: int = 1
    expression: Callable[[list[Token]], bool] | None = field(default=None, compare=False)


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
        guard: CPN transition guard — ``Callable[[], bool]``. Evaluated while
               holding the engine lock; return ``False`` to block firing.
               ``None`` (default) = always enabled.
        priority: Lower value fires first when multiple transitions are enabled.
                  Priority classes are a recognised CPN extension (CPN Tools).
    """

    name: str
    inputs: list[InputArc]
    outputs: list[OutputArc]
    action: Callable[[list[Token]], list[Token]]
    guard: Callable[[list[Token]], bool] | None = None
    priority: int = 10
