"""Smoke tests for the public petriq API surface (imported from package root)."""

import time

from petriq import (
    InputArc,
    OutputArc,
    PacedResourcePlace,
    PetriNet,
    Place,
    ResourcePlace,
    ThresholdPlace,
    Token,
    Transition,
)


class TestPublicImports:
    def test_all_symbols_importable(self):
        assert PetriNet
        assert Place
        assert ResourcePlace
        assert PacedResourcePlace
        assert ThresholdPlace
        assert Token
        assert Transition
        assert InputArc
        assert OutputArc

    def test_basic_pipeline_via_public_api(self):
        net = PetriNet(max_workers=2)
        net.add_place(Place("src"))
        net.add_place(Place("dst"))
        net.add_transition(
            Transition(
                name="copy",
                inputs=[InputArc("src")],
                outputs=[OutputArc("dst")],
                action=lambda tokens: tokens,
            )
        )
        net.deposit("src", Token(payload={"hello": "world"}))
        net.run(deadline=time.monotonic() + 2.0)

        assert len(net.places["dst"].tokens) == 1
        assert net.places["dst"].tokens[0].payload == {"hello": "world"}

    def test_resource_pipeline_via_public_api(self):
        net = PetriNet(max_workers=4)
        net.add_place(Place("jobs"))
        net.add_place(Place("done"))
        net.add_place(ResourcePlace("workers", capacity=2))

        net.add_transition(
            Transition(
                name="work",
                inputs=[InputArc("jobs"), InputArc("workers")],
                outputs=[OutputArc("done"), OutputArc("workers")],
                action=lambda tokens: [t for t in tokens if not t.is_resource],
            )
        )

        for _ in range(6):
            net.deposit("jobs", Token())

        net.run(deadline=time.monotonic() + 5.0)

        assert len(net.places["done"].tokens) == 6
        assert len(net.places["workers"].tokens) == 2  # resources returned
