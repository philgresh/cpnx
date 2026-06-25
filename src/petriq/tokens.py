import time
from dataclasses import dataclass, field
from uuid import uuid4


@dataclass
class Token:
    id: str = field(default_factory=lambda: uuid4().hex[:8])
    payload: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.monotonic)
    is_resource: bool = False
