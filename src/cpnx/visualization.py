from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cpnx.engine import PetriNet


def snapshot(net: "PetriNet") -> dict[str, Any]:
    """Capture a JSON-serialisable snapshot of a `PetriNet`'s current marking.

    Acquires the net's internal lock while reading, so the snapshot reflects a
    consistent point-in-time view. For each place, records each token's `id`,
    `payload` (as a plain `dict`), `created_at`, and `color`. For a
    [`SinkPlace`][cpnx.SinkPlace], the place's entry is instead a dict with a
    `"tokens"` list (from its ring buffer, if any) and an `"absorbed"` count
    (the cumulative number of tokens ever absorbed).

    Args:
        net: The [`PetriNet`][cpnx.PetriNet] instance to snapshot.

    Returns:
        A dict with two keys: `"places"`, mapping each place name to either a
        list of token dicts, or (for sink places) a dict with `"tokens"` and
        `"absorbed"`; and `"running_count"`, the number of transitions currently
        mid-firing.
    """
    from cpnx.places import SinkPlace

    with net._lock:
        places_snapshot: dict[str, Any] = {}
        for name, place in net.places.items():
            tokens_list: list[dict[str, Any]] = []
            for t in place.tokens:
                tokens_list.append(
                    {
                        "id": t.id,
                        "payload": dict(t.payload),
                        "created_at": t.created_at,
                        "color": t.color,
                    }
                )
            if isinstance(place, SinkPlace):
                places_snapshot[name] = {
                    "tokens": tokens_list,
                    "absorbed": place.stats()["absorbed"],
                }
            else:
                places_snapshot[name] = tokens_list

        return {"places": places_snapshot, "running_count": net._running_count}


#: Fill colour for a place/transition inside a highlighted blast radius.
_IMPACT_FILL = "#ffd9d9"
#: Fill colour for the *seed* transition a blast radius was traced from.
_IMPACT_ORIGIN_FILL = "#ff8080"
#: Fill colour for a resource-permit pool (`ResourcePlace`/`PacedResourcePlace`).
_RESOURCE_FILL = "#dbe9ff"
#: Radial-gradient fill giving a sink an "inset well" look — light centre, dark rim,
#: so a terminal place reads as an inverted cone tokens fall into (a drain).
_SINK_GRADIENT = "#f5f5f5:#9aa0a6"
#: The same well, tinted salmon for the net's `error_place` dead-letter drain.
_ERROR_GRADIENT = "#ffe0e0:#e08a8a"
#: Edge colour for engine-routed dead-letter paths — a pale, quiet red (tied to the
#: error-place rim tint) so these off-arc side channels don't compete with the flow.
_DEADLETTER_COLOR = "#e08a8a"


