"""Benchmark: cost of repeated transition-enablement checks with string expressions.

The engine re-evaluates ``_is_transition_enabled`` for every transition on every
``step()``. When a transition carries a *string* guard or a *string* input-arc
expression, that string is fed to :class:`cpnx.sandbox.SandboxEvaluator`. This
benchmark isolates that hot path so the cost of (re)parsing/compiling the
expression versus reusing a cached/compiled code object is directly visible.

Run it on ``main`` (pre-fix) and on the optimized branch (post-fix) and compare
the reported microseconds-per-call. Native stdlib only -- no dependencies.

    python benchmarks/bench_enablement.py
"""

import sys
import timeit
from pathlib import Path

# Make ``src/`` importable when run from a checkout without installation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cpnx.engine import PetriNet  # noqa: E402
from cpnx.places import Place  # noqa: E402
from cpnx.tokens import Token  # noqa: E402
from cpnx.transitions import InputArc, OutputArc, Transition  # noqa: E402

N = 50_000
TOKEN_COUNT = 200


def build_net() -> tuple[PetriNet, Transition]:
    """A net whose single transition uses a string guard + string input-arc expression."""
    net = PetriNet()
    net.add_place(Place("input"))
    net.add_place(Place("output"))

    transition = Transition(
        name="t",
        # String input-arc expression: returns an ordering of the tokens (identity
        # here -- the point is to exercise the parse/compile path, not the ordering).
        inputs=[InputArc("input", count=1, expression="tokens")],
        outputs=[OutputArc("output")],
        action=lambda tokens: tokens,
        # String guard: re-parsed/compiled on every enablement check pre-fix.
        guard="len(tokens) >= 1 and bool(tokens[0].color == 'data')",
    )
    net.add_transition(transition)

    for i in range(TOKEN_COUNT):
        net.deposit("input", Token(color="data", payload={"i": i}))

    return net, transition


def main() -> None:
    net, transition = build_net()

    # Warm up (and prove the transition is actually enabled, so the guard runs).
    assert net._is_transition_enabled(transition), "transition should be enabled"

    elapsed = timeit.timeit(lambda: net._is_transition_enabled(transition), number=N)
    per_call_us = elapsed / N * 1e6
    print(f"_is_transition_enabled: {elapsed:.4f}s for {N} calls  ({per_call_us:.3f} us/call)")


if __name__ == "__main__":
    main()
