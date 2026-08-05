"""Declarative colour-domain **blast-radius** (impact) analysis for a `PetriNet`.

This is the second prong of cpnx's confidence framework for the Turing-complete
callables users embed in transition inscriptions. Where :mod:`cpnx.linting` answers
*which* transitions reach outside the decidable core (network / database / clock /
randomness — see ADR 0007), this module answers *how far the effect can spread*:
given a transition that mutates or side-effects a token colour domain, it walks the
static net topology **forward** and returns the exact set of downstream places and
transitions that could be reached — the transition's *Impact Map*.

Why declare colours at all
--------------------------
cpnx cannot prove, in general, which colours a Python ``action`` produces (that is
the Halting Problem again). So impact is **declared**, not inferred: a transition
carries an optional :attr:`~cpnx.Transition.impacts_colors` annotation naming the
colour domain(s) it affects, in the spirit of the structural arc inscriptions of
high-level Petri nets (ISO/IEC 15909-1:2019, which route tokens by colour). The
tracer then uses those declarations as a *precision* (Π) to prune the forward walk.

Grounding — cone-of-influence slicing
-------------------------------------
The forward walk is a **cone-of-influence (COI) reduction** over the net's
place/transition graph. COI reduction prunes model elements that cannot affect a
property of interest by analysing the dependency graph, yielding a slice that
(bi)simulates the original with respect to that property (Watanabe, Nishizawa &
Takaki, "A Coalgebraic Representation of Reduction by Cone of Influence," *ENTCS*
164 (2006) 177–194). We follow the *forward* direction — a place is kept only when
its declared ``color_set`` can carry a traced colour — exactly as the on-the-fly
data-flow slice of Telbisz, Bajczi, Szekeres & Vörös ("On-the-fly Cone-of-Influence
Reduction for Model Checking Concurrent Software," SPIN'25) keeps only the
statements a "real observer" can transitively observe (their Def. 5–7).

Soundness stance (over-approximation)
-------------------------------------
The trace is a deliberate **over-approximation**, mirroring the soundness argument
of Telbisz et al. (their Theorem 1: an over-approximate slice never hides a
reachable fault). Concretely:

* an **undeclared** origin (``impacts_colors is None``) traces **every** colour — no
  pruning — because we cannot rule any colour out;
* a place whose ``color_set is None`` (accepts any colour) is **always** included —
  it cannot be excluded from a colour domain it does not constrain.

So the Impact Map may name a place/transition that a more precise analysis would
drop, but it never *omits* one that is genuinely reachable in the declared domain.
Operators use it to *bound* risk, not to prove its absence.

Public API
----------
:func:`trace_impact` returns an :class:`ImpactMap`. :func:`risk_report` fuses the
linter's findings with each flagged transition's blast radius into one
JSON-serialisable report for the verification / debugging path. Both are also
exposed as :meth:`cpnx.PetriNet.trace_impact` / :meth:`cpnx.PetriNet.risk_report`.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cpnx.engine import PetriNet
    from cpnx.transitions import Transition


def coerce_color_domain(value: object, *, field: str = "impacts_colors") -> frozenset[str] | None:
    """Normalise a colour-domain declaration to ``frozenset[str]`` or ``None``.

    Shared by :attr:`cpnx.Transition.impacts_colors` (at assignment) and the
    ``colors=`` argument of :func:`trace_impact`, so both accept the same shapes and
    reject the same mistakes.

    * ``None`` → ``None`` ("unspecified" — the tracer treats it as *universal*).
    * A **bare ``str``** is rejected: ``"data"`` would iterate to
      ``{"d", "a", "t"}``. Pass ``{"data"}`` for a single colour.
    * Any other iterable of ``str`` → a ``frozenset`` of those names (an empty
      iterable yields an empty ``frozenset`` — a *narrowing* declaration, distinct
      from ``None``).

    Raises:
        TypeError: on a bare ``str``, a non-iterable, or a non-``str`` member.
    """
    if value is None:
        return None
    if isinstance(value, str):
        raise TypeError(
            f"{field} must be a set of colour names, not a bare string; "
            f"pass {{{value!r}}} to declare the single colour {value!r}."
        )
    if not isinstance(value, Iterable):
        raise TypeError(f"{field} must be None or an iterable of colour names (str); got {type(value).__name__}.")
    items = list(value)
    for item in items:
        if not isinstance(item, str):
            raise TypeError(f"{field} colour names must be str; got {type(item).__name__} ({item!r}).")
    return frozenset(items)


@dataclass(frozen=True)
class ImpactMap:
    """The forward blast radius of a transition within a (optional) colour domain.

    Attributes:
        origin: Name of the transition the trace started from (its *seed*).
        colors: The traced colour domain — a ``frozenset[str]`` of colour names, or
                ``None`` for a *universal* trace (no colour pruning).
        places: Names of every downstream [`Place`][cpnx.Place] reachable from
                ``origin`` within ``colors``.
        transitions: Names of every downstream [`Transition`][cpnx.Transition]
                reachable from ``origin`` within ``colors``. **Excludes** ``origin``
                itself (even if a cycle leads back to it).
        edges: The traced graph edges as ordered ``(from_name, to_name)`` pairs
                (place→transition and transition→place hops), de-duplicated with
                first-seen order preserved — handy for rendering or auditing the walk.
    """

    origin: str
    colors: frozenset[str] | None
    places: frozenset[str]
    transitions: frozenset[str]
    edges: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict:
        """Return a JSON-serialisable view (sorted for stable diffs / snapshots)."""
        return {
            "origin": self.origin,
            "colors": None if self.colors is None else sorted(self.colors),
            "places": sorted(self.places),
            "transitions": sorted(self.transitions),
            "edges": [list(edge) for edge in self.edges],
        }


def _in_domain(place_color_set: set[str] | None, traced: frozenset[str] | None) -> bool:
    """Whether a place carrying ``place_color_set`` is inside the ``traced`` domain.

    The cone-of-influence gate. Conservative by construction (see the module
    soundness note): a universal trace keeps everything, and an unconstrained place
    (``color_set is None``) is never pruned.
    """
    if traced is None:  # universal trace — nothing is pruned
        return True
    if place_color_set is None:  # place accepts any colour — cannot be excluded
        return True
    return not traced.isdisjoint(place_color_set)


def trace_impact(
    net: "PetriNet",
    transition_name: str,
    *,
    colors: object = None,
) -> ImpactMap:
    """Trace the forward blast radius of a transition over the net's static topology.

    Breadth-first from ``transition_name``'s **output** places, hopping
    place→(consuming transition)→(its output places) and pruning any place whose
    declared ``color_set`` is disjoint from the traced colour domain (see
    :func:`_in_domain`). Terminates on cycles (each transition is expanded once).

    The walk reads only the *structure* (`net.transitions`, `net.places`, each
    transition's input/output arc `place` references) under the engine lock, so it is
    safe to call on a live net and does not depend on the scheduler having started;
    the place→consumers routing is rebuilt fresh each call, so transitions added
    after an earlier trace are picked up.

    Note on precision: an **undeclared** seed (``impacts_colors is None`` and no
    ``colors=`` override) traces *universally*, so its radius bleeds through every
    shared [`ResourcePlace`][cpnx.ResourcePlace] into every transition that borrows the
    same permit pool — a resource self-loop is a legitimate downstream hop. That is the
    documented sound over-approximation, not a bug; narrow it by *declaring* the seed's
    colours (any set excluding ``"resource"`` prunes the pools, since a resource pool's
    ``color_set`` is ``{"resource"}``).

    Args:
        net: The [`PetriNet`][cpnx.PetriNet] to analyse.
        transition_name: The seed transition.
        colors: Colour domain to trace. ``None`` (default) uses the seed's
                [`impacts_colors`][cpnx.Transition] declaration (itself possibly
                ``None`` → universal). Pass an explicit set of colour names to
                override the declaration for this trace (accepts the same shapes as
                the field — a bare ``str`` raises; see :func:`coerce_color_domain`).

    Returns:
        An :class:`ImpactMap`.

    Raises:
        KeyError: if ``transition_name`` is not a transition in ``net``.
    """
    # Make resource-return arcs structural before the static walk, so a borrowed pool is
    # reachable (ADR 0009). Done here — not only in `PetriNet.trace_impact` — so the exported
    # free-function form is honest too. One-time and idempotent; must run outside the lock
    # (`_ensure_...` acquires the same non-reentrant lock).
    net._ensure_resource_returns_synthesized()
    override = coerce_color_domain(colors, field="trace_impact(colors=...)")

    with net._lock:
        transitions = net.transitions
        if transition_name not in transitions:
            known = ", ".join(sorted(transitions)) or "<none>"
            raise KeyError(f"trace_impact: no transition named {transition_name!r} in this net (known: {known}).")
        seed: "Transition" = transitions[transition_name]

        # Colour precision: explicit override wins; else the seed's declaration.
        traced: frozenset[str] | None = override if colors is not None else getattr(seed, "impacts_colors", None)

        # Rebuild the place -> consuming-transitions routing fresh (structure is the
        # source of truth; keeps the tracer independent of scheduler state).
        consumers: dict[str, list["Transition"]] = {}
        for t in transitions.values():
            for arc in t.inputs:
                consumers.setdefault(arc.place, []).append(t)

        places = net.places

        def gate(place_name: str) -> bool:
            place = places.get(place_name)
            return _in_domain(place.color_set if place is not None else None, traced)

        impacted_places: set[str] = set()
        impacted_transitions: set[str] = set()
        edges: list[tuple[str, str]] = []
        seen_edges: set[tuple[str, str]] = set()
        expanded: set[str] = {transition_name}  # never re-expand the seed (cycle safety)

        def add_edge(a: str, b: str) -> None:
            if (a, b) not in seen_edges:
                seen_edges.add((a, b))
                edges.append((a, b))

        frontier: deque[str] = deque()

        def visit_outputs(source_transition: "Transition") -> None:
            for out in source_transition.outputs:
                if not gate(out.place):
                    continue
                add_edge(source_transition.name, out.place)
                if out.place not in impacted_places:
                    impacted_places.add(out.place)
                    frontier.append(out.place)

        visit_outputs(seed)

        while frontier:
            place_name = frontier.popleft()
            for consumer in consumers.get(place_name, []):
                add_edge(place_name, consumer.name)
                if consumer.name != transition_name:
                    impacted_transitions.add(consumer.name)
                if consumer.name in expanded:
                    continue
                expanded.add(consumer.name)
                visit_outputs(consumer)

    return ImpactMap(
        origin=transition_name,
        colors=traced,
        places=frozenset(impacted_places),
        transitions=frozenset(impacted_transitions),
        edges=tuple(edges),
    )


def _selection_callables(transition: "Transition") -> Iterable[tuple[str, object]]:
    """Yield ``(role, callable)`` for every *enabling* inscription on a transition.

    These are the callables that decide firing — the ones the linter cares about
    (see :mod:`cpnx.linting`). The ``action`` is deliberately excluded: side effects
    are legitimate there.
    """
    if transition.guard is not None:
        yield "guard", transition.guard
    key = getattr(transition, "binding_priority_key", None)
    if key is not None:
        yield "binding_priority_key", key
    for i, arc in enumerate(transition.inputs):
        if getattr(arc, "key", None) is not None:
            yield f"inputs[{i}({arc.place})].key", arc.key
        if getattr(arc, "filter", None) is not None:
            yield f"inputs[{i}({arc.place})].filter", arc.filter


def risk_report(net: "PetriNet") -> dict:
    """Fuse the side-effect linter with blast-radius tracing into one report.

    The verification / debugging entry point. For every transition it lints each
    enabling inscription (guard, arc ``key``/``filter``, ``binding_priority_key``);
    for any transition that either **trips the linter** or carries an explicit
    :attr:`~cpnx.Transition.impacts_colors` declaration, it attaches that
    transition's :meth:`ImpactMap.to_dict`. The result answers, in one place, both
    "which transitions reach outside the decidable core?" (ADR 0007) and "how far
    downstream can each one's effect spread?" (this module).

    Returns:
        A JSON-serialisable ``dict``::

            {
              "findings": [
                {"transition": <name>,
                 "findings": [{"role": <str>, "category": <str>, "symbol": <str>}, ...]},
                ...
              ],
              "impact_maps": {<transition name>: <ImpactMap.to_dict()>, ...},
            }

        ``findings`` lists only transitions with at least one lint finding;
        ``impact_maps`` covers every transition that was linted-dirty **or**
        colour-annotated.
    """
    from cpnx.linting import lint_callable  # local import: avoid import-time coupling

    # Structural resource returns before tracing, so the exported free-function form matches
    # `PetriNet.risk_report` (ADR 0009). Outside the lock; idempotent.
    net._ensure_resource_returns_synthesized()

    with net._lock:
        transitions = list(net.transitions.values())

    findings_out: list[dict] = []
    impact_maps: dict[str, dict] = {}

    for t in transitions:
        t_findings: list[dict] = []
        for role, fn in _selection_callables(t):
            for finding in lint_callable(fn):
                t_findings.append({"role": role, "category": finding.category, "symbol": finding.symbol})

        declared = getattr(t, "impacts_colors", None) is not None
        if t_findings or declared:
            try:
                impact_maps[t.name] = trace_impact(net, t.name).to_dict()
            except KeyError:
                # The transition list was snapshotted under the lock, but each
                # trace re-acquires it: a concurrent removal between snapshot and
                # trace leaves a stale name. Skip it rather than aborting the whole
                # report — a live-mutation edge, not a bug in a static net.
                pass
        if t_findings:
            findings_out.append({"transition": t.name, "findings": t_findings})

    return {"findings": findings_out, "impact_maps": impact_maps}