def to_dot(net: "PetriNet", *, highlight_impact_from: str | None = None) -> str:
    """Render a `PetriNet`'s structure and current token counts as Graphviz DOT.

    The rendering is built around a token's **valid paths**, so places carry a count
    only when they actually hold tokens (a bare `name` otherwise). Transitions are boxes;
    places are circles, with these role-distinct renderings — several because their tokens
    arrive or leave **off-arc** and would otherwise mislead:

    - a **resource pool** ([`ResourcePlace`][cpnx.ResourcePlace] /
      [`PacedResourcePlace`][cpnx.PacedResourcePlace]) is a **double circle** filled
      `#dbe9ff` — seeded with permits at construction (the CPN initial marking) and
      cycling them via resource arcs;
    - a **sink / terminal** ([`SinkPlace`][cpnx.SinkPlace]) is a **radial-gradient well**
      (light centre, dark rim) — an inset-shadow "inverted cone" a token falls into and
      is absorbed, so it does not read as an ordinary holding place;
    - the net's **error place** is the same well tinted salmon — it is fed off-arc by the
      engine's dead-lettering, not by a customer, so it is a drain, not a source;
    - an **external source** — a place **no transition produces into** (arc in-degree 0,
      excluding pools, sinks, and the error place) — has a **dashed** border; its tokens
      can only come from an external `deposit(...)` (or an initial marking).

    Each input arc is an edge from its place to the transition, labelled with its `count`
    and, when applicable, `consume_all` / `settle=<settle_secs>s`; each output arc an edge
    to its place. Two *off-arc* token paths are drawn dashed so no place looks isolated
    and no path is hidden: a **dead-letter** side channel (pale red, thin, unlabelled —
    named once in the legend, `constraint=false` so it does not warp the layout) from every
    transition with a finite `max_retries` to the error place — the engine's failure
    routing; and a **`return (implicit)`** edge from any transition that borrows a resource
    permit **without** a matching output arc back to its pool (i.e. relying on the engine's
    implicit leftover-return rather than a structural self-loop).

    **Terminal places** — those with incoming tokens but **no outgoing arc** (nothing consumes
    from them: a `SinkPlace`, or the dead-letter bin whose only edges are the off-arc,
    rank-less dead-letter channels) — are pinned to the sink rank via `{ rank=sink; ... }`, so
    the flow reads left-to-right and endpoints land at the far right instead of floating at
    rank 0. Inferred from the arc structure, so no place name is special-cased.

    Args:
        net: The [`PetriNet`][cpnx.PetriNet] instance to export.
        highlight_impact_from: If given, the name of a transition whose forward
            colour-domain blast radius (see [`trace_impact`][cpnx.trace_impact]) is
            shaded: every impacted place and transition is filled `#ffd9d9`, and the
            seed transition is filled `#ff8080` with a heavier border. `None`
            (default) adds no blast-radius shading (resource/source styling still applies).

    Returns:
        A string containing the full `digraph PetriNet { ... }` DOT source,
        suitable for rendering with Graphviz (e.g. `dot -Tpng`).

    Raises:
        KeyError: if `highlight_impact_from` names no transition in `net`. The trace
            runs before any DOT is built, so nothing is emitted on failure.
    """
    from cpnx.places import ResourcePlace

    # Compute the blast radius *before* taking the lock: trace_impact acquires the
    # same (non-reentrant) lock itself.
    impact = None
    if highlight_impact_from is not None:
        from cpnx.analysis import trace_impact

        impact = trace_impact(net, highlight_impact_from)

    with net._lock:
        places = net.places
        transitions = net.transitions
        error_place = net.error_place
        # Places produced by at least one output arc — anything absent is arc-sourceless.
        produced = _produced_places(transitions)

        lines = ["digraph PetriNet {", "  rankdir=LR;"]

        # Nodes: Places (each contributes at most one legend "role").
        roles: set[str] = set()
        for name, place in places.items():
            line, role = _place_node_line(name, place, impact=impact, produced=produced, error_place=error_place)
            lines.append(line)
            if role is not None:
                roles.add(role)

        # Nodes: Transitions
        for name in transitions:
            lines.append(_transition_node_line(name, impact))

        # Edges: input/output arcs, in transition order.
        for name, trans in transitions.items():
            lines.extend(_arc_edge_lines(name, trans))

        # Off-arc token paths (dashed) so no place looks isolated and no path is hidden.
        implicit = _implicit_return_lines(transitions, places)
        lines.extend(implicit)
        deadletter, deadletter_drawn = _dead_letter_lines(transitions, error_place, places)
        lines.extend(deadletter)

        # Pin terminal places (incoming tokens, no outgoing arc) to the sink rank, so a place
        # like a dead-letter bin — whose only edges are off-arc (constraint=false) and thus
        # rank-less — still lands at the far right where the flow ends, not floating left.
        terminal_rank = _terminal_rank_line(_terminal_places(places, transitions, produced, error_place))
        if terminal_rank is not None:
            lines.append(terminal_rank)

        legend = _legend_line(
            has_resource=any(isinstance(p, ResourcePlace) for p in places.values()),
            roles=roles,
            deadletter_drawn=deadletter_drawn,
            has_implicit=bool(implicit),
            impact=impact,
        )
        if legend is not None:
            lines.append(legend)

        lines.append("}")
        return "\n".join(lines)


