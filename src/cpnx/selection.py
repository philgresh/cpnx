"""External binding selection: let a callable choose the tokens a transition binds (#80).

A transition with a `binding_selector` does not search the product of its input arcs. The
engine hands the selector a read-only [`BindingView`][cpnx.BindingView] of the tokens each
input arc could take, and fires exactly the tokens it returns, once it has checked them. See
ADR 0013.
"""

import random
import weakref
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Callable, Iterable, Iterator

from cpnx.certification import is_inline_safe
from cpnx.places import Place
from cpnx.tokens import Token

if TYPE_CHECKING:
    from cpnx.transitions import InputArc, Transition

BindingSelector = Callable[
    ["BindingView"], "Sequence[Sequence[Token] | Token] | Mapping[int, Sequence[Token] | Token] | None"
]
"""A transition's binding selector: a view in, one token choice per input arc (or `None`) out."""


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


_EMPTY_GROUP = TokenGroup(())


class BindingView:
    """What a [`binding_selector`][cpnx.Transition] sees: the tokens each input arc could take.

    Read-only, and valid only for the duration of the selector call: it reads the places
    directly, under the engine lock, without copying them.

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
        lambda or bound method built per call), so the index is built once and reused.
        """
        return {k: TokenGroup(g) for k, g in self._groups(arc, key, None).items()}

    def group(self, arc: int, key: Callable[[Token], object], value: object) -> TokenGroup:
        """The tokens input arc *arc* could take whose `key(token) == value`; O(1) when indexed."""
        found = self._groups(arc, key, (value,)).get(value)
        return _EMPTY_GROUP if found is None else TokenGroup(found)


class InvalidSelection(ValueError):
    """A binding selector returned a choice the engine cannot fire.

    The `__cause__` of the error `on_error` receives for a rejected choice, which tells a bad
    choice apart from a selector that raised.
    """


def _as_tokens(item: object, arc: "InputArc", position: int) -> list[Token]:
    tokens = [item] if isinstance(item, Token) else list(item)  # type: ignore[call-overload]
    if len(tokens) != arc.count or not all(isinstance(t, Token) for t in tokens):
        raise InvalidSelection(
            f"input arc {position} on '{arc.place}' takes {arc.count} token(s); the selector chose {tokens!r}."
        )
    return tokens


def _choice_items(choice: object, arcs: "Sequence[InputArc]") -> list[object]:
    """One raw choice per input arc, from a sequence in `inputs` order or a mapping by arc index."""
    if isinstance(choice, Mapping):
        if set(choice) != set(range(len(arcs))):
            raise InvalidSelection(
                f"the selector's choice must name input arcs 0..{len(arcs) - 1}, got {list(choice)}."
            )
        return [choice[i] for i in range(len(arcs))]
    if isinstance(choice, Sequence) and not isinstance(choice, (str, bytes, Token)):
        if len(choice) != len(arcs):
            raise InvalidSelection(f"the selector returned {len(choice)} choice(s) for {len(arcs)} input arc(s).")
        return list(choice)
    raise InvalidSelection(f"a selector returns None, a sequence or a mapping, not {type(choice).__name__}.")


def _reject_repeats(per_arc: list[list[Token]]) -> None:
    seen: set[int] = set()
    for token in (t for tokens in per_arc for t in tokens):
        if id(token) in seen:
            raise InvalidSelection(f"the selector chose token {token.id} more than once.")
        seen.add(id(token))


def normalize_choice(choice: object, arcs: "Sequence[InputArc]") -> list[list[Token]]:
    """Turn a selector's return value into one token list per input arc, or raise `InvalidSelection`."""
    items = _choice_items(choice, arcs)
    per_arc = [_as_tokens(item, arc, i) for i, (item, arc) in enumerate(zip(items, arcs, strict=True))]
    _reject_repeats(per_arc)
    return per_arc


def selector_conflict(transition: "Transition") -> str | None:
    """Why `transition`'s arcs or settings cannot be combined with its selector, or `None`."""
    if transition.binding_priority_key is not None:
        return "binding_priority_key has no effect on a transition with a binding_selector"
    for arc in transition.inputs:
        for name in ("join_key", "key", "filter"):
            if getattr(arc, name) is not None:
                return f"input arc on '{arc.place}' sets {name}, which a binding_selector does not apply"
        if arc.consume_all:
            return f"input arc on '{arc.place}' is consume_all, which a binding_selector cannot choose"
    return None


def _shape(key: Callable[[Token], object]) -> "tuple[object, tuple[object, ...]] | None":
    """What a key's certification depends on beyond its identity: its code, and what it closes over.

    Two functions with the same code, defaults, bound `self` and closure contents certify alike,
    which is what lets a key built inline on every call (a fresh lambda, or a bound method,
    which is a new object on each attribute access) reuse a verdict. `None` for a callable
    that is not a plain function or method.
    """
    func = getattr(key, "__func__", key)
    code = getattr(func, "__code__", None)
    if code is None:
        return None
    try:
        cells = tuple(cell.cell_contents for cell in func.__closure__ or ())
    except ValueError:  # an empty cell: not certifiable anyway, so don't share a verdict
        return None
    bound = getattr(key, "__self__", None) if func is not key else None
    return code, (bound, func.__defaults__, func.__kwdefaults__, func.__globals__, *cells)


def _same(left: tuple[object, ...], right: tuple[object, ...]) -> bool:
    return len(left) == len(right) and all(a is b for a, b in zip(left, right, strict=True))


class KeyCertifications:
    """Remembers whether `BindingView.groups` key functions certify, and which were seen before.

    A verdict is cached by the key's code object together with the identities of what it
    closes over (see `_shape`), so a key built inline on every selector call, such as
    `view.groups(0, lambda t: t.payload["skill"])` or a bound method, is certified once rather
    than on every ask. Both caches are weak in the code object or key, so they stay as large as
    the set of key functions still alive. Other callables are certified on each call.
    """

    def __init__(self) -> None:
        self._verdicts: "weakref.WeakKeyDictionary[object, tuple[tuple[object, ...], bool]]" = (
            weakref.WeakKeyDictionary()
        )
        self._seen: "weakref.WeakSet[Callable[[Token], object]]" = weakref.WeakSet()

    def __len__(self) -> int:
        return len(self._verdicts)

    def inline_safe(self, key: Callable[[Token], object]) -> bool:
        shape = _shape(key)
        if shape is None:
            return is_inline_safe(key)
        code, closed_over = shape
        known = self._verdicts.get(code)
        if known is not None and _same(known[0], closed_over):
            return known[1]
        verdict = is_inline_safe(key)
        self._verdicts[code] = (closed_over, verdict)
        return verdict

    def seen_before(self, key: Callable[[Token], object]) -> bool:
        """Whether this same key object was passed before; records it if not.

        A place index is worth building only for a key the selector passes again. One built
        inline on every call is a new object each time, so it is keyed by a scan instead of
        rebuilding an index over the whole place on each ask.
        """
        try:
            if key in self._seen:
                return True
            self._seen.add(key)
        except TypeError:  # not weakly referenceable: never indexed
            pass
        return False
