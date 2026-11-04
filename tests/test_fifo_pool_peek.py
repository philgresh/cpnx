"""How many tokens the bounded FIFO peek reads, per transition shape (issue #59, cause B).

A head-only transition (`FIRST`, no guard) consumes only the first `count` tokens of each
arc, so its pool read must stop there. A searching transition still needs the first
`binding_search_limit + 1` candidates. On a place holding timed tokens every token read is
a heap pop and push, so over-reading is what made a guard-free drain scale with depth.
"""

import pytest

from cpnx.engine import PetriNet
from cpnx.places import Place
from cpnx.tokens import Token
from cpnx.transitions import BindingPolicy, InputArc, OutputArc, Transition

LIMIT = 5
DEPTH = 20


def _spy_peek(monkeypatch, place):
    """Record `len(result)` for every `peek` on *place*."""
    reads: list[int] = []
    original = place.peek

    def spy(count=1, model_time=None):
        result = original(count, model_time=model_time)
        reads.append(len(result))
        return result

    monkeypatch.setattr(place, "peek", spy)
    return reads


def _net(*, guard=None, policy=None, available_at=lambda i: 0.5 + 0.01 * i):
    net = PetriNet(seed=7, binding_search_limit=LIMIT)
    for name in ("jobs", "done"):
        net.add_place(Place(name))
    net.add_transition(
        Transition(
            name="work",
            inputs=[InputArc("jobs")],
            outputs=[OutputArc("done")],
            action=lambda toks: toks,
            guard=guard,
            binding_policy=policy,
        )
    )
    for i in range(DEPTH):
        net.deposit("jobs", Token(payload={"i": i}, available_at=available_at(i)))
    net.advance_time(1.0)
    return net


@pytest.mark.parametrize("available_at", [lambda i: 0.5 + 0.01 * i, lambda i: 0.0], ids=["timed", "untimed"])
def test_head_only_reads_only_count_tokens(monkeypatch, available_at):
    net = _net(available_at=available_at)
    reads = _spy_peek(monkeypatch, net.places["jobs"])
    assert net.step() is True
    assert reads and all(n == 1 for n in reads), reads


def test_head_only_quiescence_probe_reads_only_count_tokens(monkeypatch):
    net = _net(available_at=lambda i: 100.0 + i)
    reads = _spy_peek(monkeypatch, net.places["jobs"])
    assert net.is_quiescent() is False
    assert reads == [1]


@pytest.mark.parametrize(
    ("guard", "policy"),
    [
        (lambda toks: True, None),
        (None, BindingPolicy.RANDOM),
        (None, BindingPolicy.PRIORITY),
    ],
    ids=["guarded-first", "guard-free-random", "guard-free-priority"],
)
def test_searching_transitions_still_read_limit_plus_one(monkeypatch, guard, policy):
    net = _net(guard=guard, policy=policy)
    reads = _spy_peek(monkeypatch, net.places["jobs"])
    assert net.step() is True
    assert reads and all(n == LIMIT + 1 for n in reads), reads


def test_head_only_drain_order_is_unchanged():
    net = _net()
    net.drive_to_quiescence()
    assert [t.payload["i"] for t in net.places["done"].tokens] == list(range(DEPTH))
