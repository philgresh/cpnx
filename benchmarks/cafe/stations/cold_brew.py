"""🧊 The cold-brew tower — a genuinely **deep timed** place.

Cafe role:
    A rack of cold-brew batches steeping overnight. Each batch is put up at a
    different time and is undrinkable until its own steep has elapsed; when one
    matures a barista pours it straight over ice. No grinder, no group head, no
    steam wand — cold brew bypasses the whole espresso pipeline.

Demonstrates:
    The **deep timed marking**. Every other timed thing in the cafe is a
    [`PacedResourcePlace`][cpnx.PacedResourcePlace] with capacity 2-3, so its cooling set never holds more
    than a handful of entries. A cold-brew tower holds dozens-to-hundreds of
    concurrently-steeping tokens, each with its own future [`Token.available_at`][cpnx.Token],
    which is the only shape that puts real pressure on the token store's cooling
    min-heap and on the engine's `_earliest_cooldown_boundary` clock advance.

    Note the *place* is a plain [`Place`][cpnx.Place] — nothing about the class makes it timed.
    What makes it timed is that the tokens deposited into it carry a future
    `available_at`, which is why nothing here deposits: the benchmark stocks the
    tower itself.

    With `key=True` this station also reproduces the **timed×key residual**
    ([#25](https://github.com/philgresh/cpnx/issues/25)) — see `cold_brew_key`.
"""

from cafe.support import with_work
from cpnx import InputArc, OutputArc, Place, Token, Transition


def cold_brew_key(token: Token) -> tuple[int, float]:
    """[`InputArc.key`][cpnx.InputArc] for the tower: biggest cup first, then oldest batch.

    Cafe role:
        Faced with a rack of matured batches, a barista pulls the one that fills the
        largest pending cup — a 20oz order empties a batch usefully, a 12oz leaves an
        awkward remainder. Ties go to whichever has been steeping longest.

    Demonstrates:
        **The timed×key residual, deliberately.** This key is perfectly ordinary and
        fully *certified* — a pure per-token closure over the token's own payload, no
        closed-over mutable state — so on any untimed place it would be served from
        the place's persistent `(key, seq)` min-heap in O(cap log cap).

        It is not, because the place it sits on holds cooling tokens.
        [`Place.peek_by_key`][cpnx.Place.peek_by_key] refuses to answer whenever the store has *any* cooling
        entry: the key index covers the ready set only, and a cooling token is served
        straight off the cooling heap without ever migrating into the ready set, so
        the index cannot claim to represent the whole available pool. Rather than
        return a silently incomplete ordering, it declines and the engine falls back
        to the per-firing filter-then-sort over the full marking.

        The result is the one retrieval shape in the corpus that is still ≈O(N² log N)
        despite doing everything the documentation asks. `build_cafe(cold_brew=True,
        cold_brew_key=True)` is its reproducer; the plain `cold_brew=True` arm is the
        control, identical in every respect except the arc's `key`.
    """
    return (-int(token.payload.get("cup_oz", 12)), token.created_at)


def pull_cold_brew(tokens: list[Token]) -> list[Token]:
    """**T_Pull_Cold_Brew**'s action: pour a matured batch straight into a served drink.

    Cafe role:
        Cold brew is pre-brewed and poured over ice, so a matured batch goes directly
        to the hatch rather than through the shot/milk rendezvous.

    Demonstrates:
        That **maturity needs no check in user code**. The engine refuses to hand this
        action a token whose `available_at` is still in the future (see
        [`Place.retrieve`][cpnx.Place.retrieve]), so arrival at this action *is* the "matured" signal — there
        is deliberately no timestamp comparison in the body.
    """
    steeped = tokens[0]
    return [steeped.evolve(payload_updates={"stage": "cold_brew"}, color="drink")]


def places() -> list[Place]:
    """The tower itself — one colour-restricted [`Place`][cpnx.Place] holding steeping batches."""
    return [Place("P_Cold_Brew_Steeping", color_set={"cold_brew"})]


def transitions(*, work_secs: float = 0.0, key: bool = False) -> list[Transition]:
    """**T_Pull_Cold_Brew** — pour whatever has finished steeping.

    Demonstrates:
        With `key=False` (the default) this is deliberately the plainest transition in
        the whole fixture: no guard, no `key`, no `filter`, default `LEGACY` policy,
        one input arc, one output arc. [`Place.retrieve`][cpnx.Place.retrieve] has already filtered to matured
        tokens before the arc sees them, so plain FIFO over "whatever's ready" is all
        it needs — and that isolates the *timed store* as the only thing being
        measured.

        With `key=True` the arc carries `cold_brew_key` and the station becomes the
        timed×key reproducer instead. Everything else is held constant, so an A/B
        between the two arms attributes the whole difference to the index declining.

    Args:
        work_secs: Physical seconds the station occupies a worker.
        key: Attach `cold_brew_key` to the input arc, reproducing the timed×key
            residual. Defaults to `False`.
    """
    arc = InputArc("P_Cold_Brew_Steeping", key=cold_brew_key) if key else InputArc("P_Cold_Brew_Steeping")
    return [
        Transition(
            name="T_Pull_Cold_Brew",
            inputs=[arc],
            outputs=[OutputArc("P_Served")],
            action=with_work(work_secs, pull_cold_brew),
            action_timeout_secs=0.5,
        )
    ]
