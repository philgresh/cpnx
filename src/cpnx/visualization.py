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
    """
    from cpnx.places import ResourcePlace, SinkPlace

    # Compute the blast radius *before* taking the lock: trace_impact acquires the
    # same (non-reentrant) lock itself.
    impact = None
    if highlight_impact_from is not None:
        from cpnx.analysis import trace_impact

        impact = trace_impact(net, highlight_impact_from)

    with net._lock:
        places = net.places
        transitions = net.transitions

        # Places produced by at least one output arc — anything absent is arc-sourceless.
        produced = {arc.place for t in transitions.values() for arc in t.outputs}

        def _is_resource(place_name: str) -> bool:
            return isinstance(places.get(place_name), ResourcePlace)

        # Resource borrows with no matching resource output arc → engine returns them
        # implicitly; we draw that as a dashed edge so the picture stays honest.
        implicit_returns: list[tuple[str, str]] = []
        for tname, trans in transitions.items():
            res_in = {a.place for a in trans.inputs if _is_resource(a.place)}
            res_out = {a.place for a in trans.outputs if _is_resource(a.place)}
            implicit_returns.extend((tname, pool) for pool in sorted(res_in - res_out))

        error_place = net.error_place
        lines = ["digraph PetriNet {", "  rankdir=LR;"]

        # Nodes: Places
        has_source = has_sink = has_error = False
        for name, place in places.items():
            is_resource = isinstance(place, ResourcePlace)
            is_sink = isinstance(place, SinkPlace)
            is_error = bool(error_place) and name == error_place
            is_drain = is_sink or is_error  # terminal / dead-letter — tokens fall in and stay
            count = place.stats()["absorbed"] if is_sink else len(place)
            # De-emphasise counts (the focus is a token's *paths*): show one only when a
            # place actually holds tokens — e.g. an initial resource marking.
            label = name if count == 0 else f"{name}\\n({count})"
            shape = "doublecircle" if is_resource else "circle"
            attrs = [f"shape={shape}", f'label="{label}"']

            fill: str | None = None
            style: str | None = None
            if impact is not None and name in impact.places:
                fill, style = _IMPACT_FILL, "filled"  # blast-radius overlay wins (solid)
            elif is_resource:
                fill, style = _RESOURCE_FILL, "filled"
            elif is_drain:  # inverted-cone "well": a radial gradient reads as an inset shadow
                fill = _ERROR_GRADIENT if is_error else _SINK_GRADIENT
                style = "radial"
                has_error = has_error or is_error
                has_sink = has_sink or not is_error
            elif name not in produced:  # arc in-degree 0 → external source
                style = "dashed"
                has_source = True
            if fill is not None:
                attrs.append(f'fillcolor="{fill}"')
            if style is not None:
                attrs.append(f'style="{style}"')
            lines.append(f'  "{name}" [{", ".join(attrs)}];')

        # Nodes: Transitions
        for name in transitions.keys():
            attrs = f'shape=box, label="{name}"'
            if impact is not None:
                if name == impact.origin:
                    attrs += f', style=filled, fillcolor="{_IMPACT_ORIGIN_FILL}", penwidth=2'
                elif name in impact.transitions:
                    attrs += f', style=filled, fillcolor="{_IMPACT_FILL}"'
            lines.append(f'  "{name}" [{attrs}];')

        # Edges
        for name, trans in transitions.items():
            # Inputs: Place -> Transition
            for arc in trans.inputs:
                label_parts = [f"count={arc.count}"]
                if arc.consume_all:
                    label_parts.append("consume_all")
                if arc.settle_secs > 0.0:
                    label_parts.append(f"settle={arc.settle_secs}s")
                label = ", ".join(label_parts)
                lines.append(f'  "{arc.place}" -> "{name}" [label="{label}"];')

            # Outputs: Transition -> Place
            for out_arc in trans.outputs:
                label = f"count={out_arc.count}"
                lines.append(f'  "{name}" -> "{out_arc.place}" [label="{label}"];')

        # Implicit resource returns (drawn only when the model omits the self-loop).
        for tname, pool in implicit_returns:
            lines.append(f'  "{tname}" -> "{pool}" [label="return (implicit)", style=dashed];')

        # Dead-letter paths: a transition with a finite `max_retries` routes a failed data
        # token to the error place *off-arc* (engine dead-lettering), which is why the error
        # place otherwise looks isolated. Draw it dashed and `constraint=false` so the token's
        # valid failure path is visible without warping the main flow layout. Skip any
        # transition that already has a real output arc to the error place.
        deadletter_drawn = False
        if error_place and error_place in places:
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
                deadletter_drawn = True

        # Legend — only the encodings actually present, so trivial nets stay clean.
        legend = []
        if any(isinstance(p, ResourcePlace) for p in places.values()):
            legend.append("double circle = resource pool")
        if has_source:
            legend.append("dashed circle = external source")
        if has_sink:
            legend.append("well (radial fill) = sink / terminal")
        if has_error:
            legend.append("salmon well = error place (dead-letter target)")
        if deadletter_drawn:
            legend.append("dashed red = dead-letter path")
        if implicit_returns:
            legend.append("dashed edge = implicit permit return")
        if impact is not None:
            legend.append(f"pink = blast radius of {impact.origin}")
        if legend:
            lines.append(f'  label="{"  ·  ".join(legend)}"; labelloc="b"; fontsize=9; fontcolor="#666666";')

        lines.append("}")
        return "\n".join(lines)
