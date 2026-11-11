"""Benchmark: a self-join with two `join_key` functions over a `manager == id` chain (#78).

One place, `emp`, holds employees. Transition `pair` takes a manager and one of their reports
from it: its first arc keys on `id`, its second on `manager`, so a key binds when one token
carries it as `id` and another as `manager`. Employee `i` reports to `i - 1`, so every key but
the last binds, and N employees give N - 1 keys that bind.

Arcs on one place with different functions must take different tokens (#75), which the engine
checks with Hall's condition per key (ADR 0011 §4a). This measures what that and the key merge
cost as the keys grow:

- **probe** -- per-call `_binding_exists` with no guard (`free`), which needs one key that
  binds, and with an always-true guard (`guarded`), which walks candidates in product order.
- **fire** -- per-call `_resolve_binding` under `FIRST`, with no guard and with the guard. Both
  pick the first candidate in product order.
- **backlog** -- the guard-free probe with N self-managed employees (`manager == id`) and one
  pair: every self-managed key fails Hall's condition, since its only token carries it under
  both functions. Once the match is built, a probe should not read them.

Every column should hold ~flat as N quadruples. Report the shape (growth per step) and the
ratio, not raw microseconds. Native stdlib only.

    python benchmarks/bench_self_join_chain.py
"""

import sys
import timeit
from pathlib import Path

# Make ``src/`` importable when run from a checkout without installation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cpnx.engine import PetriNet  # noqa: E402
from cpnx.places import Place  # noqa: E402
from cpnx.tokens import Token  # noqa: E402
from cpnx.transitions import InputArc, OutputArc, Transition  # noqa: E402

SIZES = (1_000, 4_000, 16_000)


def _ident(token: Token) -> object:
    return token.payload["id"]


def _manager(token: Token) -> object:
    return token.payload["manager"]


def _always(tokens: list[Token]) -> bool:
    return True


def _build(payloads: list[dict], *, guarded: bool) -> PetriNet:
    """A net whose `pair` transition joins a manager and a report on `emp`, holding `payloads`."""
    net = PetriNet(seed=0)
    for name in ("emp", "out"):
        net.add_place(Place(name))
    net.add_transition(
        Transition(
            name="pair",
            inputs=[InputArc("emp", join_key=_ident), InputArc("emp", join_key=_manager)],
            outputs=[OutputArc("out", count=2)],
            action=lambda tokens: list(tokens),
            guard=_always if guarded else None,
        )
    )
    for payload in payloads:
        net.deposit("emp", Token(payload=payload))
    return net


def _chain(n: int) -> list[dict]:
    return [{"id": i, "manager": i - 1} for i in range(n)]


def _backlog(n: int) -> list[dict]:
    return [{"id": -i - 10, "manager": -i - 10} for i in range(n)] + [
        {"id": 1, "manager": 0},
        {"id": 0, "manager": -1},
    ]


def _us_per_call(call) -> float:
    """Microseconds per call, after two warm-up calls (the first builds the match)."""
    call()
    call()
    number, elapsed = timeit.Timer(call).autorange()
    best = min([elapsed / number] + [timeit.timeit(call, number=number) / number for _ in range(2)])
    return best * 1e6


def _probe(payloads: list[dict], *, guarded: bool) -> float:
    net = _build(payloads, guarded=guarded)
    transition = net.transitions["pair"]
    assert net._binding_exists(transition, net.model_time)
    return _us_per_call(lambda: net._binding_exists(transition, net.model_time))


def _fire(payloads: list[dict], *, guarded: bool) -> float:
    net = _build(payloads, guarded=guarded)
    transition = net.transitions["pair"]
    binding = net._resolve_binding(transition, net.model_time)
    assert binding is not None and binding[0][1][0] is not binding[1][1][0]
    return _us_per_call(lambda: net._resolve_binding(transition, net.model_time))


def main() -> None:
    columns = {
        "probe free": lambda n: _probe(_chain(n), guarded=False),
        "probe guarded": lambda n: _probe(_chain(n), guarded=True),
        "fire free": lambda n: _fire(_chain(n), guarded=False),
        "fire guarded": lambda n: _fire(_chain(n), guarded=True),
        "backlog probe": lambda n: _probe(_backlog(n), guarded=False),
    }
    print("-- us/call over a manager == id chain of N employees (backlog: N self-managed + 1 pair) --")
    print(f"   {'N':>7}" + "".join(f"  {name:>14}  {'growth':>7}" for name in columns))
    previous: dict[str, float] = {}
    for n in SIZES:
        row = f"   {n:>7}"
        for name, measure in columns.items():
            us = measure(n)
            growth = f"x{us / previous[name]:.2f}" if name in previous else "-"
            row += f"  {us:>14.1f}  {growth:>7}"
            previous[name] = us
        print(row)
    print()
    print("   Expected: every column holds ~flat as N quadruples (growth ~x1).")


if __name__ == "__main__":
    main()
