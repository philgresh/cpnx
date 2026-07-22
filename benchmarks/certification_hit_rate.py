"""Informational benchmark: what fraction of the repo's real guards/selectors certify.

``cpnx.certification.certify`` proves a callable guard, arc selector (``InputArc.key``,
``InputArc.filter``, ``OutputArc.condition``), or ``binding_priority_key`` is closed-world
and provably terminating, which lets the engine dispatch it *inline* (no
``ThreadPoolExecutor`` round trip -- see ``bench_enablement.py`` for how much that dispatch
choice costs). A callable that fails certification simply falls back to the timeout-bounded
executor; nothing breaks, it's just slower.

This script does **not** hand-write toy guards. It builds the two real benchmark nets --
``benchmarks/bench_enablement.py``'s ``build_net`` (both ``guard_kind`` variants) and
``benchmarks/concurrency_cafe.py``'s ``build_cafe`` -- and certifies every guard, input-arc
``key``, input-arc ``filter``, output-arc ``condition``, and binding-priority key actually
attached to their transitions. It reports the certification *hit rate* across that corpus:
the fraction of real callables that certify for inline execution.

This is **purely informational** -- there is no assertion, no exit code, no CI gate. A high
hit rate is the empirical justification for *not* prioritizing further work on the executor
fallback path: if inline certification already covers the overwhelming majority of real
guards/selectors, the uncertified/executor path matters less for typical workloads. A
low hit rate would suggest the opposite. Either way this script only reports the number; it
never fails the build.

Run it directly:

    python benchmarks/certification_hit_rate.py
"""

import sys
from pathlib import Path
from typing import Callable, NamedTuple

# Make ``src/`` (and this benchmarks/ dir, for ``concurrency_cafe``/``bench_enablement``)
# importable from a bare checkout, mirroring the other benchmark scripts.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_enablement import build_net  # noqa: E402
from concurrency_cafe import build_cafe  # noqa: E402

from cpnx.certification import certify  # noqa: E402
from cpnx.engine import PetriNet  # noqa: E402


class Callable_(NamedTuple):
    """One certifiable callable pulled off a real net, plus a label identifying where it lives."""

    label: str
    func: Callable


def _collect_callables(net: PetriNet, net_label: str) -> list[Callable_]:
    """Collect every guard/selector/priority-key attached to *net*'s transitions.

    Walks ``net.transitions.values()`` and, for each transition, gathers ``guard``,
    ``binding_priority_key``, every input arc's ``key`` and ``filter``, and every output
    arc's ``condition`` -- whichever of those attributes is not ``None``. These are the real
    callables the engine actually has to dispatch (inline or via the executor) when the net
    runs.
    """
    found: list[Callable_] = []
    for transition in net.transitions.values():
        prefix = f"{net_label}:{transition.name}"
        if transition.guard is not None:
            found.append(Callable_(f"{prefix}.guard", transition.guard))
        if transition.binding_priority_key is not None:
            found.append(Callable_(f"{prefix}.binding_priority_key", transition.binding_priority_key))
        for i, arc in enumerate(transition.inputs):
            if arc.key is not None:
                found.append(Callable_(f"{prefix}.inputs[{i}:{arc.place}].key", arc.key))
            if arc.filter is not None:
                found.append(Callable_(f"{prefix}.inputs[{i}:{arc.place}].filter", arc.filter))
        for i, arc in enumerate(transition.outputs):
            if arc.condition is not None:
                found.append(Callable_(f"{prefix}.outputs[{i}:{arc.place}].condition", arc.condition))
    return found


def _build_corpus() -> list[Callable_]:
    """Build the real benchmark nets and collect their certifiable callables.

    - ``bench_enablement.build_net``: both ``guard_kind`` variants ("certified" and
      "uncertified"), so the corpus includes at least one guard each side certifies and
      rejects.
    - ``concurrency_cafe.build_cafe``: the default configuration, which installs the dose
      guard (``T_Weigh_And_Grind``), its complement (``T_Rework_Dose``), the
      mobile-pickup-first ``binding_priority_key``, and the ``OutputArc.on_color``
      conditions on ``T_Steam_Milk``.
    """
    corpus: list[Callable_] = []

    net_certified, _ = build_net(guard_kind="certified")
    corpus += _collect_callables(net_certified, "bench_enablement[certified]")

    net_uncertified, _ = build_net(guard_kind="uncertified")
    corpus += _collect_callables(net_uncertified, "bench_enablement[uncertified]")

    # Default config installs the dose guard/rework guard/priority key/on_color conditions
    # described in the module docstring above.
    cafe_net = build_cafe()
    corpus += _collect_callables(cafe_net, "concurrency_cafe")

    # De-duplicate by function identity: the same shared callable object must only be
    # counted once even if it is reachable from more than one label (it isn't here, but
    # this keeps the corpus honest if the nets above ever come to share callables).
    seen: set[int] = set()
    deduped: list[Callable_] = []
    for item in corpus:
        if id(item.func) in seen:
            continue
        seen.add(id(item.func))
        deduped.append(item)
    return deduped


def main() -> None:
    corpus = _build_corpus()

    print("-- Certification hit rate over the repo's real guards/selectors --")
    print(f"{'label':55} {'certified':10} reason (if rejected)")
    print("-" * 100)

    certified_count = 0
    for label, func in corpus:
        verdict = certify(func)
        if verdict.certified:
            certified_count += 1
            print(f"{label:55} {'True':10}")
        else:
            print(f"{label:55} {'False':10} {verdict.reason}")

    total = len(corpus)
    ratio = certified_count / total * 100 if total else 0.0
    print("-" * 100)
    print(f"certified {certified_count} / {total} callables ({ratio:.1f}%)")
    print()
    print(
        "Informational only, not a gate: this ratio is the empirical case for deferring "
        "further work on the executor-fallback path -- the closer to 100%, the less that "
        "path matters for real workloads."
    )


if __name__ == "__main__":
    main()
