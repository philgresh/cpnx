"""The searching path reads only the candidates it inspects (issue #59, cause C).

`FIRST` stops at the first guard-satisfying binding, so the options it reads must scale with
that binding's position in the candidate order, not with the depth of the input places. The
order itself must not change: the lazy product has to yield exactly what `itertools.product`
over the fully built option lists would, or `FIRST` would pick a different binding.
"""

import itertools
import random

import pytest

from cpnx.engine import PetriNet, _lazy_product, _PeekStream
from cpnx.places import Place
from cpnx.tokens import Token
from cpnx.transitions import BindingPolicy, InputArc, OutputArc, Transition


class _CountingStream:
    """An iterator over `items` that counts how many it has handed out."""

    def __init__(self, items):
        self._it = iter(items)
        self.pulled = 0

    def __iter__(self):
        return self

    def __next__(self):
        item = next(self._it)
        self.pulled += 1
        return item


def test_lazy_product_matches_itertools_product():
    rng = random.Random(59)
    for _ in range(2_000):
        lists = [list(range(rng.randint(0, 5))) for _ in range(rng.randint(0, 4))]
        assert list(_lazy_product([iter(xs) for xs in lists])) == list(itertools.product(*lists)), lists


@pytest.mark.parametrize("take", [1, 2, 7, 8, 9, 30])
def test_lazy_product_reads_one_option_per_yield(take):
    streams = [_CountingStream(range(1_000)), _CountingStream(range(8)), _CountingStream(range(1_000))]
    got = list(itertools.islice(_lazy_product(streams), take))
    assert got == list(itertools.islice(itertools.product(range(1_000), range(8), range(1_000)), take))
    assert sum(s.pulled for s in streams) <= take + len(streams)


def test_lazy_product_stops_at_an_empty_stream_after_one_pull_from_each():
    streams = [_CountingStream(range(1_000)), _CountingStream([]), _CountingStream(range(1_000))]
    assert list(_lazy_product(streams)) == []
    assert [s.pulled for s in streams] == [1, 0, 1]


def test_every_arc_selection_runs_when_an_earlier_arc_is_empty():
    net = PetriNet(binding_search_limit=10)
    for name in ("a", "b", "c", "out"):
        net.add_place(Place(name))
    keyed: list[int] = []

    def key(token):
        keyed.append(token.payload["v"])
        return token.payload["v"]

    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("a"), InputArc("b", filter=lambda t: False), InputArc("c", key=key)],
            outputs=[OutputArc("out")],
            guard=lambda toks: True,
            action=lambda toks: toks[:1],
        )
    )
    net.deposit("a", Token(payload={"v": 0}))
    net.deposit("b", Token(payload={"v": 1}))
    for v in (3, 2):
        net.deposit("c", Token(payload={"v": v}))
    keyed.clear()
    assert net._resolve_binding(net.transitions["t"], net.model_time) is None
    assert sorted(keyed) == [2, 3]


def _eager_first(net, transition):
    """`FIRST`'s binding as `main` computed it: the full capped product, filtered by the guard."""
    pools = net._gather_arc_pools(transition, net.model_time, False)
    if pools is None:
        return None
    cap = net.binding_search_limit + 1
    option_lists = [list(itertools.islice(net._arc_options(arc, pool), cap)) for arc, pool in pools]
    for examined, combo in enumerate(itertools.product(*option_lists), start=1):
        if examined > net.binding_search_limit:
            return None
        if transition.guard([t for group in combo for t in group]):
            return [list(group) for group in combo]
    return None


def _random_net(rng):
    limit = rng.choice([3, 7, 50, 1_000])
    net = PetriNet(seed=rng.randrange(1_000), binding_search_limit=limit)
    arcs = []
    for p in range(rng.randint(1, 3)):
        name = f"bin{p}"
        net.add_place(Place(name))
        count = rng.choice([1, 1, 2])
        keyed = rng.random() < 0.3
        arcs.append(InputArc(name, count=count, key=(lambda t: -t.payload["v"]) if keyed else None))
        for i in range(rng.randint(0, 12)):
            net.deposit(name, Token(payload={"v": rng.randrange(6), "i": i}))
    net.add_place(Place("out"))
    modulus, residue = rng.randint(2, 7), rng.randrange(7)
    net.add_transition(
        Transition(
            name="pick",
            inputs=arcs,
            outputs=[OutputArc("out")],
            guard=lambda toks: sum(t.payload["v"] for t in toks) % modulus == residue % modulus,
            action=lambda toks: toks[:1],
        )
    )
    return net


