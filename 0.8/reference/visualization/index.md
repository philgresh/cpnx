# Visualization

Introspect a running net or export it to Graphviz DOT.

## cpnx.snapshot

```
snapshot(net: PetriNet) -> dict[str, Any]
```

Capture a JSON-serialisable snapshot of a `PetriNet`'s current marking.

Acquires the net's internal lock while reading, so the snapshot reflects a consistent point-in-time view. For each place, records each token's `id`, `payload` (as a plain `dict`), `created_at`, and `color`. For a SinkPlace, the place's entry is instead a dict with a `"tokens"` list (from its ring buffer, if any) and an `"absorbed"` count (the cumulative number of tokens ever absorbed).

Parameters:

| Name  | Type       | Description                        | Default    |
| ----- | ---------- | ---------------------------------- | ---------- |
| `net` | `PetriNet` | The PetriNet instance to snapshot. | *required* |

Returns:

| Type             | Description                                                          |
| ---------------- | -------------------------------------------------------------------- |
| `dict[str, Any]` | A dict with two keys: "places", mapping each place name to either a  |
| `dict[str, Any]` | list of token dicts, or (for sink places) a dict with "tokens" and   |
| `dict[str, Any]` | "absorbed"; and "running_count", the number of transitions currently |
| `dict[str, Any]` | mid-firing.                                                          |

Source code in `src/cpnx/visualization.py`

```
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
```

## cpnx.to_dot

```
to_dot(
    net: PetriNet,
    *,
    highlight_impact_from: str | None = None,
) -> str
```

Render a `PetriNet`'s structure and current token counts as Graphviz DOT.

The rendering is built around a token's **valid paths**, so places carry a count only when they actually hold tokens (a bare `name` otherwise). Transitions are boxes; places are circles, with these role-distinct renderings — several because their tokens arrive or leave **off-arc** and would otherwise mislead:

- a **resource pool** (ResourcePlace / PacedResourcePlace) is a **double circle** filled `#dbe9ff` — seeded with permits at construction (the CPN initial marking) and cycling them via resource arcs;
- a **sink / terminal** (SinkPlace) is a **radial-gradient well** (light centre, dark rim) — an inset-shadow "inverted cone" a token falls into and is absorbed, so it does not read as an ordinary holding place;
- the net's **error place** is the same well tinted salmon — it is fed off-arc by the engine's dead-lettering, not by a customer, so it is a drain, not a source;
- an **external source** — a place **no transition produces into** (arc in-degree 0, excluding pools, sinks, and the error place) — has a **dashed** border; its tokens can only come from an external `deposit(...)` (or an initial marking).

Each input arc is an edge from its place to the transition, labelled with its `count` and, when applicable, `consume_all` / `settle=<settle_secs>s`; each output arc an edge to its place. Two *off-arc* token paths are drawn dashed so no place looks isolated and no path is hidden: a **dead-letter** side channel (pale red, thin, unlabelled — named once in the legend; drawn `constraint=true, weight=0` so it ranks the error place downstream of its feeders and fans in cleanly, yet exerts no pull that would warp the main flow) from every transition with a finite `max_retries` to the error place — the engine's failure routing; and a **`return (implicit)`** edge from any transition that borrows a resource permit **without** a matching output arc back to its pool (i.e. relying on the engine's implicit leftover-return rather than a structural self-loop).

**Terminal places** — those with incoming tokens but **no outgoing arc** (nothing consumes from them: a `SinkPlace`, or the dead-letter bin whose only edges are the off-arc, rank-less dead-letter channels) — are pinned to the sink rank via `{ rank=sink; ... }`, so the flow reads left-to-right and endpoints land at the far right instead of floating at rank 0. Inferred from the arc structure, so no place name is special-cased.

Parameters:

| Name                    | Type       | Description                      | Default                                                                                                                                                                                                                                                                                                                |
| ----------------------- | ---------- | -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `net`                   | `PetriNet` | The PetriNet instance to export. | *required*                                                                                                                                                                                                                                                                                                             |
| `highlight_impact_from` | \`str      | None\`                           | If given, the name of a transition whose forward colour-domain blast radius (see trace_impact) is shaded: every impacted place and transition is filled #ffd9d9, and the seed transition is filled #ff8080 with a heavier border. None (default) adds no blast-radius shading (resource/source styling still applies). |

Returns:

| Type  | Description                                                       |
| ----- | ----------------------------------------------------------------- |
| `str` | A string containing the full digraph PetriNet { ... } DOT source, |
| `str` | suitable for rendering with Graphviz (e.g. dot -Tpng).            |

Raises:

| Type       | Description                                                                                                                    |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `KeyError` | if highlight_impact_from names no transition in net. The trace runs before any DOT is built, so nothing is emitted on failure. |

Source code in `src/cpnx/visualization.py`

```
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
    named once in the legend; drawn `constraint=true, weight=0` so it ranks the error place
    downstream of its feeders and fans in cleanly, yet exerts no pull that would warp the main
    flow) from every transition with a finite `max_retries` to the error place — the engine's
    failure routing; and a **`return (implicit)`** edge from any transition that borrows a
    resource permit **without** a matching output arc back to its pool (i.e. relying on the
    engine's implicit leftover-return rather than a structural self-loop).

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

    # Make resource-return arcs structural before drawing, so the graph is honest even on a
    # never-run net (ADR 0009). Done here — not only in `PetriNet.to_dot` — so the exported
    # free-function form matches. One-time, idempotent, and outside the lock (`_ensure_...`
    # acquires the same non-reentrant lock).
    net._ensure_resource_returns_synthesized()

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

        # Pin terminal places (incoming tokens, no outgoing arc) to the sink rank, so every
        # endpoint lands at the far right where the flow ends. The `constraint=true`
        # dead-letter edges already rank the bin downstream of its feeders, but this guarantees
        # terminal→far-right universally — e.g. a bin fed only by an early transition, which
        # those edges alone would otherwise park mid-graph.
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
```

## Highlighting a blast radius

Both to_dot and PetriNet.to_dot accept an optional `highlight_impact_from=<transition name>` argument. When given, the transition's forward colour-domain blast radius (see trace_impact) is shaded into the rendering:

| Element                         | Fill                                     | Meaning                                   |
| ------------------------------- | ---------------------------------------- | ----------------------------------------- |
| Impacted places and transitions | `#ffd9d9` (light red)                    | reachable within the traced colour domain |
| The seed transition (`origin`)  | `#ff8080` (heavier red) + thicker border | the transition the trace started from     |

`highlight_impact_from=None` (the default) leaves the rendering exactly as it was — no styling is added. See [ADR 0008](https://github.com/philgresh/cpnx/blob/main/docs/adr/0008-color-domain-impact-analysis.md) for the underlying impact-analysis model.
