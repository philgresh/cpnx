import json

from petriq.engine import PetriNet
from petriq.places import Place, ResourcePlace
from petriq.tokens import Token
from petriq.transitions import InputArc, OutputArc, Transition


def test_visualization_and_snapshot():
    net = PetriNet(max_workers=2)
    net.add_place(Place("input"))
    net.add_place(ResourcePlace("gpu", capacity=2))

    net.add_transition(
        Transition(
            name="train",
            inputs=[InputArc("input"), InputArc("gpu")],
            outputs=[OutputArc("gpu")],
            action=lambda tokens: [],
        )
    )

    net.deposit("input", Token(payload={"test": "data"}))

    # 1. Snapshot test
    snap = net.snapshot()
    assert isinstance(snap, dict)
    assert "places" in snap
    assert "input" in snap["places"]
    assert "gpu" in snap["places"]

    # Assert JSON serializability
    serialized = json.dumps(snap)
    assert isinstance(serialized, str)

    # 2. DOT string test
    dot_str = net.to_dot()
    assert isinstance(dot_str, str)
    assert "digraph PetriNet" in dot_str
    assert '"input"' in dot_str
    assert '"gpu"' in dot_str
    assert '"train"' in dot_str
    assert '"input" -> "train"' in dot_str
    assert '"gpu" -> "train"' in dot_str
    assert '"train" -> "gpu"' in dot_str
