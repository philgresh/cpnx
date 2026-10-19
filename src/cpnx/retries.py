"""Retry scheduling — how long a rolled-back data token waits before each retry.

When a transition's action fails, the engine rolls its data tokens back to their source
places with a delay (disposition B — see [`PetriNet`][cpnx.PetriNet]). A
[`RetryPolicy`][cpnx.RetryPolicy] says how long that delay is for each successive attempt:
capped exponential backoff with optional full jitter, the shape recommended by the AWS
Builders' Library's
[Timeouts, retries, and backoff with jitter](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/)
and used by the AWS SDKs' `standard` retry mode.

A policy is attached per transition ([`Transition.retry_policy`][cpnx.Transition]) because
retry behaviour belongs to the *dependency* a transition talks to, not to the net: a
throttled third-party API wants a doubling delay with a 30 s ceiling and jitter, while a
local write wants 50 ms flat. A transition that declares none inherits the net's default
([`PetriNet.retry_policy`][cpnx.PetriNet]).
"""

import math
import random
from dataclasses import dataclass


def _validated_retry_number(
    name: str,
    value: object,
    *,
    minimum: float,
    allow_none: bool = False,
    allow_infinite: bool = False,
    hint: str = "",
) -> float | None:
    """Validate one retry number and return it as a `float` (or `None`).

    Rejects the mistakes that are otherwise *silent* at run time rather than loud: a
    negative delay schedules the retry in the past (a hot loop on a failing transition),
    a `NaN` makes every `available_at` comparison false (the token is never retrievable
    again — a stalled net, not an error), and an infinite delay parks the token for good.
    `NaN` in particular slips past every ``<`` check, so it needs its own test.

    Args:
        name: Field name, used in the error messages.
        value: The value to validate.
        minimum: Smallest value accepted.
        allow_none: Whether `None` is a legal value (it maps to "unset").
        allow_infinite: Whether an infinity is a legal value (it spells "uncapped").
        hint: Extra sentence appended to the out-of-range error.

    Returns:
        The value as a `float`, or `None` when `value` is `None` and `allow_none` is set.

    Raises:
        TypeError: If `value` is not a real number (or `None` without `allow_none`).
        ValueError: If `value` is `NaN`, below `minimum`, or infinite without
            `allow_infinite`.
    """
    if value is None:
        if allow_none:
            return None
        raise TypeError(f"{name} must be a number, got None.")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number, got {type(value).__name__}: {value!r}.")
    number = float(value)
    if math.isnan(number):
        raise ValueError(
            f"{name} must not be NaN. A NaN delay compares false against every clock, so the "
            "token would never become retrievable again and the net would stall silently."
        )
    if number < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {number}.{hint}")
    if math.isinf(number) and not allow_infinite:
        raise ValueError(f"{name} must be finite, got {number}. An infinite delay parks the token for good.")
    return number


