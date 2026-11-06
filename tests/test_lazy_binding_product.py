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
def test_lazy_product_reads_options_in_proportion_to_what_it_yields(take):
    streams = [_CountingStream(range(1_000)), _CountingStream(range(8)), _CountingStream(range(1_000))]
    got = list(itertools.islice(_lazy_product(streams), take))
    assert got == list(itertools.islice(itertools.product(range(1_000), range(8), range(1_000)), take))
    assert sum(s.pulled for s in streams) <= 2 * take + len(streams)


@pytest.mark.parametrize("width", [32, 33, 40])
def test_lazy_product_order_holds_on_both_sides_of_the_width_cap(width):
    lists = [[0, 1, 2] if i in (0, width - 1) else [i] for i in range(width)]
    assert list(_lazy_product([iter(xs) for xs in lists])) == list(itertools.product(*lists))


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


NOW = 10.0


def _oracle_options(net, arc, t_limit):
    """One arc's candidate groups, built from public `Place` reads and `itertools` only."""
    place = net.places[arc.place]
    if not place.can_retrieve(arc.count, model_time=t_limit):
        return None
    pool = place.peek(len(place), model_time=t_limit)
    if arc.consume_all:
        return [pool]
    if arc.filter is not None:
        pool = [t for t in pool if arc.filter(t)]
    if arc.key is not None:
        pool = sorted(pool, key=arc.key)
    return [list(group) for group in itertools.combinations(pool, arc.count)]


def _oracle_search(net, transition, t_limit, *, probe, rng_state):
    """`(binding, guard_calls, exhausted)` as ADR 0001 specifies the search, independent of the engine.

    Candidates are the product of each arc's groups in lexicographic order, examined up to
    `binding_search_limit`. `FIRST` and the probe take the first satisfying one, `PRIORITY` the
    first-encountered minimum key, and `RANDOM` reservoir-samples with the net's RNG.
    """
    lists = []
    for arc in transition.inputs:
        options = _oracle_options(net, arc, t_limit)
        if options is None:
            return None, [], False
        lists.append(options)
    policy = transition.binding_policy or net.binding_policy
    stop_at_first = probe or policy is BindingPolicy.FIRST
    calls, satisfying, exhausted = [], [], False
    for examined, combo in enumerate(itertools.product(*lists), start=1):
        if examined > net.binding_search_limit:
            exhausted = True
            break
        flat = [t for group in combo for t in group]
        calls.append(tuple(t.id for t in flat))
        if transition.guard(flat):
            satisfying.append(([list(group) for group in combo], flat))
            if stop_at_first:
                break
    if not satisfying:
        return None, calls, exhausted
    if stop_at_first:
        return satisfying[0][0], calls, exhausted
    if policy is BindingPolicy.PRIORITY:
        best = min(range(len(satisfying)), key=lambda i: (transition.binding_priority_key(satisfying[i][1]), i))
        return satisfying[best][0], calls, exhausted
    rng = random.Random()
    rng.setstate(rng_state)
    chosen = None
    for seen, (binding, _flat) in enumerate(satisfying, start=1):
        if rng.random() < 1.0 / seen:
            chosen = binding
    return chosen, calls, exhausted


ARC_SHAPES = ("plain", "plain", "pair", "keyed", "filtered", "drain")


def _descending_v(token):
    return -token.payload["v"]


def _not_three(token):
    return token.payload["v"] != 3


def _priority(tokens):
    return (sum(t.payload["i"] for t in tokens) * 7) % 5


def _sum_guard(modulus, residue):
    return lambda toks: sum(t.payload["v"] for t in toks) % modulus == residue


GUARDS = [_sum_guard(m, r) for m in range(2, 8) for r in range(m)]


def _arc(name, shape):
    if shape == "pair":
        return InputArc(name, count=2)
    if shape == "keyed":
        return InputArc(name, key=_descending_v)
    if shape == "filtered":
        return InputArc(name, filter=_not_three)
    if shape == "drain":
        return InputArc(name, consume_all=True)
    return InputArc(name)


def _first_token(tokens):
    return tokens[:1]