def _produced_places(transitions: dict) -> set[str]:
    """Names of places produced by at least one output arc (in-degree > 0)."""
    return {arc.place for t in transitions.values() for arc in t.outputs}


def _consumed_places(transitions: dict) -> set[str]:
    """Names of places consumed by at least one input arc (out-degree > 0)."""
    return {arc.place for t in transitions.values() for arc in t.inputs}


def _terminal_places(places: dict, transitions: dict, produced: set[str], error_place: str | None) -> list[str]:
    """Places where the flow ends: they receive tokens but no transition consumes from them.

    Inferred purely from the arc structure (no per-net hard-coding): a place with **no
    outgoing arc** (`out-degree 0`) that still receives tokens — produced by an output arc, or
    the dead-letter target — is terminal, so it belongs at the sink rank. Resource pools
    (always consumed by a borrow) and external sources (never produced) are excluded.
    """
    consumed = _consumed_places(transitions)
    return [name for name in places if name not in consumed and (name in produced or name == error_place)]


def _terminal_rank_line(terminals: list[str]) -> str | None:
    """A `{ rank=sink; ... }` group pinning terminal places to the last rank (far right under
    `rankdir=LR`), or `None` when the net has none."""
    if not terminals:
        return None
    nodes = " ".join(f'"{name}";' for name in terminals)
    return f"  {{ rank=sink; {nodes} }}"


def _place_fill_style(
    name: str, *, is_resource: bool, is_sink: bool, is_error: bool, impact: Any, produced: set[str]
) -> tuple[str | None, str | None, str | None]:
    """Resolve a place's `(fillcolor, style, legend-role)` in priority order.

    `role` is one of `None`, `"source"`, `"sink"`, `"error"` — the legend keys whose
    presence depends on a place actually appearing. A blast-radius overlay wins over
    every structural role; a resource pool over a drain; error over sink.
    """
    if impact is not None and name in impact.places:
        return _IMPACT_FILL, "filled", None  # blast-radius overlay wins (solid)
    if is_resource:
        return _RESOURCE_FILL, "filled", None
    if is_error:  # inverted-cone "well": a radial gradient reads as an inset shadow
        return _ERROR_GRADIENT, "radial", "error"
    if is_sink:
        return _SINK_GRADIENT, "radial", "sink"
    if name not in produced:  # arc in-degree 0 → external source
        return None, "dashed", "source"
    return None, None, None


def _place_node_line(
    name: str, place: Any, *, impact: Any, produced: set[str], error_place: str | None
) -> tuple[str, str | None]:
    """Render one place node as a DOT line; return `(line, legend-role-or-None)`."""
    from cpnx.places import ResourcePlace, SinkPlace

    is_resource = isinstance(place, ResourcePlace)
    is_sink = isinstance(place, SinkPlace)
    is_error = bool(error_place) and name == error_place
    count = place.stats()["absorbed"] if is_sink else len(place)
    # De-emphasise counts (the focus is a token's *paths*): show one only when a
    # place actually holds tokens — e.g. an initial resource marking.
    label = name if count == 0 else f"{name}\\n({count})"
    shape = "doublecircle" if is_resource else "circle"
    attrs = [f"shape={shape}", f'label="{label}"']

    fill, style, role = _place_fill_style(
        name,
        is_resource=is_resource,
        is_sink=is_sink,
        is_error=is_error,
        impact=impact,
        produced=produced,
    )
    if fill is not None:
        attrs.append(f'fillcolor="{fill}"')
    if style is not None:
        attrs.append(f'style="{style}"')
    return f'  "{name}" [{", ".join(attrs)}];', role


