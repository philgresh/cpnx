"""Benchmark: cost of repeated transition-enablement checks, inline vs executor dispatch.

The engine re-evaluates ``_is_transition_enabled`` for every transition on every
``step()``, and under ``RANDOM``/``PRIORITY`` it evaluates the guard once per
*candidate binding*. How a callable guard is dispatched therefore dominates that
hot path:

- a **certified** callable (closed-world, proven terminating -- see
  ``cpnx.certification``) is called *inline*, directly, with no timeout;
- an **uncertified** callable is round-tripped through ``_call_expr`` --
  ``self._expr_executor.submit(fn, *args)`` then ``fut.result(timeout=...)`` -- a
  full ThreadPoolExecutor submission *per call*, taken while holding the engine's
  global lock.

Both guards below compute the *exact same* predicate
(``len(tokens) >= 1 and tokens[0].color == 'data'``); the only difference is that the
uncertified one reads a module-level mutable dict, which the certifier rejects (a guard
closing over external mutable state), forcing it onto the executor path. So the reported
ratio isolates dispatch overhead, not predicate cost.

(String guards/expressions were removed -- callables are the only expression form -- so
this benchmark, which used to compare string-vs-callable, now compares the two callable
dispatch paths that remain.) Report the ratio, not the raw microseconds; absolute figures
are hardware- and interpreter-specific. Native stdlib only -- no dependencies.

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

# Guard callables MUST be module-level `def`s (never dynamically defined) because
# `Transition.__setattr__` runs `verify_callable_purity`, which uses `inspect.getsource`
# and raises for callables whose source isn't retrievable that way.


# Certified: closed-world (only whitelisted builtins over the parameter), so it runs inline.
def guard_certified(tokens: list[Token]) -> bool:
    return len(tokens) >= 1 and bool(tokens[0].color == "data")


# Uncertified: identical predicate, but reads a module-level *mutable* dict. That closure
# over external mutable state is exactly what certification rejects, so it is dispatched
# through the timeout-bounded executor instead of inline. `verify_callable_purity` still
# accepts it (no I/O), so construction succeeds.
_EXPECTED = {"color": "data"}


def guard_uncertified(tokens: list[Token]) -> bool:
    return len(tokens) >= 1 and bool(tokens[0].color == _EXPECTED["color"])


def _accept_all_filter(token: Token) -> bool:
    # Certified identity input-arc filter (runs inline); accepts every token unchanged.
    # Setting it (even to an always-true predicate) routes the arc through the
    # per-token filter/sort path (`engine._order_available`) instead of the count==1
    # fast-path bypass that applies when both `key` and `filter` are `None`, matching
    # this benchmark's original intent of exercising that path.
    return True


def build_net(
    binding_policy: BindingPolicy = BindingPolicy.LEGACY,
    guard_kind: str = "certified",
) -> tuple[PetriNet, Transition]:
    """A net whose single transition uses a certified or uncertified callable guard.

    ``binding_policy`` selects the transition's binding-resolution strategy so the same
    enablement check can be timed under ``LEGACY`` (leading-token check) and ``FIRST``
    (deterministic-complete guarded search).

    ``guard_kind`` is ``"certified"`` (evaluated inline) or ``"uncertified"`` (round-tripped
    through `_call_expr` -> the `_expr_executor` thread pool). Both compute the identical
    predicate -- see `guard_certified`/`guard_uncertified`.
    """
    if guard_kind not in ("certified", "uncertified"):
        raise ValueError(f"unknown guard_kind: {guard_kind!r}")

    net = PetriNet()
    net.add_place(Place("input"))
    net.add_place(Place("output"))

    transition = Transition(
        name="t",
        inputs=[InputArc("input", count=1, filter=_accept_all_filter)],
        outputs=[OutputArc("output")],
        action=lambda tokens: tokens,
        guard=guard_certified if guard_kind == "certified" else guard_uncertified,
        binding_policy=binding_policy,
    )
    net.add_transition(transition)

    for i in range(TOKEN_COUNT):
        net.deposit("input", Token(color="data", payload={"i": i}))

    return net, transition


# RANDOM/PRIORITY resolve the binding by scanning *every* candidate (see
# `_iter_satisfying_bindings`), so an uncertified guard there pays the ~10us `_call_expr`
# thread-pool round trip once per token (TOKEN_COUNT=200), per call. At the default `N`
# that's 50_000 * 200 * ~10us ~= 100s per policy -- too slow for a micro benchmark. Use a
# smaller call count for exactly that combination; every other combination is O(1) guard
# evaluations per call and safely uses the full `N`.
N_EXECUTOR_SCAN = 500


def _time_policy(label: str, binding_policy: BindingPolicy, guard_kind: str = "certified") -> float:
    """Time `_is_transition_enabled`; returns the measured microseconds/call."""
    net, transition = build_net(binding_policy, guard_kind=guard_kind)

    # Warm up (and prove the transition is actually enabled, so the guard runs).
    assert net._is_transition_enabled(transition), "transition should be enabled"

    elapsed = timeit.timeit(lambda: net._is_transition_enabled(transition), number=N)
    per_call_us = elapsed / N * 1e6
    print(
        f"_is_transition_enabled [{label:8}] [{guard_kind:11}]: "
        f"{elapsed:.4f}s for {N} calls  ({per_call_us:.3f} us/call)"
    )
    return per_call_us


def _time_resolve(label: str, binding_policy: BindingPolicy, guard_kind: str = "certified") -> float:
    """Time the *firing* resolution path (``_resolve_binding``); returns microseconds/call.

    Unlike ``_is_transition_enabled`` (an existence probe that short-circuits at the first
    satisfying binding for every policy), the firing path is where ``RANDOM`` and ``PRIORITY``
    pay to enumerate the whole candidate set to sample / rank it.
    """
    net, transition = build_net(binding_policy, guard_kind=guard_kind)
    m_time = net._get_model_time_under_lock()
    assert net._resolve_binding(transition, m_time) is not None, "transition should be enabled"

    scans_full_candidate_set = binding_policy in (BindingPolicy.RANDOM, BindingPolicy.PRIORITY)
    calls = N_EXECUTOR_SCAN if (guard_kind == "uncertified" and scans_full_candidate_set) else N

    elapsed = timeit.timeit(lambda: net._resolve_binding(transition, m_time), number=calls)
    per_call_us = elapsed / calls * 1e6
    note = "" if calls == N else f"  [reduced N={calls}: full-scan x executor dispatch]"
    print(
        f"_resolve_binding       [{label:8}] [{guard_kind:11}]: "
        f"{elapsed:.4f}s for {calls} calls  ({per_call_us:.3f} us/call){note}"
    )
    return per_call_us


def _ratio(uncertified_us: float, certified_us: float) -> str:
    return f"{uncertified_us / certified_us:.1f}x"


def main() -> None:
    print("-- _is_transition_enabled: certified (inline) vs uncertified (executor) guard --")
    print("   predicate (both arms): len(tokens) >= 1 and tokens[0].color == 'data'")
    for label, policy in (("LEGACY", BindingPolicy.LEGACY), ("FIRST", BindingPolicy.FIRST)):
        certified_us = _time_policy(label, policy, guard_kind="certified")
        uncertified_us = _time_policy(label, policy, guard_kind="uncertified")
        print(f"   ratio [{label:8}]: uncertified/certified = {_ratio(uncertified_us, certified_us)}")
    print()

    # Firing path: RANDOM/PRIORITY must scan all candidates, so they cost more than FIRST.
    print("-- _resolve_binding: certified (inline) vs uncertified (executor) guard --")
    for label, policy in (
        ("LEGACY", BindingPolicy.LEGACY),
        ("FIRST", BindingPolicy.FIRST),
        ("RANDOM", BindingPolicy.RANDOM),
        ("PRIORITY", BindingPolicy.PRIORITY),
    ):
        certified_us = _time_resolve(label, policy, guard_kind="certified")
        uncertified_us = _time_resolve(label, policy, guard_kind="uncertified")
        print(f"   ratio [{label:8}]: uncertified/certified = {_ratio(uncertified_us, certified_us)}")


if __name__ == "__main__":
    main()
