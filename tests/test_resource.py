import time

from petriq.engine import PetriNet
from petriq.places import PacedResourcePlace, Place, ResourcePlace
from petriq.tokens import Token
from petriq.transitions import InputArc, OutputArc, Transition


def test_resource_place_prefill():
    place = ResourcePlace("res", capacity=3)
    assert len(place.tokens) == 3
    assert all(t.is_resource for t in place.tokens)


def test_paced_resource_place_cooldown():
    place = PacedResourcePlace("pace", capacity=1, pacing_secs=0.1)
    # Available immediately
    assert place.can_retrieve(1)
    tokens = place.retrieve(1)
    assert len(tokens) == 1
    assert tokens[0].is_resource

    # Deposit again, must trigger pacing cooldown
    place.deposit(tokens[0])
    assert not place.can_retrieve(1)

    time.sleep(0.12)
    assert place.can_retrieve(1)


def test_paced_transitions():
    net = PetriNet(max_workers=2)
    net.add_place(Place("input"))
    net.add_place(Place("output"))
    # Capacity 1, pacing 0.1s
    net.add_place(PacedResourcePlace("resource", capacity=1, pacing_secs=0.1))

    def action(tokens):
        data = [t for t in tokens if not t.is_resource][0]
        return [data]

    net.add_transition(
        Transition(
            name="t1",
            inputs=[InputArc("input"), InputArc("resource")],
            outputs=[OutputArc("output"), OutputArc("resource")],
            action=action,
        )
    )

    # Deposit 3 data tokens
    for _ in range(3):
        net.deposit("input", Token())

    start = time.monotonic()
    net.run(deadline=start + 2.0)
    elapsed = time.monotonic() - start

    # 3 transitions with 1 paced resource token must take at least 2 * pacing_secs (0.2s)
    # Let's verify we got all outputs
    assert len(net.places["output"].tokens) == 3
    assert elapsed >= 0.18  # With 20ms tolerance
