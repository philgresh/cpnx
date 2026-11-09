"""Benchmark: per-step cost of #59's job-shop net, where both sides of a join grow with N (#64).

Jobs arrive as timed tokens over a simulated day, take a free worker, are worked in timed
steps, and sometimes park for an external delay. A parked job then either rejoins the *same*
worker or, after a long wait, goes back to the queue. Workers scale with the job count
(one per 130), so the load is constant and only the size of the net changes.

The rejoin is `resume`: a job in `rejoin` and the idle worker whose `wid` equals the job's.
Two variants of that join:

- `guard` -- the original net: `resume` and `requeue` both read `rejoin`, and `resume`'s
  guard tests the route and the `wid` equality.
- `join_key` -- `unpark` routes each job by output condition to `rejoin` (worker route) or
  `requeued` (queue route), and `resume` declares `join_key` on `wid` with no guard
  (ADR 0010).

Most idle workers have no job waiting in `rejoin`. Reading every idle worker's key on each
probe of `resume` made `join_key` grow 1.2 to 1.4x per doubling of N. With the engine
tracking which keys both places hold (ADR 0011) it should hold flat. Report the *shape*
(growth per doubling) and the ratio, not raw microseconds.
Native stdlib only.

    python benchmarks/bench_shop_join.py [N ...]
"""

import math
import random
import sys
import time
from pathlib import Path

# Make ``src/`` importable when run from a checkout without installation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cpnx.engine import PetriNet  # noqa: E402
from cpnx.places import Place  # noqa: E402
from cpnx.tokens import Token  # noqa: E402
from cpnx.transitions import InputArc, OutputArc, Transition  # noqa: E402

SEED = 11
DEPTHS = (1_000, 2_000, 4_000, 8_000, 16_000)


def _wid(token: Token) -> object:
    return token.payload["wid"]


def _is_done(tokens: list[Token]) -> bool:
    return tokens[0].payload["stage"] == "done"


def _not_done(tokens: list[Token]) -> bool:
    return tokens[0].payload["stage"] != "done"


def _via_worker(tokens: list[Token]) -> bool:
    return tokens[0].payload["via"] == "worker"


def _via_queue(tokens: list[Token]) -> bool:
    return tokens[0].payload["via"] == "queue"


def _resume_guard(tokens: list[Token]) -> bool:
    return tokens[0].payload["via"] == "worker" and tokens[0].payload["wid"] == tokens[1].payload["wid"]


