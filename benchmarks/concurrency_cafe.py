"""☕ The Concurrency Cafe — entry point and back-compatible import shim.

The fixture itself now lives in the `cafe` package next door, split so that every
place, transition, guard, key, and action is an individually documented symbol (see
`cafe/__init__.py` for the tour, or the rendered page in the docs). This module stays
put because every benchmark and test imports `from concurrency_cafe import build_cafe`,
and because running the demo as a script is the friendliest way to meet the cafe:

    python benchmarks/concurrency_cafe.py
"""

import sys
import time
from pathlib import Path

if __name__ == "__main__":  # pragma: no cover - path shim for standalone execution
    # Mirrors how the repo's pytest config makes ``src`` importable
    # (``pythonpath = ["src"]`` in pyproject.toml): when this file is run directly
    # rather than through pytest, ``cpnx`` is not yet on sys.path, so add it here.
    # ``benchmarks/`` itself must be on the path too, for the ``cafe`` package.
    _here = Path(__file__).resolve().parent
    sys.path.insert(0, str(_here.parent / "src"))
    sys.path.insert(0, str(_here))

from cafe import build_cafe  # noqa: E402

from cpnx import Token  # noqa: E402

__all__ = ["build_cafe"]


if __name__ == "__main__":
    orders = [
        {"ratio": "1:2", "weight_g": 18, "dairy_free": True, "mobile_pickup": False},
        {"ratio": "1:2", "weight_g": 18, "dairy_free": False, "mobile_pickup": True},
        {"ratio": "1:2.5", "weight_g": 20, "dairy_free": False, "mobile_pickup": False},
        {"ratio": "1:2", "weight_g": 18, "dairy_free": True, "mobile_pickup": True},
    ]

    with build_cafe() as net:
        for payload in orders:
            net.deposit("P_Ticket_Line", Token(payload=payload))

        net.run(deadline=time.monotonic() + 2.0)

        marking = net.marking
        print("☕ Concurrency Cafe — final marking:")
        for place_name, tokens in marking.items():
            print(f"  {place_name:20s} {len(tokens)} token(s)")

        served = net.places["P_Served"].stats()
        trashed = net.places["P_Trash_Can"].stats()
        print(f"\nServed: {served['absorbed']} drink(s)")
        print(f"Trashed: {trashed['absorbed']} botched shot(s)")