def _transition_node_line(name: str, impact: Any) -> str:
    """Render one transition node, shaded if it is the blast-radius seed or a member."""
    attrs = f'shape=box, label="{name}"'
    if impact is not None:
        if name == impact.origin:
            attrs += f', style=filled, fillcolor="{_IMPACT_ORIGIN_FILL}", penwidth=2'
        elif name in impact.transitions:
            attrs += f', style=filled, fillcolor="{_IMPACT_FILL}"'
    return f'  "{name}" [{attrs}];'


def _arc_edge_lines(name: str, trans: Any) -> list[str]:
    """Input (place→transition) then output (transition→place) edges for one transition."""
    lines = []
    for arc in trans.inputs:
        label_parts = [f"count={arc.count}"]
        if arc.consume_all:
            label_parts.append("consume_all")
        if arc.settle_secs > 0.0:
            label_parts.append(f"settle={arc.settle_secs}s")
        lines.append(f'  "{arc.place}" -> "{name}" [label="{", ".join(label_parts)}"];')
    for out_arc in trans.outputs:
        lines.append(f'  "{name}" -> "{out_arc.place}" [label="count={out_arc.count}"];')
    return lines


def _implicit_return_lines(transitions: dict, places: dict) -> list[str]:
    """Dashed `return (implicit)` edges — a resource borrow with no matching output arc,
    so the engine returns the permit implicitly (drawn only when the self-loop is omitted)."""
    from cpnx.places import ResourcePlace

    pools = {n for n, p in places.items() if isinstance(p, ResourcePlace)}
    lines = []
    for tname, trans in transitions.items():
        res_in = {a.place for a in trans.inputs if a.place in pools}
        res_out = {a.place for a in trans.outputs if a.place in pools}
        for pool in sorted(res_in - res_out):
            lines.append(f'  "{tname}" -> "{pool}" [label="return (implicit)", style=dashed];')
    return lines


def _dead_letter_lines(transitions: dict, error_place: str | None, places: dict) -> tuple[list[str], bool]:
    """Dashed, `constraint=false` dead-letter side channels from finite-`max_retries`
    transitions to the error place (engine failure routing), so it does not look isolated.

    Returns `(lines, drawn)`; `drawn` gates the legend entry. Skips any transition that
    already has a real output arc to the error place.
    """
    if not (error_place and error_place in places):
        return [], False
    lines = []
    for tname, trans in transitions.items():
        if trans.max_retries is None:  # infinite retry → never dead-letters
            continue
        if any(a.place == error_place for a in trans.outputs):  # already a real arc
            continue
        # Unlabelled, thin, pale: a quiet side channel. The legend names it once,
        # so we don't repeat "dead-letter" on every one of these edges.
        lines.append(
            f'  "{tname}" -> "{error_place}" [style=dashed, color="{_DEADLETTER_COLOR}", '
            f"penwidth=0.6, arrowsize=0.7, constraint=false];"
        )
    return lines, bool(lines)


def _legend_line(
    *, has_resource: bool, roles: set[str], deadletter_drawn: bool, has_implicit: bool, impact: Any
) -> str | None:
    """Assemble the bottom legend from only the encodings actually present (or `None`)."""
    legend = []
    if has_resource:
        legend.append("double circle = resource pool")
    if "source" in roles:
        legend.append("dashed circle = external source")
    if "sink" in roles:
        legend.append("well (radial fill) = sink / terminal")
    if "error" in roles:
        legend.append("salmon well = error place (dead-letter target)")
    if deadletter_drawn:
        legend.append("dashed red = dead-letter path")
    if has_implicit:
        legend.append("dashed edge = implicit permit return (auto_return_resources=False)")
    if impact is not None:
        legend.append(f"pink = blast radius of {impact.origin}")
    if not legend:
        return None
    return f'  label="{"  ·  ".join(legend)}"; labelloc="b"; fontsize=9; fontcolor="#666666";'
