"""Benchmark: a dispatcher choosing its binding through `binding_selector` (#80, ADR 0013).

A dispatcher pairs one waiting job with one free server whose skills include the job's. The
eligibility guard, `job.skill in server.skills`, is membership rather than equality, so no
`join_key` index applies, and the product search walks jobs x servers until the guard passes.

Jobs arrive every 0.002 time units; 100 servers each hold 3 of 13 skills and are busy for 1.0
per job, so a backlog builds. Three arms, in microseconds per firing on `drive_to_quiescence`:

- **FIRST**: the guarded product search, oldest eligible pair first.
- **RANDOM**: the same search with a uniform draw, over the first `binding_search_limit`
  candidates only.
- **selector**: a `binding_selector` that picks uniformly among all eligible pairs, reading the
  jobs grouped by skill from a place index. Its cost should not grow with the backlog.

The uniformity check repeats #80's: the first job picked over 200 seeds, from 2 000 waiting jobs
and 100 always-eligible servers. Under `RANDOM` it is always among the first 10 jobs.

Report the *shape* and the ratios, not raw microseconds. Native stdlib only.

    python benchmarks/bench_dispatch_selector.py
"""

import random
import sys
import time
from pathlib import Path

# Make ``src/`` importable when run from a checkout without installation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cpnx import BindingPolicy, BindingView, InputArc, OutputArc, PetriNet, Place, Token, Transition  # noqa: E402

JOBS = (1_000, 2_000, 4_000)
SERVERS = 100
SKILLS = 13
PER_SERVER = 3


def eligible(tokens: list[Token]) -> bool:
    return tokens[0].payload["skill"] in tokens[1].payload["skills"]


def skill_of(token: Token) -> object:
    return token.payload["skill"]


def pick_uniform_eligible(view: BindingView):
    """Choose uniformly among every (job, server) pair whose skills match."""
    by_skill = view.groups(0, skill_of)
    servers = []
    weights = []
    for server in view.available(1):
        weight = sum(len(by_skill[s]) for s in server.payload["skills"] if s in by_skill)
        if weight:
            servers.append(server)
            weights.append(weight)
    if not servers:
        return None
    draw = view.random.randrange(sum(weights))
    chosen = 0
    while draw >= weights[chosen]:
        draw -= weights[chosen]
        chosen += 1
    server = servers[chosen]
    for skill in sorted(server.payload["skills"]):
        group = by_skill.get(skill)
        if group is None:
            continue
        if draw < len(group):
            return [group[draw], server]
        draw -= len(group)
    raise AssertionError("unreachable: the draw is below the total weight")


def _dispatch_net(n_jobs: int, arm: str, seed: int = 0) -> PetriNet:
    rng = random.Random(seed)
    policy = BindingPolicy.RANDOM if arm == "RANDOM" else BindingPolicy.FIRST
    net = PetriNet(seed=0, binding_policy=policy)
    for name in ("queue", "free", "done"):
        net.add_place(Place(name))

    def dispatch(tokens: list[Token]) -> list[Token]:
        job, server = tokens
        return [job, server.evolve(available_at=net.model_time + 1.0)]

    selector = arm == "selector"
    net.add_transition(
        Transition(
            "dispatch",
            inputs=[InputArc("queue"), InputArc("free")],
            outputs=[OutputArc("done"), OutputArc("free")],
            guard=None if selector else eligible,
            action=dispatch,
            binding_selector=pick_uniform_eligible if selector else None,
        )
    )
    net.advance_time(1.0)
    for s in range(SERVERS):
        net.deposit("free", Token(payload={"s": s, "skills": frozenset(rng.sample(range(SKILLS), PER_SERVER))}))
    for j in range(n_jobs):
        net.deposit("queue", Token(payload={"j": j, "skill": rng.randrange(SKILLS)}, available_at=1.0 + 0.002 * j))
    return net


def _us_per_firing(n_jobs: int, arm: str) -> float:
    net = _dispatch_net(n_jobs, arm)
    started = time.perf_counter()
    result = net.drive_to_quiescence()
    elapsed = time.perf_counter() - started
    assert len(net.places["done"]) == n_jobs, (arm, len(net.places["done"]))
    return elapsed / result.steps * 1e6


def always(tokens: list[Token]) -> bool:
    return True


def any_server(token: Token) -> object:
    return 0


def pick_uniform_any(view: BindingView):
    jobs = view.group(0, any_server, 0)
    servers = list(view.available(1))
    return [jobs[view.random.randrange(len(jobs))], servers[view.random.randrange(len(servers))]]


def _first_pick(seed: int, arm: str, n_jobs: int = 2_000) -> int:
    net = PetriNet(seed=seed, binding_policy=BindingPolicy.RANDOM)
    for name in ("jobs", "servers", "done"):
        net.add_place(Place(name))
    picked: list[int] = []

    def act(tokens: list[Token]) -> list[Token]:
        picked.append(tokens[0].payload["j"])
        return [tokens[0], tokens[1]]

    selector = arm == "selector"
    net.add_transition(
        Transition(
            "dispatch",
            inputs=[InputArc("jobs"), InputArc("servers")],
            outputs=[OutputArc("done"), OutputArc("servers")],
            guard=None if selector else always,
            action=act,
            binding_selector=pick_uniform_any if selector else None,
        )
    )
    for s in range(SERVERS):
        net.deposit("servers", Token(payload={"s": s}))
    for j in range(n_jobs):
        net.deposit("jobs", Token(payload={"j": j}))
    with net:
        net.step()
        net._await_inflight()
    return picked[0]


def main() -> None:
    print(f"-- dispatch: {SERVERS} servers, {SKILLS} skills ({PER_SERVER} each), us per firing --")
    print(f"   {'jobs':>6}  {'FIRST':>9}  {'RANDOM':>9}  {'selector':>9}  {'RANDOM/selector':>16}  {'growth':>7}")
    prev: float | None = None
    for n in JOBS:
        first = _us_per_firing(n, "FIRST")
        rand = _us_per_firing(n, "RANDOM")
        sel = _us_per_firing(n, "selector")
        growth = f"x{sel / prev:.2f}" if prev else "-"
        print(f"   {n:>6}  {first:>9.1f}  {rand:>9.1f}  {sel:>9.1f}  {rand / sel:>15.1f}x  {growth:>7}")
        prev = sel

    print()
    print("-- uniformity: first job picked over 200 seeds, 2 000 jobs x 100 servers --")
    for arm in ("RANDOM", "selector"):
        picks = [_first_pick(seed, arm) for seed in range(200)]
        print(f"   {arm:>8}: max index {max(picks):>5}, distinct {len(set(picks)):>4}")
    print()
    print("   Expected: the selector's 'growth' stays near 1.0 as the backlog doubles, and its")
    print("   picks spread over the whole queue, where RANDOM's stay within the first 10 jobs.")


if __name__ == "__main__":
    main()
