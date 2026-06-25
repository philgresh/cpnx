from typing import Any


def snapshot(net: Any) -> dict:
    with net._lock:
        places_snapshot = {}
        for name, place in net.places.items():
            tokens_list = []
            for t in place.tokens:
                tokens_list.append(
                    {
                        "id": t.id,
                        "payload": t.payload,
                        "created_at": t.created_at,
                        "is_resource": t.is_resource,
                    }
                )
            places_snapshot[name] = tokens_list

        return {"places": places_snapshot, "running_count": net._running_count}


def to_dot(net: Any) -> str:
    with net._lock:
        lines = ["digraph PetriNet {", "  rankdir=LR;"]

        # Nodes: Places
        for name, place in net.places.items():
            token_count = len(place.tokens)
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
            for arc in trans.outputs:
                label = f"count={arc.count}"
                lines.append(f'  "{name}" -> "{arc.place}" [label="{label}"];')

        lines.append("}")
        return "\n".join(lines)