@dataclass(frozen=True)
class RetryPolicy:
    """How long a rolled-back data token waits before each retry.

    The delay for a 0-based `attempt` — the token's `Token.attempts` count at the moment it
    failed, so its first failure is attempt 0 — is

    ```python
    delay = min(delay * backoff ** attempt, max_delay)
    if jitter:
        delay = uniform(0, delay)
    ```

    Attach one per transition ([`Transition.retry_policy`][cpnx.Transition]) to pace each
    dependency on its own terms; a transition that declares none inherits the net's default
    ([`PetriNet.retry_policy`][cpnx.PetriNet]). How *many* times a transition retries before
    its tokens are dead-lettered stays `Transition.max_retries`.

    Immutable, comparable and cheap, so one policy can be shared by many transitions.
    Fields are validated on construction — a schedule that could only stall or hot-loop is
    rejected here, where it is written, rather than inside the rollback that would use it.

    Attributes:
        delay: Base delay in seconds — the wait before the first retry. Must be a
            non-negative, finite number.
        backoff: Multiplier applied once per failed attempt, so attempt *n* waits
            `delay * backoff ** n`. Must be `>= 1.0`: a multiplier below 1 shrinks the delay
            on every attempt, turning a persistent failure into a retry storm. `1.0`
            (default) is a flat schedule — every retry waits `delay`.
        max_delay: Ceiling in seconds on a single delay, applied **before** jitter. `None`
            (default) means uncapped, which is only sensible with `backoff=1.0` or a small
            `Transition.max_retries` — uncapped growth parks a token for longer and longer,
            and under `max_retries=None` the exponent eventually runs past the float range
            (saturating at infinity, i.e. parking the token for good). `math.inf` is
            accepted as an explicit spelling of uncapped.
        jitter: When `True`, the delay is a uniform draw in `[0, capped delay]` — the "full
            jitter" shape from the AWS Architecture Blog's
            [Exponential Backoff And Jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/).
            This de-correlates retries, so a batch of tokens that failed together (a
            downstream outage, a throttled API) does not re-fire in lockstep. It can draw a
            near-zero delay, so it bounds the *aggregate* retry rate rather than any one
            token's wait. The draws come from the net's jitter stream
            (`PetriNet(retry_rng=...)`, seeded by `PetriNet(seed=...)`).

    Raises:
        TypeError: If `delay`, `backoff`, or `max_delay` is not a number.
        ValueError: If `delay` or `max_delay` is negative, `backoff < 1.0`, any is `NaN`,
            or `delay`/`backoff` is infinite.

    Example:
        ```python
        from cpnx import RetryPolicy, Transition

        # Pace a throttled API: 0.5s, 1s, 2s, 4s, ... capped at 30s, jittered.
        flaky_api = RetryPolicy(delay=0.5, backoff=2.0, max_delay=30.0, jitter=True)

        Transition(
            name="charge_card",
            inputs=[InputArc("pending")],
            outputs=[OutputArc("charged")],
            action=charge,
            retry_policy=flaky_api,
            max_retries=8,
        )
        ```
    """

    delay: float = 1.0
    backoff: float = 1.0
    max_delay: float | None = None
    jitter: bool = False

    def __post_init__(self) -> None:
        """Reject a schedule that could not retry sanely. See `_validated_retry_number`."""
        _validated_retry_number("RetryPolicy.delay", self.delay, minimum=0.0)
        _validated_retry_number(
            "RetryPolicy.backoff",
            self.backoff,
            minimum=1.0,
            hint=(
                " A multiplier below 1 shrinks the delay on every attempt, which turns a "
                "persistent failure into a retry storm."
            ),
        )
        _validated_retry_number(
            "RetryPolicy.max_delay", self.max_delay, minimum=0.0, allow_none=True, allow_infinite=True
        )

    def delay_for(self, attempt: int, rng: random.Random | None = None) -> float:
        """Return the delay in seconds to wait before retry number `attempt` (0-based).

        Args:
            attempt: The token's `Token.attempts` count at the moment it failed, so its
                first failure is `0`.
            rng: Jitter source, used only when `jitter` is set. The engine always supplies
                one; called directly without it, the delay is the unjittered ceiling.

        Returns:
            The delay in seconds, `min(delay * backoff ** attempt, max_delay)`, drawn
            uniformly from `[0, that]` when `jitter` is set and `rng` is given.
        """
        delay = self.delay
        if self.backoff != 1.0 and attempt > 0:
            try:
                delay = self.delay * self.backoff**attempt
            except OverflowError:
                # An unbounded (`max_retries=None`) net with `backoff > 1` can run the
                # exponent past the float range. Saturate rather than raise; an uncapped
                # policy then parks the token, which is why `max_delay` is advised.
                delay = math.inf
        if self.max_delay is not None and delay > self.max_delay:
            delay = self.max_delay
        if self.jitter and rng is not None and math.isfinite(delay):
            # Full jitter: uniform over the whole capped window. `uniform(0, inf)` can
            # produce a NaN, so an infinite window is left unjittered (it is already a park).
            delay = rng.uniform(0.0, delay)
        return delay


#: The schedule a net falls back on for transitions that declare no `retry_policy`:
#: a flat 1-second delay, which is what cpnx did before policies existed.
DEFAULT_RETRY_POLICY = RetryPolicy()


@dataclass(frozen=True)
class _RetrySchedule:
    """A net's resolved retry context: its default policy plus its jitter stream.

    Threaded into the rollback path so it can resolve each transition's policy at the
    moment it fails. The `rng` is drawn under the engine lock (see
    `_rollback_failed_transition`), so it needs no locking of its own.
    """

    default: RetryPolicy
    rng: random.Random | None = None

    def delay_for(self, retry_policy: RetryPolicy | None, attempt: int) -> float:
        """Return the delay for `attempt` under `retry_policy`, or the net default if `None`."""
        return (retry_policy or self.default).delay_for(attempt, self.rng)
