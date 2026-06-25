"""examples/api_rate_limit.py — Rate-limited API with pacing."""

import time
from petriq import InputArc, OutputArc, PacedResourcePlace, PetriNet, Place, Token, Transition


def call_api(tokens: list[Token]) -> list[Token]:
    data = tokens[0]
    time.sleep(0.05)  # Simulate network latency
    data.payload["response"] = {"status": "ok"}
    return [data]


net = PetriNet(max_workers=10)
net.add_place(Place("requests"))
net.add_place(Place("responses"))
net.add_place(PacedResourcePlace("api_tokens", capacity=3, pacing_secs=0.2))

net.add_transition(Transition(
    name="api_call",
    inputs=[InputArc("requests"), InputArc("api_tokens")],
    outputs=[OutputArc("responses"), OutputArc("api_tokens")],
    action=call_api,
))

# Deposit 6 requests
for i in range(6):
    net.deposit("requests", Token(payload={"req_id": i}))

start = time.monotonic()
net.run(deadline=start + 5)
elapsed = time.monotonic() - start

print(f"Processed: {len(net.places['responses'].tokens)} responses in {elapsed:.2f} seconds")
print(f"API tokens returned: {len(net.places['api_tokens'].tokens)}")
