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

import contextlib
import math
import random
import warnings
from dataclasses import dataclass
from typing import cast


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
            `allow_infinite` — including an `int` too large to be a `float` at all, which
            is an infinity by any other name.
    """
    if value is None:
        if allow_none:
            return None
        raise TypeError(f"{name} must be a number, got None.")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number, got {type(value).__name__}: {value!r}.")
    try:
        number = float(value)
    except OverflowError as exc:
        # A Python `int` has no bound, so `10**400` reaches here as a legal number that no
        # `float` can hold. Report it as the out-of-range value it is, not as the raw
        # `OverflowError` from the conversion — this function promises TypeError/ValueError.
        raise ValueError(
            f"{name} must be within the float range, got an int of {value.bit_length()} bits. "
            "A delay that large parks the token for good."
        ) from exc
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

    **The attempt index is per token, not per transition.** `Token.attempts` counts "failed
    firings this token has been rolled back from" anywhere in the net, and `Token.evolve`
    carries it forward, so a token that failed three times upstream arrives already at
    attempt 3: under `RetryPolicy(delay=0.5, backoff=2.0)` its *first* retry at the next
    transition waits 4 s, not 0.5 s. `Transition.max_retries` reads the same counter and so
    has the same property — that token also has three of its retries already spent. This is
    the documented meaning of `attempts` rather than an artefact of backoff, but backoff
    makes it far more visible, so: on a net where tokens cross several fallible transitions,
    either keep `backoff=1.0` (flat, no accumulation to notice), set a `max_delay` low
    enough to bound the inherited growth, or have the action return a token with
    `attempts=0` once it has made progress, which resets the budget for the stages ahead.

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
        TypeError: If `delay`, `backoff`, or `max_delay` is not a number, or `jitter` is not
            a `bool`.
        ValueError: If `delay` or `max_delay` is negative, `backoff < 1.0`, any is `NaN`,
            `delay`/`backoff` is infinite, or any is an `int` too large to be a `float`.

    Note:
        Numeric fields are stored as `float`, so `RetryPolicy(delay=1, backoff=2).delay`
        reads back as `1.0`. The coercion is load-bearing, not cosmetic: `int ** int` is
        arbitrary-precision and never overflows, so an `int` backoff would grow without
        bound instead of saturating at the ceiling described under `max_delay`.

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
        """Reject a schedule that could not retry sanely, and store the fields as `float`.

        See `_validated_retry_number`. Keeping the *coerced* value matters as much as the
        check: an `int` field would make `backoff ** attempt` arbitrary-precision integer
        arithmetic, which never raises `OverflowError`, so `delay_for` would hand the
        rollback a 1000-bit `int` instead of saturating — and `ref_time + delay` would then
        raise `OverflowError` outside `_RetrySchedule.delay_for`'s guard, stranding every
        token not yet re-deposited. `float ** int` saturates where this class says it does.
        """
        if not isinstance(self.jitter, bool):
            # Checked because a truthy non-bool (`jitter="false"`) would silently turn jitter
            # *on*, which is the opposite of what it reads as.
            raise TypeError(f"RetryPolicy.jitter must be a bool, got {type(self.jitter).__name__}: {self.jitter!r}.")
        object.__setattr__(self, "delay", _validated_retry_number("RetryPolicy.delay", self.delay, minimum=0.0))
        object.__setattr__(
            self,
            "backoff",
            _validated_retry_number(
                "RetryPolicy.backoff",
                self.backoff,
                minimum=1.0,
                hint=(
                    " A multiplier below 1 shrinks the delay on every attempt, which turns a "
                    "persistent failure into a retry storm."
                ),
            ),
        )
        object.__setattr__(
            self,
            "max_delay",
            _validated_retry_number(
                "RetryPolicy.max_delay", self.max_delay, minimum=0.0, allow_none=True, allow_infinite=True
            ),
        )

    def delay_for(self, attempt: int, rng: random.Random | None = None) -> float:
        """Return the delay in seconds to wait before retry number `attempt` (0-based).

        Args:
            attempt: The token's `Token.attempts` count at the moment it failed, so its
                first failure *anywhere in the net* is `0`. The counter travels with the
                token, so a token carrying failures from an upstream transition starts
                further along this schedule — see the class docstring.
            rng: Jitter source, used only when `jitter` is set. The engine always supplies
                one; called directly without it, the delay is the unjittered ceiling.

        Returns:
            The delay in seconds, `min(delay * backoff ** attempt, max_delay)`, drawn
            uniformly from `[0, that]` when `jitter` is set and `rng` is given.

        Raises:
            ValueError: If `attempt` is negative. A negative attempt silently returned
                `delay` (the `attempt > 0` branch is skipped), hiding the caller's bug.
            TypeError: If `rng.uniform` returns anything but a non-negative finite number.
        """
        if attempt < 0:
            raise ValueError(f"RetryPolicy.delay_for: attempt must be >= 0 (0 is the first retry), got {attempt}.")
        delay = self._capped(self._grown(attempt))
        if self.jitter and rng is not None and math.isfinite(delay):
            # Full jitter: uniform over the whole capped window. `uniform(0, inf)` can
            # produce a NaN, so an infinite window is left unjittered (it is already a park).
            delay = _validated_jitter_sample(rng.uniform(0.0, delay), delay)
        return delay

    def _capped(self, delay: float) -> float:
        """Clamp `delay` to `max_delay`, which is applied before jitter."""
        if self.max_delay is not None and delay > self.max_delay:
            return self.max_delay
        return delay

    def _grown(self, attempt: int) -> float:
        """Return `delay * backoff ** attempt`, saturating at infinity rather than raising.

        Two short-circuits come first, for the cases where the exponent cannot change the
        answer. A zero base stays zero for every attempt and every backoff — without this the
        saturation below would turn `0 * huge` (still 0) into an infinite park. And a base
        already at or above the cap can only grow into it, so the result is `max_delay`
        whatever the exponent — worth skipping, because `backoff ** attempt` is real work at a
        large `attempt`.
        """
        if not self.delay or self.backoff == 1.0 or attempt == 0:
            return self.delay
        if self.max_delay is not None and self.delay >= self.max_delay:
            return self.max_delay
        try:
            return self.delay * self.backoff**attempt
        except OverflowError:
            pass
        # `backoff ** attempt` overflowed, but that is the *intermediate* term: with a base
        # below 1.0 the true product can still be finite (`0.1 * 2**1024` is 1.8e307). Redo it
        # in log space, where the base pulls the exponent back down, before concluding that an
        # unbounded (`max_retries=None`) net has genuinely run past the float range. An
        # uncapped policy that really does overflow parks the token, which is why `max_delay`
        # is advised.
        try:
            return math.exp(math.log(self.delay) + attempt * math.log(self.backoff))
        except (OverflowError, ValueError):
            return math.inf


def _validated_jitter_sample(sample: object, window: float) -> float:
    """Return `sample` as a float once it is a usable delay, else raise.

    `validated_jitter_rng` proves a generator can serve a draw at construction, but it can
    only probe `uniform(0.0, 0.0)` — a source that returns `None`, a string, a `NaN` or a
    negative on a *real* window slips past it. Each of those is silently destructive rather
    than loud: a non-number defers its `TypeError` to `ref_time + delay` in the engine's
    rollback, outside the guard that keeps a rollback from stranding tokens; a `NaN`
    `available_at` compares false against every clock and corrupts the cooling heaps'
    ordering; a negative schedules the retry in the past. Raising here instead puts the
    failure *inside* `_RetrySchedule.delay_for`, which falls back to the unjittered delay.
    """
    if isinstance(sample, bool) or not isinstance(sample, (int, float)) or not math.isfinite(sample) or sample < 0.0:
        raise TypeError(f"retry_rng.uniform(0.0, {window}) must return a non-negative finite number, got {sample!r}.")
    return float(sample)


#: The schedule a net falls back on for transitions that declare no `retry_policy`:
#: a flat 1-second delay, which is what cpnx did before policies existed.
DEFAULT_RETRY_POLICY = RetryPolicy()


def validated_jitter_rng(rng: object, *, name: str = "retry_rng") -> random.Random:
    """Return `rng` once a trial draw proves it can actually serve jitter.

    A jitter source is the one piece of caller-supplied code the engine invokes *on the
    rollback path* — under the engine lock, holding tokens that have been consumed but not
    yet returned — so a broken one must be rejected here, at construction. Checking that
    `uniform` merely *exists and is callable* is not enough: `random.Random` (the class,
    not an instance) passes that test and then raises on the draw, because the unbound
    method takes `self`. So this draws for real and requires a usable number back.

    The trial draw consumes randomness, which would shift the caller's stream and make a
    seeded generator produce a different jitter sequence than the same seed does elsewhere.
    So the state is snapshotted and restored around the probe when the generator supports
    `getstate`/`setstate` (every `random.Random` does); a duck-typed source without them
    keeps the draw, since validating it matters more than its stream position.

    Args:
        rng: The candidate jitter source.
        name: Parameter name, used in the error messages.

    Returns:
        `rng` unchanged, typed as a `random.Random`, its stream left where it was.

    Raises:
        TypeError: If `rng` has no callable `uniform`, if a trial `uniform(0.0, 0.0)` call
            raises, or if it returns anything but a real, finite number.
    """
    draw = getattr(rng, "uniform", None)
    if not callable(draw):
        raise TypeError(f"{name} must have a callable `uniform(a, b)`, got {type(rng).__name__}.")
    getstate, setstate = getattr(rng, "getstate", None), getattr(rng, "setstate", None)
    state = None
    if callable(getstate) and callable(setstate):
        try:
            state = getstate()
        except Exception:  # noqa: BLE001,S110 — unusable state support; the draw below reports why
            state = None
    try:
        sample = draw(0.0, 0.0)
    except Exception as exc:  # noqa: BLE001 — any failure here means it cannot serve a draw
        raise TypeError(
            f"{name}.uniform(0.0, 0.0) raised {type(exc).__name__}: {exc}. Pass a random "
            f"generator *instance* (e.g. `random.Random(0)`), not the class itself."
        ) from exc
    finally:
        if state is not None:
            # Suppressed: restoring the stream is a courtesy to the caller, not a validity
            # test. A duck-typed source whose `setstate` raises must not mask the probe's own
            # TypeError above, nor fail a generator that just proved it can draw.
            with contextlib.suppress(Exception):
                setstate(state)
    if isinstance(sample, bool) or not isinstance(sample, (int, float)) or not math.isfinite(sample):
        raise TypeError(f"{name}.uniform(0.0, 0.0) must return a finite number, got {sample!r}.")
    return cast(random.Random, rng)


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
        """Return the delay for `attempt` under `retry_policy`, or the net default if `None`.

        Never raises. This is called from `_rollback_failed_transition`, which returns
        consumed tokens to their places one at a time and has no `except` of its own, so an
        exception here would strand every token not yet re-deposited — silently, with
        `on_error` unfired and the net reporting itself quiescent. The jitter source is
        caller-supplied, and `PetriNet` proves it works with a trial draw at construction
        (`validated_jitter_rng`), so this is the belt behind that brace: a draw that fails
        anyway degrades to the unjittered delay and warns, rather than losing work.

        "Never raises" is meant literally, so neither the warning nor the fallback may raise
        either: `warnings.warn` *does* raise under `-W error` (a common test setting), and
        recomputing the unjittered delay would re-raise anything that was not the draw's
        fault. The last resort is a zero delay — an immediate retry is recoverable, a
        stranded token is not.
        """
        policy = retry_policy or self.default
        try:
            return policy.delay_for(attempt, self.rng)
        except Exception as exc:  # noqa: BLE001 — a rollback must not raise; see the docstring
            with contextlib.suppress(Exception):  # `-W error` would turn this warning into a raise
                warnings.warn(
                    f"Retry jitter draw failed ({type(exc).__name__}: {exc}); falling back to the "
                    f"unjittered delay for attempt {attempt}. Check the generator passed as "
                    "`PetriNet(retry_rng=...)`.",
                    RuntimeWarning,
                    stacklevel=2,
                )
            try:
                return policy.delay_for(attempt)
            except Exception:  # noqa: BLE001 — the jitter was not the cause; retry immediately
                return 0.0
