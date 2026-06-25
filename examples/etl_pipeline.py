"""examples/etl_pipeline.py — Multi-stage ETL pipeline using petriq."""

import time
from petriq import InputArc, OutputArc, PetriNet, Place, ResourcePlace, ThresholdPlace, Token, Transition


# Stage 1: Transform action (takes 3 extracted tokens, batches them, produces 1 batch token)
def transform_batch(tokens: list[Token]) -> list[Token]:
    payloads = [t.payload for t in tokens]
    print(f"[Transform] Processing batch of {len(tokens)} items: {payloads}")
    time.sleep(0.1)
    batch_token = Token(payload={"batch": payloads, "transformed": True})
    return [batch_token]


# Stage 2: Load action (takes 1 batch token and 1 DB connection token, loads it)
def load_db(tokens: list[Token]) -> list[Token]:
    batch = [t for t in tokens if not t.is_resource][0]
    print(f"[Load] Loading batch into Database: {batch.payload['batch']}")
    time.sleep(0.1)
    loaded_token = Token(payload={"batch": batch.payload["batch"], "loaded": True})
    return [loaded_token]


# Initialize engine
net = PetriNet(max_workers=4)

# Places
net.add_place(Place("raw_data"))
net.add_place(ThresholdPlace("extracted", threshold=3))  # requires 3 to proceed
net.add_place(Place("transformed_batches"))
net.add_place(ResourcePlace("db_connections", capacity=2))  # max 2 concurrent DB writes
net.add_place(Place("loaded_data"))

# Transitions
# Extract step (simple enrichment)
net.add_transition(Transition(
    name="extract",
    inputs=[InputArc("raw_data")],
    outputs=[OutputArc("extracted")],
    action=lambda tokens: [Token(payload={"val": tokens[0].payload["val"] * 10})],
))

# Transform step (batches 3 tokens into 1)
net.add_transition(Transition(
    name="transform",
    inputs=[InputArc("extracted", count=3)],
    outputs=[OutputArc("transformed_batches")],
    action=transform_batch,
))

# Load step (requires DB connection slot)
net.add_transition(Transition(
    name="load",
    inputs=[InputArc("transformed_batches"), InputArc("db_connections")],
    outputs=[OutputArc("loaded_data"), OutputArc("db_connections")],
    action=load_db,
))

# Deposit 10 raw items
for i in range(10):
    net.deposit("raw_data", Token(payload={"val": i}))

# Run until quiescent
start = time.monotonic()
net.run(deadline=start + 5)

print(f"ETL completed. Loaded data count: {len(net.places['loaded_data'].tokens)}")