def _random_net(rng):
    limit = rng.choice([3, 7, 50, 1_000])
    policy = rng.choice([None, BindingPolicy.FIRST, BindingPolicy.RANDOM, BindingPolicy.PRIORITY])
    net = PetriNet(seed=rng.randrange(1_000), binding_search_limit=limit)
    net.advance_time(NOW)
    arcs = []
    for p in range(rng.randint(1, 3)):
        name = f"bin{p}"
        net.add_place(Place(name))
        arcs.append(_arc(name, rng.choice(ARC_SHAPES)))
        timed_share = rng.choice([0.0, 0.0, 0.3, 0.8])
        for i in range(rng.choice([rng.randint(0, 12), rng.randint(9, 40)])):
            at = rng.uniform(0.0, 2 * NOW) if rng.random() < timed_share else 0.0
            net.deposit(name, Token(payload={"v": rng.randrange(6), "i": i}, available_at=at))
    net.add_place(Place("out"))
    net.add_transition(
        Transition(
            name="pick",
            inputs=arcs,
            outputs=[OutputArc("out")],
            guard=rng.choice(GUARDS),
            action=_first_token,
            binding_policy=policy,
            binding_priority_key=_priority,
        )
    )
    return net


def _record_search(monkeypatch, net):
    """Record every guard evaluation (as token ids) and every exhaustion signal on *net*."""
    calls: list[tuple[str, tuple[str, ...]]] = []
    check, signal = net._check_transition_guard, net._signal_search_exhausted

    def recording_check(transition, tokens):
        calls.append(("guard", tuple(t.id for t in tokens)))
        return check(transition, tokens)

    def recording_signal(name):
        calls.append(("exhausted", ()))
        return signal(name)

    monkeypatch.setattr(net, "_check_transition_guard", recording_check)
    monkeypatch.setattr(net, "_signal_search_exhausted", recording_signal)
    return calls


def _guard_calls(calls):
    return [ids for kind, ids in calls if kind == "guard"]


@pytest.mark.parametrize("ignore_timing", [False, True], ids=["at-model-time", "ignoring-timing"])
def test_every_policy_runs_the_same_search_as_an_independent_oracle(monkeypatch, ignore_timing):
    """Same binding, same guard evaluations in order, same exhaustion: resolution and probe, every policy."""
    rng = random.Random(1959)
    found = exhausted = streamed = 0
    policies = set()
    for _ in range(300):
        net = _random_net(rng)
        transition = net.transitions["pick"]
        m_time = float("inf") if ignore_timing else NOW
        calls = _record_search(monkeypatch, net)

        rng_state = net._rng.getstate()
        expected, oracle_calls, oracle_exhausted = _oracle_search(
            net, transition, m_time, probe=False, rng_state=rng_state
        )
        binding = net._resolve_binding(transition, m_time, ignore_timing=ignore_timing)
        got = None if binding is None else [tokens for _arc, tokens in binding]
        assert got == expected
        assert _guard_calls(calls) == oracle_calls
        assert (("exhausted", ()) in calls) is oracle_exhausted

        calls.clear()
        exists, probe_calls, probe_exhausted = _oracle_search(net, transition, m_time, probe=True, rng_state=None)
        assert net._binding_exists(transition, m_time, ignore_timing=ignore_timing) is (exists is not None)
        assert _guard_calls(calls) == probe_calls
        assert (("exhausted", ()) in calls) is probe_exhausted

        head_only, lazy = net._search_mode(transition, net._effective_policy(transition), probe=True)
        pools = net._gather_arc_pools(transition, m_time, ignore_timing, head_only=head_only, lazy=lazy) or []
        streamed += any(isinstance(pool, _PeekStream) for _arc, pool in pools)
        found += expected is not None
        exhausted += oracle_exhausted
        policies.add(net._effective_policy(transition))
    assert policies == set(BindingPolicy)
    assert 50 < found < 270, found
    assert exhausted > 10, exhausted
    assert streamed > 15, streamed


@pytest.mark.parametrize(
    ("shape", "limit", "policy", "streams"),
    [
        ("plain", 1_000, None, True),
        ("plain", 7, None, False),
        ("pair", 1_000, None, False),
        ("keyed", 1_000, None, False),
        ("filtered", 1_000, None, False),
        ("drain", 1_000, None, False),
        ("plain", 1_000, BindingPolicy.RANDOM, False),
        ("plain", 1_000, BindingPolicy.PRIORITY, False),
    ],
)
def test_only_a_deep_plain_single_token_arc_searched_lazily_streams(shape, limit, policy, streams):
    net = PetriNet(binding_search_limit=limit)
    net.add_place(Place("bin"))
    for i in range(40):
        net.deposit("bin", Token(payload={"v": i % 6, "i": i}))
    transition = Transition(
        name="pick",
        inputs=[_arc("bin", shape)],
        outputs=[],
        guard=lambda toks: True,
        action=lambda toks: toks,
        binding_policy=policy,
    )
    head_only, lazy = net._search_mode(transition, net._effective_policy(transition), probe=False)
    pools = net._gather_arc_pools(transition, net.model_time, False, head_only=head_only, lazy=lazy)
    assert isinstance(pools[0][1], _PeekStream) is streams
    assert isinstance(pools[0][1], list) is not streams


