"""examples/circuit_breaker.py — Dependency-health circuit breaker.

A net has two transitions that depend on the *same* external service: an expensive `enrich`
step upstream, and a `deliver` step downstream that actually calls the service. When the service
goes down, `deliver` fails — and without coordination `enrich` keeps burning compute producing
intermediate work that `deliver` then discards.

A `CircuitBreakerPlace` fixes this. Both transitions gate on it with a non-consuming test arc, so
when `deliver`'s failures trip the breaker, *both* stop firing and their input tokens simply
queue. A probe re-checks the dependency on a cooldown cadence and closes the breaker on recovery,
at which point all the queued work resumes — no restart, no busy-spin.
"""

import time

from cpnx import CircuitBreakerPlace, InputArc, OutputArc, PetriNet, Place, Token, Transition


class ServiceDownError(Exception):
    """Raised by the downstream call while the external service is unavailable."""


# A stand-in for the external dependency's health, flipped by the driver below.
service = {"up": False}


def enrich(tokens: list[Token]) -> list[Token]:
    """Expensive upstream work whose result is wasted if `deliver` cannot run."""
    time.sleep(0.01)  # pretend this costs real compute
    return [tokens[0].evolve(payload_updates={"enriched": True})]


def deliver(tokens: list[Token]) -> list[Token]:
    """Downstream call to the shared external service."""
    time.sleep(0.005)
    if not service["up"]:
        raise ServiceDownError("dependency unavailable")
    return [tokens[0].evolve(payload_updates={"delivered": True})]


def probe() -> bool:
    """Cheap health check the engine runs on the cooldown cadence while the breaker is open."""
    return service["up"]


# A single worker plus a shallow `enriched` bound keeps the pipeline from racing the whole batch
# through before the breaker observes enough failures to trip — so the outage visibly *holds*
# work rather than draining it.
net = PetriNet(max_workers=1)
net.add_place(Place("incoming"))
net.add_place(Place("enriched", bound=3))
net.add_place(Place("delivered"))
net.add_place(
    CircuitBreakerPlace(
        "service_healthy",
        trip_predicate=lambda exc: isinstance(exc, ServiceDownError),
        failure_threshold=3,
        cooldown_secs=0.25,
        probe=probe,
    )
)

# Both transitions gate on the breaker via a test arc; `deliver` also reports its failures to it.
net.add_transition(
    Transition(
        name="enrich",
        inputs=[InputArc("incoming"), InputArc("service_healthy", test=True)],
        outputs=[OutputArc("enriched")],
        action=enrich,
        max_retries=0,
    )
)
net.add_transition(
    Transition(
        name="deliver",
        inputs=[InputArc("enriched"), InputArc("service_healthy", test=True)],
        outputs=[OutputArc("delivered")],
        action=deliver,
        breaker="service_healthy",
    )
)

for i in range(50):
    net.deposit("incoming", Token(payload={"req": i}))


def counts() -> str:
    p = net.places
    return (
        f"breaker={p['service_healthy'].state}, incoming={len(p['incoming'])}, "
        f"enriched={len(p['enriched'])}, delivered={len(p['delivered'])}"
    )


# Phase 1: the service is down. Step until the breaker trips, then observe that the gated
# transitions stop firing — the remaining requests wait in `incoming` and `enriched` instead of
# being enriched-then-discarded. (We step explicitly so the outage's effect is deterministic.)
net.validate()
for _ in range(200):
    net.step()
    net._await_inflight()
    if net.places["service_healthy"].is_open():
        break
print(f"while down:     {counts()}  <- work held, upstream stopped")

# Phase 2: the service recovers. The probe closes the breaker on the cooldown cadence and all
# the held work drains, with no restart.
service["up"] = True
net.run(deadline=time.monotonic() + 3.0)
print(f"after recovery: {counts()}  <- all requests delivered")
