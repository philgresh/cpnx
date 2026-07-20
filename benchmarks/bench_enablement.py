"""Benchmark: cost of repeated transition-enablement checks with string expressions.

The engine re-evaluates ``_is_transition_enabled`` for every transition on every
``step()``. When a transition carries a *string* guard or a *string* input-arc
expression, that string is fed to :class:`cpnx.sandbox.SandboxEvaluator`. This
benchmark isolates that hot path so the cost of (re)parsing/compiling the
expression versus reusing a cached/compiled code object is directly visible.

It also isolates the *other* dispatch path: a **callable** guard. Per
``engine.py``'s ``_eval_expression``, a string expression is evaluated inline via
``SandboxEvaluator.evaluate_compiled``, while a callable expression is round-tripped
through ``_call_expr`` -- ``self._expr_executor.submit(fn, *args)`` followed by
``fut.result(timeout=...)`` -- a full ThreadPoolExecutor submission *per call*, taken
while holding the engine's global lock. The string and callable guards below compute
the exact same predicate (``len(tokens) >= 1 and tokens[0].color == 'data'``) so the
reported ratio isolates dispatch overhead, not predicate cost. Report the ratio, not
the raw microseconds -- absolute figures are hardware- and interpreter-specific.

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
from cpnx.transitions import BindingPolicy, InputArc, OutputArc, Transition  # noqa: E402

N = 50_000
TOKEN_COUNT = 200

# The callable guard MUST be a module-level `def` (or a plain lambda) -- never a
# dynamically-defined function -- because `Transition.__setattr__` runs
# `verify_callable_purity`, which uses `inspect.getsource` and raises `PermissionError`
# for callables whose source isn't retrievable that way.
#
# This computes the *exact same predicate* as the string guard below
# (``len(tokens) >= 1 and bool(tokens[0].color == 'data')``) so the string-vs-callable
# comparison isolates dispatch overhead rather than differences in the predicate body.
def guard_callable(tokens: list[Token]) -> bool:
    return len(tokens) >= 1 and bool(tokens[0].color == "data")


GUARD_STRING = "len(tokens) >= 1 and bool(tokens[0].color == 'data')"


def build_net(
    binding_policy: BindingPolicy = BindingPolicy.LEGACY,
    guard_kind: str = "string",
) -> tuple[PetriNet, Transition]:
    """A net whose single transition uses a string or callable guard + string input-arc expression.

    ``binding_policy`` selects the transition's binding-resolution strategy so the same
    enablement check can be timed under ``LEGACY`` (leading-token check) and ``FIRST``
    (deterministic-complete guarded search).

    ``guard_kind`` is ``"string"`` (evaluated inline via `SandboxEvaluator`) or
    ``"callable"`` (round-tripped through `_call_expr` -> the `_expr_executor` thread
    pool). Both compute the identical predicate -- see `GUARD_STRING`/`guard_callable`.
    """
    if guard_kind not in ("string", "callable"):
        raise ValueError(f"unknown guard_kind: {guard_kind!r}")

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
        # Callable guard: dispatched through `_call_expr` -> executor.submit + fut.result.
        guard=GUARD_STRING if guard_kind == "string" else guard_callable,
        binding_policy=binding_policy,
    )
    net.add_transition(transition)

    for i in range(TOKEN_COUNT):
        net.deposit("input", Token(color="data", payload={"i": i}))

    return net, transition


# RANDOM/PRIORITY resolve the binding by scanning *every* candidate (see
# `_iter_satisfying_bindings`), so a callable guard there pays the ~10us `_call_expr`
# thread-pool round trip once per token (TOKEN_COUNT=200), per call. At the default `N`
# that's 50_000 * 200 * ~10us ~= 100s per policy -- too slow for a micro benchmark. Use a
# smaller call count for exactly that combination; every other combination is O(1) guard
# evaluations per call and safely uses the full `N`.
N_CALLABLE_SCAN = 500


def _time_policy(label: str, binding_policy: BindingPolicy, guard_kind: str = "string") -> float:
    """Time `_is_transition_enabled`; returns the measured microseconds/call."""
    net, transition = build_net(binding_policy, guard_kind=guard_kind)

    # Warm up (and prove the transition is actually enabled, so the guard runs).
    assert net._is_transition_enabled(transition), "transition should be enabled"

    elapsed = timeit.timeit(lambda: net._is_transition_enabled(transition), number=N)
    per_call_us = elapsed / N * 1e6
    print(
        f"_is_transition_enabled [{label:8}] [{guard_kind:8}]: "
        f"{elapsed:.4f}s for {N} calls  ({per_call_us:.3f} us/call)"
    )
    return per_call_us


def _time_resolve(label: str, binding_policy: BindingPolicy, guard_kind: str = "string") -> float:
    """Time the *firing* resolution path (``_resolve_binding``); returns microseconds/call.

    Unlike ``_is_transition_enabled`` (an existence probe that short-circuits at the first
    satisfying binding for every policy), the firing path is where ``RANDOM`` and ``PRIORITY``
    pay to enumerate the whole candidate set to sample / rank it.
    """
    net, transition = build_net(binding_policy, guard_kind=guard_kind)
    m_time = net._get_model_time_under_lock()
    assert net._resolve_binding(transition, m_time) is not None, "transition should be enabled"

    scans_full_candidate_set = binding_policy in (BindingPolicy.RANDOM, BindingPolicy.PRIORITY)
    calls = N_CALLABLE_SCAN if (guard_kind == "callable" and scans_full_candidate_set) else N

    elapsed = timeit.timeit(lambda: net._resolve_binding(transition, m_time), number=calls)
    per_call_us = elapsed / calls * 1e6
    note = "" if calls == N else f"  [reduced N={calls}: full-scan x callable dispatch]"
    print(
        f"_resolve_binding       [{label:8}] [{guard_kind:8}]: "
        f"{elapsed:.4f}s for {calls} calls  ({per_call_us:.3f} us/call){note}"
    )
    return per_call_us


def _ratio(callable_us: float, string_us: float) -> str:
    return f"{callable_us / string_us:.1f}x"


def main() -> None:
    print("-- _is_transition_enabled: string guard vs callable guard --")
    print(f"   predicate (both arms): {GUARD_STRING}")
    for label, policy in (("LEGACY", BindingPolicy.LEGACY), ("FIRST", BindingPolicy.FIRST)):
        string_us = _time_policy(label, policy, guard_kind="string")
        callable_us = _time_policy(label, policy, guard_kind="callable")
        print(f"   ratio [{label:8}]: callable/string = {_ratio(callable_us, string_us)}")
    print()

    # Firing path: RANDOM/PRIORITY must scan all candidates, so they cost more than FIRST.
    print("-- _resolve_binding: string guard vs callable guard --")
    for label, policy in (
        ("LEGACY", BindingPolicy.LEGACY),
        ("FIRST", BindingPolicy.FIRST),
        ("RANDOM", BindingPolicy.RANDOM),
        ("PRIORITY", BindingPolicy.PRIORITY),
    ):
        string_us = _time_resolve(label, policy, guard_kind="string")
        callable_us = _time_resolve(label, policy, guard_kind="callable")
        print(f"   ratio [{label:8}]: callable/string = {_ratio(callable_us, string_us)}")


if __name__ == "__main__":
    main()
