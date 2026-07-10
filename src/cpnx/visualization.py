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


def to_dot(net: "PetriNet") -> str:
    """Render a `PetriNet`'s structure and current token counts as Graphviz DOT.

    Acquires the net's internal lock while reading. Places are drawn as circles
    labelled with their name and current token count (or, for
    [`SinkPlace`][cpnx.SinkPlace]s, the cumulative absorbed count). Transitions
    are drawn as boxes. Each input arc is drawn as an edge from its place to the
    transition, labelled with its `count` and, when applicable, `consume_all`
    and/or `settle=<settle_secs>s`. Each output arc is drawn as an edge from the
    transition to its place, labelled with its `count`.

    Args:
        net: The [`PetriNet`][cpnx.PetriNet] instance to export.

    Returns:
        A string containing the full `digraph PetriNet { ... }` DOT source,
        suitable for rendering with Graphviz (e.g. `dot -Tpng`).
    """
    from cpnx.places import SinkPlace

    with net._lock:
        lines = ["digraph PetriNet {", "  rankdir=LR;"]

        # Nodes: Places
        for name, place in net.places.items():
            if isinstance(place, SinkPlace):
                token_count = place.stats()["absorbed"]
            else:
                token_count = len(place)
            label = f"{name}\\n({token_count})"
            lines.append(f'  "{name}" [shape=circle, label="{label}"];')

        # Nodes: Transitions
        for name in net.transitions.keys():
            lines.append(f'  "{name}" [shape=box, label="{name}"];')

        # Edges
        for name, trans in net.transitions.items():
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

        lines.append("}")
        return "\n".join(lines)
