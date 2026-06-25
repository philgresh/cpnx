from dataclasses import dataclass
from typing import Callable

from petriq.tokens import Token


@dataclass
class InputArc:
    place: str
    count: int = 1
    consume_all: bool = False  # drain the entire place (for batch transitions)
    settle_secs: float = 0.0  # wait for no new arrivals before consuming (batch only)


@dataclass
class OutputArc:
    place: str
    count: int = 1


@dataclass
class Transition:
    name: str
    inputs: list[InputArc]
    outputs: list[OutputArc]
    action: Callable[[list[Token]], list[Token]]
    guard: Callable[[], bool] | None = None
    priority: int = 10  # lower = fires first