def test_first_picks_the_same_binding_as_the_eager_product():
    rng = random.Random(1959)
    found = 0
    for _ in range(600):
        net = _random_net(rng)
        transition = net.transitions["pick"]
        expected = _eager_first(net, transition)
        binding = net._resolve_binding(transition, net.model_time)
        got = None if binding is None else [tokens for _arc, tokens in binding]
        assert got == expected
        assert net._binding_exists(transition, net.model_time) is (expected is not None)
        found += expected is not None
    assert 100 < found < 500, found


WORKERS = 8


def _join_net(depth, first_match, *, policy=None):
    """`depth` jobs waiting on 8 free workers; only job `first_match` and later have a worker."""
    net = PetriNet(seed=3, binding_search_limit=1_000_000)
    for name in ("waiting", "workers", "out"):
        net.add_place(Place(name))
    net.add_transition(
        Transition(
            name="match",
            inputs=[InputArc("waiting"), InputArc("workers")],
            outputs=[OutputArc("out"), OutputArc("workers")],
            guard=lambda toks: toks[0].payload["wid"] == toks[1].payload["wid"],
            action=lambda toks: toks,
            binding_policy=policy,
        )
    )
    for w in range(WORKERS):
        net.deposit("workers", Token(payload={"wid": w}))
    for i in range(depth):
        net.deposit("waiting", Token(payload={"job": i, "wid": WORKERS + 1 if i < first_match else 5}))
    return net


def _count_options(monkeypatch, net):
    yielded = [0]
    original = net._arc_options

    def counting(arc, available, **kwargs):
        for group in original(arc, available, **kwargs):
            yielded[0] += 1
            yield group

    monkeypatch.setattr(net, "_arc_options", counting)
    return yielded


@pytest.mark.parametrize("first_match", [0, 3, 40])
@pytest.mark.parametrize("probe", [False, True], ids=["resolve", "exists"])
def test_first_reads_options_up_to_the_first_satisfying_binding(monkeypatch, first_match, probe):
    depth = 5_000
    net = _join_net(depth, first_match)
    yielded = _count_options(monkeypatch, net)
    transition = net.transitions["match"]
    if probe:
        assert net._binding_exists(transition, net.model_time)
    else:
        binding = net._resolve_binding(transition, net.model_time)
        assert binding[0][1][0].payload["job"] == first_match
        assert binding[1][1][0].payload["wid"] == 5
    position = first_match * WORKERS + 5
    assert yielded[0] <= position + 1 + 2, yielded[0]


def test_random_still_enumerates_the_whole_capped_product(monkeypatch):
    depth = 200
    net = _join_net(depth, 0, policy=BindingPolicy.RANDOM)
    yielded = _count_options(monkeypatch, net)
    assert net._resolve_binding(net.transitions["match"], net.model_time) is not None
    assert yielded[0] == depth + WORKERS


def _spy_peek(monkeypatch, place):
    reads: list[int] = []
    original = place.peek

    def spy(count=1, model_time=None):
        result = original(count, model_time=model_time)
        reads.append(len(result))
        return result

    monkeypatch.setattr(place, "peek", spy)
    return reads


@pytest.mark.parametrize("first_match", [0, 3, 40])
def test_first_peeks_the_deep_pool_in_proportion_to_what_it_inspects(monkeypatch, first_match):
    net = _join_net(5_000, first_match)
    reads = _spy_peek(monkeypatch, net.places["waiting"])
    binding = net._resolve_binding(net.transitions["match"], net.model_time)
    assert binding[0][1][0].payload["job"] == first_match
    assert sum(reads) < 4 * (first_match + 1) + 8, reads


@pytest.mark.parametrize("policy", [BindingPolicy.RANDOM, BindingPolicy.PRIORITY])
def test_full_scan_policies_still_peek_the_whole_bound(monkeypatch, policy):
    net = _join_net(300, 0, policy=policy)
    reads = _spy_peek(monkeypatch, net.places["waiting"])
    assert net._resolve_binding(net.transitions["match"], net.model_time) is not None
    assert reads == [300]


def test_peek_stream_offers_each_token_once_if_the_place_changes_between_chunks():
    place = Place("jobs")
    tokens = [Token(payload={"i": i}) for i in range(40)]
    for token in tokens:
        place.deposit(token)
    seen = []
    for token in _PeekStream(place, None, 40):
        seen.append(token)
        if len(seen) == 3:
            place.retrieve(1)
    assert len(seen) == len({id(t) for t in seen})
    assert seen[:8] == tokens[:8]
    assert set(map(id, seen)) <= set(map(id, tokens))
