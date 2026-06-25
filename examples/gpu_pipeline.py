"""examples/gpu_pipeline.py — GPU slot management with petriq."""

import time
from petriq import InputArc, OutputArc, PetriNet, Place, ResourcePlace, Token, Transition


def train_model(tokens: list[Token]) -> list[Token]:
    data = tokens[0]
    time.sleep(0.5)  # simulate GPU work
    return [data.evolve(payload_updates={"trained": True})]


net = PetriNet(max_workers=4)
net.add_place(Place("raw_data"))
net.add_place(Place("trained_models"))
net.add_place(ResourcePlace("gpu_slots", capacity=2))

net.add_transition(Transition(
    name="train",
    inputs=[InputArc("raw_data"), InputArc("gpu_slots")],
    outputs=[OutputArc("trained_models"), OutputArc("gpu_slots")],
    action=train_model,
))

for i in range(10):
    net.deposit("raw_data", Token(payload={"model_id": i}))

net.run(deadline=time.monotonic() + 30)

print(f"Trained: {len(net.places['trained_models'].tokens)}")
print(f"GPU slots returned: {len(net.places['gpu_slots'].tokens)}")
