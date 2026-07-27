"""Shared helpers for the ☕ Concurrency Cafe fixture.

Nothing here models a cafe station in its own right — these are the small pieces
every station borrows: the dose target the whole net is calibrated around, the
tolerance-band arithmetic that turns one `dose_tolerance_g` knob into the
`[low, high]` pair three inscriptions close over, and the `work_secs` wrapper
that gives an otherwise-instant action some GIL-releasing physical duration.
"""

import time
from collections.abc import Callable, Mapping

from cpnx import Token

#: Target dose in grams. Orders in this demo cluster around 18g (a typical single-shot
#: dose); every tolerance band in the net is centered on it.
DOSE_TARGET_G = 18.0

#: Type of a transition action: consumes the bound input tokens, returns output tokens.
Action = Callable[[list[Token]], list[Token]]


def is_order(payload: Mapping) -> bool:
    """Place `schema` predicate for order-ticket queues: a real ticket carries its dose weight.

    Every order is seeded with ``weight_g`` and each pipeline step derives new tokens via
    [`Token.evolve`][cpnx.Token.evolve] (which preserves payload), so grounds and milk tickets
    keep it too. A bare or mis-wired token reaching one of these places would be missing it and
    get dead-lettered — which is the point: unlike the old ``schema=dict`` (always true, since
    every payload is a mapping), this actually rejects something.
    """
    return "weight_g" in payload


def has_payload(payload: Mapping) -> bool:
    """Place `schema` predicate for mixed/terminal queues: every cafe *data* token has a payload.

    Used where a place legitimately holds heterogeneous tokens — freshly-assembled drinks
    (``{"components": ...}``), cold-brew batches (``{"batch": ...}``), station-specific tickets —
    so no single key can be required, but a payload-*less* data token still signals a wiring bug.
    Resource permits are exempt from schema validation, so their empty payload never trips this.
    """
    return len(payload) > 0


def dose_band(dose_tolerance_g: float | None) -> tuple[float | None, float | None]:
    """Expand a half-width tolerance into the ``(low, high)`` band the guards close over.

    Returns ``(None, None)`` when *dose_tolerance_g* is ``None``, which is the fixture's
    signal to omit the dose guard (and `T_Rework_Dose`) entirely and reproduce the cheap
    guard-free binding-search path for A/B comparison.
    """
    if dose_tolerance_g is None:
        return (None, None)
    return (DOSE_TARGET_G - dose_tolerance_g, DOSE_TARGET_G + dose_tolerance_g)


def with_work(work_secs: float, action: Action) -> Action:
    """Wrap *action* so it sleeps ``work_secs`` before running, unless ``work_secs`` is 0.

    Models the physical time a barista actually spends at a station, as opposed to
    [`PacedResourcePlace.pacing_secs`][cpnx.PacedResourcePlace], which models a *machine's* recovery time.
    ``time.sleep`` releases the GIL, which is the whole point — it is what makes
    parallel speedup across the engine's thread pool observable instead of purely
    theoretical, since an instant pure-Python action would just measure CPython.

    Returns *action* unchanged when ``work_secs <= 0``, so the default configuration
    adds no wrapper frame to the profile.
    """
    if work_secs <= 0:
        return action

    def _wrapped(tokens: list[Token]) -> list[Token]:
        time.sleep(work_secs)
        return action(tokens)

    return _wrapped
