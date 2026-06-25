import json

from petriq.engine import PetriNet
from petriq.places import Place, ResourcePlace
from petriq.tokens import Token
from petriq.transitions import InputArc, OutputArc, Transition


class TestSnapshot:
    def test_snapshot_returns_dict(self):
        net = PetriNet()
        snap = net.snapshot()
        assert isinstance(snap, dict)
        assert "places" in snap

    def test_snapshot_includes_all_places(self):
        net = PetriNet()
        net.add_place(Place("p1"))
        net.add_place(Place("p2"))
        snap = net.snapshot()
        assert "p1" in snap["places"]
        assert "p2" in snap["places"]

    def test_snapshot_is_json_serializable(self):
        net = PetriNet()
        net.add_place(Place("input"))
        net.deposit("input", Token(payload={"key": "value", "num": 42}))
        snap = net.snapshot()
        serialized = json.dumps(snap)
        parsed = json.loads(serialized)
        assert "input" in parsed["places"]

    def test_snapshot_token_fields(self):
        net = PetriNet()
        net.add_place(Place("p"))
        t = Token(payload={"x": 1})
        net.deposit("p", t)

        snap = net.snapshot()
        token_data = snap["places"]["p"][0]
        assert token_data["id"] == t.id
        assert token_data["payload"] == {"x": 1}
        assert token_data["is_resource"] is False
        assert isinstance(token_data["created_at"], float)

    def test_snapshot_resource_token_flagged(self):
        net = PetriNet()
        net.add_place(ResourcePlace("gpu", capacity=2))
        snap = net.snapshot()
        assert all(t["is_resource"] for t in snap["places"]["gpu"])

    def test_snapshot_running_count(self):
        net = PetriNet()
        snap = net.snapshot()
        assert "running_count" in snap
        assert snap["running_count"] == 0

    def test_snapshot_empty_place(self):
        net = PetriNet()
        net.add_place(Place("empty"))
        snap = net.snapshot()
        assert snap["places"]["empty"] == []


class TestToDot:
    def test_dot_contains_digraph(self):
        net = PetriNet()
        assert "digraph PetriNet" in net.to_dot()

    def test_dot_includes_places_and_transitions(self):
        net = PetriNet()
        net.add_place(Place("source"))
        net.add_place(Place("sink"))
        net.add_transition(
            Transition(
                name="process",
                inputs=[InputArc("source")],
                outputs=[OutputArc("sink")],
                action=lambda t: t,
            )
        )

        dot = net.to_dot()
        assert '"source"' in dot
        assert '"sink"' in dot
        assert '"process"' in dot
        assert '"source" -> "process"' in dot
        assert '"process" -> "sink"' in dot

    def test_dot_place_shapes_are_circle(self):
        net = PetriNet()
        net.add_place(Place("p"))
        dot = net.to_dot()
        assert "shape=circle" in dot

    def test_dot_transition_shapes_are_box(self):
        net = PetriNet()
        net.add_place(Place("a"))
        net.add_place(Place("b"))
        net.add_transition(
            Transition("t", [InputArc("a")], [OutputArc("b")], action=lambda t: t)
        )
        dot = net.to_dot()
        assert "shape=box" in dot

    def test_dot_consume_all_label_present(self):
        net = PetriNet()
        net.add_place(Place("a"))
        net.add_place(Place("b"))
        net.add_transition(
            Transition(
                "t",
                [InputArc("a", consume_all=True)],
                [OutputArc("b")],
                action=lambda t: t,
            )
        )
        dot = net.to_dot()
        assert "consume_all" in dot

    def test_dot_is_string(self):
        net = PetriNet()
        assert isinstance(net.to_dot(), str)