def build(n: int, *, join: bool) -> PetriNet:
    """The shop net with `n` jobs, `max(2, n // 130)` workers, and the `resume` join as `join` picks."""
    rng = random.Random(SEED)
    net = PetriNet(seed=SEED, binding_search_limit=1_000_000)
    workers = max(2, n // 130)

    def ln(median: float, sigma: float) -> float:
        return rng.lognormvariate(math.log(median), sigma)

    names = ["queue", "idle", "busy", "parked", "rejoin", "done"] + (["requeued"] if join else [])
    for name in names:
        net.add_place(Place(name))

    def take(tokens: list[Token]) -> list[Token]:
        job, worker = tokens
        due = net.model_time + ln(2.0, 0.5)
        return [job.evolve(payload_updates={"wid": worker.payload["wid"]}, available_at=due)]

    def finish(tokens: list[Token]) -> list[Token]:
        (job,) = tokens
        free = Token(payload={"wid": job.payload["wid"]})
        if job.payload.get("resumed") or rng.random() < 0.25:
            return [job.evolve(payload_updates={"stage": "done"}), free]
        if rng.random() < 0.7:
            return [job.evolve(payload_updates={"stage": "done"}), free]
        back = net.model_time + ln(5.0, 1.4)
        return [job.evolve(payload_updates={"stage": "parked"}, available_at=back), free]

    def unpark(tokens: list[Token]) -> list[Token]:
        (job,) = tokens
        wait = ln(60.0, 1.1)
        if wait > 300.0:
            updates = {"resumed": True, "via": "queue"}
            return [job.evolve(payload_updates=updates, available_at=net.model_time + 300.0)]
        updates = {"resumed": True, "via": "worker"}
        return [job.evolve(payload_updates=updates, available_at=net.model_time + wait)]

    def resume(tokens: list[Token]) -> list[Token]:
        job, _worker = tokens
        return [job.evolve(available_at=net.model_time + ln(2.0, 0.5))]

    def requeue(tokens: list[Token]) -> list[Token]:
        (job,) = tokens
        return [job.evolve(payload_updates={"wid": None})]

    net.add_transition(
        Transition(name="take", inputs=[InputArc("queue"), InputArc("idle")], outputs=[OutputArc("busy")], action=take)
    )
    net.add_transition(
        Transition(
            name="finish",
            inputs=[InputArc("busy")],
            outputs=[
                OutputArc("done", condition=_is_done),
                OutputArc("parked", condition=_not_done),
                OutputArc("idle"),
            ],
            action=finish,
        )
    )
    if join:
        unpark_outputs = [OutputArc("rejoin", condition=_via_worker), OutputArc("requeued", condition=_via_queue)]
        resume_inputs = [InputArc("rejoin", join_key=_wid), InputArc("idle", join_key=_wid)]
        resume_guard = None
        requeue_from, requeue_guard = "requeued", None
    else:
        unpark_outputs = [OutputArc("rejoin")]
        resume_inputs = [InputArc("rejoin"), InputArc("idle")]
        resume_guard = _resume_guard
        requeue_from, requeue_guard = "rejoin", _via_queue
    net.add_transition(Transition(name="unpark", inputs=[InputArc("parked")], outputs=unpark_outputs, action=unpark))
    net.add_transition(
        Transition(name="resume", inputs=resume_inputs, outputs=[OutputArc("busy")], guard=resume_guard, action=resume)
    )
    net.add_transition(
        Transition(
            name="requeue",
            inputs=[InputArc(requeue_from)],
            outputs=[OutputArc("queue")],
            guard=requeue_guard,
            action=requeue,
        )
    )

    net.advance_time(1.0)
    for i in range(workers):
        net.deposit("idle", Token(payload={"wid": i}))
    for i in range(n):
        arrives = 1.0 + rng.uniform(0, 1440.0)
        net.deposit("queue", Token(payload={"job": i, "stage": "new"}, available_at=arrives))
    return net


def drive_us_per_step(n: int, *, join: bool) -> tuple[float, int]:
    """Microseconds per firing to drive the shop net with `n` jobs to quiescence, and the step count."""
    net = build(n, join=join)
    started = time.perf_counter()
    result = net.drive_to_quiescence()
    elapsed = time.perf_counter() - started
    assert len(net.places["done"]) == n, "every job should finish"
    return elapsed / result.steps * 1e6, result.steps


def main() -> None:
    depths = tuple(int(arg) for arg in sys.argv[1:]) or DEPTHS
    print("-- drive: drive_to_quiescence of the shop net, workers = N // 130 --")
    print(
        f"   {'jobs N':>8}  {'steps':>7}  {'guard us/step':>14}  {'growth':>7}  {'join_key us/step':>17}  {'growth':>7}"
    )
    prev_guard: float | None = None
    prev_join: float | None = None
    for n in depths:
        guard_us, steps = drive_us_per_step(n, join=False)
        join_us, _ = drive_us_per_step(n, join=True)
        guard_growth = f"x{guard_us / prev_guard:.2f}" if prev_guard else "-"
        join_growth = f"x{join_us / prev_join:.2f}" if prev_join else "-"
        print(f"   {n:>8}  {steps:>7}  {guard_us:>14.1f}  {guard_growth:>7}  {join_us:>17.1f}  {join_growth:>7}")
        prev_guard, prev_join = guard_us, join_us
    print()
    print("   Expected: join_key us/step holds ~flat as N doubles; the guard grows with the")
    print("   rejoin backlog, since FIRST walks every (job, idle worker) pair on a miss.")


if __name__ == "__main__":
    main()