def test_a_head_only_transition_always_gets_lists():
    net = PetriNet(binding_search_limit=1_000)
    net.add_place(Place("bin"))
    for i in range(40):
        net.deposit("bin", Token(payload={"i": i}))
    transition = Transition(name="pick", inputs=[InputArc("bin")], outputs=[], action=lambda toks: toks)
    for probe in (False, True):
        head_only, lazy = net._search_mode(transition, net._effective_policy(transition), probe=probe)
        assert (head_only, lazy) == (True, False)
        pools = net._gather_arc_pools(transition, net.model_time, False, head_only=head_only, lazy=lazy)
        assert isinstance(pools[0][1], list)


class _GuardDroppingPlace(Place):
    """Clears its transition's guard during the pool read, standing in for another thread."""

    transition = None

    def can_retrieve(self, count=1, model_time=None):
        if self.transition is not None:
            self.transition.guard, self.transition = None, None
        return super().can_retrieve(count, model_time=model_time)


def test_reassigning_the_guard_mid_resolution_keeps_pools_and_path_consistent():
    net = PetriNet(binding_search_limit=1_000)
    place = _GuardDroppingPlace("bin")
    net.add_place(place)
    net.add_place(Place("out"))
    for i in range(40):
        net.deposit("bin", Token(payload={"i": i}))
    net.add_transition(
        Transition(
            name="pick",
            inputs=[InputArc("bin")],
            outputs=[OutputArc("out")],
            guard=lambda toks: False,
            action=lambda t: t,
        )
    )
    place.transition = net.transitions["pick"]
    binding = net._resolve_binding(net.transitions["pick"], net.model_time)
    assert binding is not None and binding[0][1][0].payload["i"] == 0


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
    assert yielded[0] <= 2 * (position + 1) + 2, yielded[0]


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
@pytest.mark.parametrize("probe", [False, True], ids=["resolve", "exists"])
def test_first_peeks_the_deep_pool_in_proportion_to_what_it_inspects(monkeypatch, first_match, probe):
    net = _join_net(5_000, first_match)
    reads = _spy_peek(monkeypatch, net.places["waiting"])
    transition = net.transitions["match"]
    if probe:
        assert net._binding_exists(transition, net.model_time)
    else:
        binding = net._resolve_binding(transition, net.model_time)
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


def test_peek_stream_offers_a_token_deposited_inside_the_read_prefix_between_chunks():
    place = Place("jobs")
    ready = [Token(payload={"i": i}) for i in range(4)]
    matured = [Token(payload={"i": 100 + i}, available_at=1.0 + 0.01 * i) for i in range(30)]
    for token in [*ready, *matured]:
        place.deposit(token, model_time=0.0)
    late = Token(payload={"i": -1})
    seen = []
    for token in _PeekStream(place, NOW, 40):
        seen.append(token)
        if len(seen) == 8:
            place.retrieve_specific([ready[0]], model_time=NOW)
            place.deposit(late, model_time=NOW)
    assert seen[:8] == [*ready, *matured[:4]]
    assert len(seen) == len({id(t) for t in seen})
    assert any(t is late for t in seen)
    assert {id(t) for t in seen} == {id(t) for t in [*ready, *matured, late]}


@pytest.mark.parametrize(
    ("guard", "policy"),
    [(lambda toks: True, None), (None, BindingPolicy.RANDOM), (None, BindingPolicy.PRIORITY)],
    ids=["guarded-first", "guard-free-random", "guard-free-priority"],
)
def test_a_transition_with_a_thousand_input_arcs_still_resolves(guard, policy):
    width = 1_100
    net = PetriNet(seed=5)
    names = [f"slot{i}" for i in range(width)]
    for name in [*names, "out"]:
        net.add_place(Place(name))
    for name in names:
        net.deposit(name, Token(payload={"slot": name}))
    net.add_transition(
        Transition(
            name="gather",
            inputs=[InputArc(name) for name in names],
            outputs=[OutputArc("out")],
            guard=guard,
            action=lambda toks: toks[:1],
            binding_policy=policy,
        )
    )
    assert net.is_dead() is False
    assert net.is_quiescent() is False
    assert net.step() is True
