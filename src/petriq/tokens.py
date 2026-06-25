import time
from dataclasses import dataclass, field
from uuid import uuid4


@dataclass
class Token:
    """A token flowing through a Petri net.

    In Coloured Petri Net (CPN) theory, a token is a value drawn from its
    place's colour set. Here, ``color`` is that colour: ``None`` for uncoloured
    data tokens, ``"resource"`` for permit/resource tokens, or any user-defined
    string for domain-specific colours.

    Attributes:
        id: Unique identifier (8-char hex). Auto-generated.
        payload: Mutable dict accumulating enrichment data as the token traverses
                 the net. Not used for resource tokens.
        created_at: Monotonic timestamp set at construction.
        color: CPN colour. ``None`` = uncoloured data token;
               ``"resource"`` = permit token (see :class:`~petriq.places.ResourcePlace`);
               any other string = user-defined colour.
    """

    id: str = field(default_factory=lambda: uuid4().hex[:8])
    payload: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.monotonic)
    color: str | None = None

    @property
    def is_resource(self) -> bool:
        """True when this token's colour is ``"resource"``.

        Python-friendly shorthand for ``token.color == "resource"``.
        """
        return self.color == "resource"
