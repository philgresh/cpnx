"""Two input arcs on one place take distinct tokens (issue #75).

In CPN terms, two arcs from one place take two tokens, as one arc with `count=2` does. A
binding that hands one token to both arcs is not a binding: firing it would remove the token
twice, and `retrieve_specific` raised `ValueError`. Every binding path must keep the arcs'
tokens apart: the head-only path, the product search (eager and lazy), the `join_key`
search, and the `consume_all` count-only path.
"""

import itertools
import math
import random
import time
import warnings

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from cpnx import BindingPolicy, InputArc, OutputArc, PetriNet, Place, Token, Transition, engine, places
from cpnx.engine import _DRAIN
from cpnx.places import _TokenStore

WID = lambda t: t.payload["wid"]  # noqa: E731
N = lambda t: t.payload["n"]  # noqa: E731
NEG_N = lambda t: -t.payload["n"]  # noqa: E731


def _differ(tokens: list[Token]) -> bool:
    return tokens[0].payload["n"] != tokens[1].payload["n"]


def _assert_distinct(net: PetriNet) -> None:
    binding = net._resolve_binding(net.transitions["t"], net.model_time)
    assert binding is not None
    flat = [token for _, tokens in binding for token in tokens]
    assert len({id(token) for token in flat}) == len(flat)


def _record_exhaustion(net: PetriNet) -> list[str]:
    """Record each exhausted search as it happens.

    `on_binding_search_exhausted` is buffered and flushed only by the public stepping APIs, so a
    bare `_resolve_binding` or `_binding_exists` would leave it empty.
    """
    exhausted: list[str] = []
    signal = net._signal_search_exhausted

    def record(name: str) -> None:
        exhausted.append(name)
        signal(name)

    net._signal_search_exhausted = record  # type: ignore[method-assign]
    return exhausted


def _shapes():
    same = InputArc("jobs")
    return {
        "same arc object": [same, same],
        "separate arcs": [InputArc("jobs"), InputArc("jobs")],
    }


@pytest.fixture(params=["same arc object", "separate arcs"])
def arcs(request):
    return _shapes()[request.param]


def _passthrough(tokens):
    return list(tokens)


def _net_moving(inputs: list[InputArc], *, n_tokens: int = 2, **kwargs) -> PetriNet:
    """A net whose transition `t` moves the tokens `inputs` take from `jobs` to `out`."""
    policy = kwargs.pop("binding_policy", None)
    net = PetriNet(seed=0, **({} if policy is None else {"binding_policy": policy}))
    for name in ("jobs", "other", "out"):
        net.add_place(Place(name))
    out_count = sum(arc.count for arc in inputs)
    net.add_transition(
        Transition(name="t", inputs=inputs, outputs=[OutputArc("out", count=out_count)], action=_passthrough, **kwargs)
    )
    for n in range(n_tokens):
        net.deposit("jobs", Token(payload={"wid": n // 2, "n": n}))
    return net


# -- the repro, on every policy ----------------------------------------------------------------


@pytest.mark.parametrize("policy", [BindingPolicy.FIRST, BindingPolicy.RANDOM, BindingPolicy.PRIORITY])
@pytest.mark.parametrize("guarded", [False, True])
def test_two_arcs_on_one_place_bind_and_fire_distinct_tokens(arcs, policy, guarded):
    guard = {"guard": _differ} if guarded else {}
    net = _net_moving(arcs, n_tokens=4, binding_policy=policy, **guard)
    _assert_distinct(net)
    net.drive_to_quiescence()
    assert len(net.marking["jobs"]) == 0
    assert len(net.marking["out"]) == 4


@pytest.mark.parametrize("policy", [BindingPolicy.FIRST, BindingPolicy.RANDOM, BindingPolicy.PRIORITY])
@pytest.mark.parametrize("guarded", [False, True])
def test_one_token_does_not_enable_two_arcs(arcs, policy, guarded):
    guard = {"guard": lambda tokens: True} if guarded else {}
    net = _net_moving(arcs, n_tokens=1, binding_policy=policy, **guard)
    t = net.transitions["t"]
    assert net._resolve_binding(t, net.model_time) is None
    assert not net._binding_exists(t, net.model_time)
    assert net.is_dead()
    net.drive_to_quiescence()
    assert len(net.marking["jobs"]) == 1


def test_head_only_takes_the_two_oldest_tokens(arcs):
    net = _net_moving(arcs, n_tokens=3)
    binding = net._resolve_binding(net.transitions["t"], net.model_time)
    assert [tokens[0].payload["n"] for _, tokens in binding] == [0, 1]


def test_search_skips_the_diagonal_but_keeps_product_order(arcs):
    net = _net_moving(arcs, n_tokens=3, guard=lambda tokens: True)
    binding = net._resolve_binding(net.transitions["t"], net.model_time)
    assert [tokens[0].payload["n"] for _, tokens in binding] == [0, 1]


def test_lazy_search_over_a_deep_place(arcs):
    # Deeper than `_PeekStream._FIRST_CHUNK`, so the lazy FIRST search streams its pools.
    net = _net_moving(arcs, n_tokens=40, guard=lambda tokens: tokens[0].payload["n"] + 1 == tokens[1].payload["n"])
    net.drive_to_quiescence()
    assert len(net.marking["out"]) == 40


def test_a_reused_token_is_never_a_candidate_so_it_spends_none_of_the_search_limit():
    # The later arc's options are the tokens the earlier arc left, so with a limit of 1 the
    # one candidate examined is (0, 1), not (0, 0).
    exhausted = []
    net = _net_moving([InputArc("jobs"), InputArc("jobs")], n_tokens=2, guard=lambda tokens: True)
    net.binding_search_limit = 1
    net.on_binding_search_exhausted = exhausted.append
    binding = net._resolve_binding(net.transitions["t"], net.model_time)
    assert [tokens[0].payload["n"] for _, tokens in binding] == [0, 1]
    assert exhausted == []


@pytest.mark.parametrize("counts", [(1, 2), (2, 2), (2, 1)])
@pytest.mark.parametrize("policy", [BindingPolicy.FIRST, BindingPolicy.RANDOM, BindingPolicy.PRIORITY])
def test_a_guarded_shared_place_deeper_than_the_search_limit_binds(counts, policy):
    # The second arc's first `limit + 1` pairs all hold token 0, which the first arc takes. Its
    # options must be cut to the limit after leaving out that token, not before.
    net = PetriNet(seed=0)
    for name in ("jobs", "out"):
        net.add_place(Place(name))
    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("jobs", count=counts[0]), InputArc("jobs", count=counts[1])],
            outputs=[OutputArc("out", count=sum(counts))],
            action=_passthrough,
            guard=lambda tokens: True,
            binding_policy=policy,
        )
    )
    net.binding_search_limit = 20
    for n in range(net.binding_search_limit + 10):
        net.deposit("jobs", Token(payload={"n": n}))
    t = net.transitions["t"]
    assert net._binding_exists(t, net.model_time)
    _assert_distinct(net)


@pytest.mark.parametrize("counts", [(1, 1, 1), (2, 1), (1, 3)])
def test_a_head_only_shared_place_reads_its_whole_demand_past_a_small_search_limit(counts):
    # The head-only path hands out the place's demand from one read, which must not be
    # clamped to `binding_search_limit + 1`.
    net = PetriNet(seed=0)
    for name in ("jobs", "out"):
        net.add_place(Place(name))
    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("jobs", count=count) for count in counts],
            outputs=[OutputArc("out", count=sum(counts))],
            action=_passthrough,
        )
    )
    net.binding_search_limit = 1
    for n in range(5):
        net.deposit("jobs", Token(payload={"n": n}))
    t = net.transitions["t"]
    assert net._is_head_only(t, net._effective_policy(t))
    assert net._binding_exists(t, net.model_time)
    _assert_distinct(net)
    net.drive_to_quiescence()
    assert len(net.marking["out"]) == sum(counts)


# -- arc shapes --------------------------------------------------------------------------------


@pytest.mark.parametrize("guarded", [False, True])
def test_key_ordered_arcs_each_take_their_own_head(guarded):
    # Both keys want the extremes; the second arc gets the best token the first one left.
    guard = {"guard": lambda tokens: True} if guarded else {}
    net = _net_moving([InputArc("jobs", key=N), InputArc("jobs", key=N)], n_tokens=4, **guard)
    binding = net._resolve_binding(net.transitions["t"], net.model_time)
    assert [tokens[0].payload["n"] for _, tokens in binding] == [0, 1]
    net = _net_moving([InputArc("jobs", key=NEG_N), InputArc("jobs", key=N)], n_tokens=4, **guard)
    binding = net._resolve_binding(net.transitions["t"], net.model_time)
    assert [tokens[0].payload["n"] for _, tokens in binding] == [3, 0]


@pytest.mark.parametrize("guarded", [False, True])
def test_counts_add_up_across_arcs_on_one_place(guarded):
    guard = {"guard": lambda tokens: True} if guarded else {}
    net = _net_moving([InputArc("jobs", count=2), InputArc("jobs")], n_tokens=2, **guard)
    assert net._resolve_binding(net.transitions["t"], net.model_time) is None
    net.deposit("jobs", Token(payload={"wid": 9, "n": 9}))
    binding = net._resolve_binding(net.transitions["t"], net.model_time)
    assert [[token.payload["n"] for token in tokens] for _, tokens in binding] == [[0, 1], [9]]
    net.drive_to_quiescence()
    assert len(net.marking["out"]) == 3


_POLICIES = [BindingPolicy.FIRST, BindingPolicy.RANDOM, BindingPolicy.PRIORITY]


def _is_flush(token: Token) -> bool:
    return token.payload.get("flush", False)


def _drain_net(inputs: list[InputArc], batch: int, policy, guarded: bool, *, flush: bool = True):
    """A net whose `t` reads `batch` (a flush marker, then `batch` jobs) and records what it fired with."""
    net = PetriNet(seed=0)
    for name in ("batch", "a", "b"):
        net.add_place(Place(name))
    fired: list[list[object]] = []

    def record(tokens: list[Token]) -> list[Token]:
        fired.append([token.payload.get("n", "flush") for token in tokens])
        return []

    guard = {"guard": lambda tokens: True} if guarded else {}
    net.add_transition(Transition(name="t", inputs=inputs, outputs=[], action=record, binding_policy=policy, **guard))
    exhausted = _record_exhaustion(net)
    if flush:
        net.deposit("batch", Token(payload={"flush": True}))
    for n in range(batch):
        net.deposit("batch", Token(payload={"n": n}))
    for name in ("a", "b"):
        net.deposit(name, Token(payload={"wid": 1, "n": name}))
    return net, fired, exhausted


@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("policy", _POLICIES)
@pytest.mark.parametrize("drain_first", [False, True])
@pytest.mark.parametrize("batch", [0, 4])
def test_a_consume_all_arc_takes_what_the_other_arcs_on_its_place_leave(guarded, policy, drain_first, batch):
    # "Marker + drain": one arc takes the flush marker, the other drains the place. The drain's
    # option is the whole pool, marker included, so it takes the rest instead, in either arc
    # order and on every path. One firing empties the place, and no token is taken twice. With
    # nothing but the marker the drain takes nothing: the place still holds its one token.
    inputs = [InputArc("batch", filter=_is_flush), InputArc("batch", consume_all=True)]
    if drain_first:
        inputs.reverse()
    net, fired, exhausted = _drain_net(inputs, batch, policy, guarded)
    t = net.transitions["t"]
    assert net._binding_exists(t, net.model_time)
    net.drive_to_quiescence()
    jobs = list(range(batch))
    assert fired == [[*jobs, "flush"] if drain_first else ["flush", *jobs]]
    assert len(net.marking["batch"]) == 0
    assert exhausted == []


@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("policy", _POLICIES)
@pytest.mark.parametrize("drain_first", [False, True])
def test_a_consume_all_arc_beside_a_plain_arc_takes_the_rest(guarded, policy, drain_first):
    # No filter: the plain arc takes one token, the drain every other one. Under FIRST the
    # plain arc takes the head; the other policies choose among the tokens.
    inputs = [InputArc("batch"), InputArc("batch", consume_all=True)]
    if drain_first:
        inputs.reverse()
    net, fired, exhausted = _drain_net(inputs, 4, policy, guarded, flush=False)
    if policy is BindingPolicy.FIRST and not guarded:  # the count-only path: the place is not read to bind
        assert net._resolve_binding(net.transitions["t"], net.model_time)[drain_first ^ 1][1] is _DRAIN
    net.drive_to_quiescence()
    assert len(fired) == 1
    assert sorted(fired[0]) == [0, 1, 2, 3]
    if policy is BindingPolicy.FIRST:
        assert fired[0] == ([1, 2, 3, 0] if drain_first else [0, 1, 2, 3])
    assert len(net.marking["batch"]) == 0
    assert exhausted == []


@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("policy", _POLICIES)
@pytest.mark.parametrize("order", ["join first", "drain first", "drain between"])
def test_a_consume_all_arc_in_a_join_takes_what_the_other_arcs_leave(guarded, policy, order):
    # The same in a transition with join arcs, where the drain's place is checked through the
    # join plan: the plain arc on `batch` and the drain never hold one token.
    join = [InputArc("a", join_key=WID), InputArc("b", join_key=WID)]
    plain, drain = InputArc("batch", filter=_is_flush), InputArc("batch", consume_all=True)
    inputs = {
        "join first": [*join, plain, drain],
        "drain first": [drain, *join, plain],
        "drain between": [join[0], drain, plain, join[1]],
    }[order]
    net, fired, exhausted = _drain_net(inputs, 3, policy, guarded)
    t = net.transitions["t"]
    assert net._binding_exists(t, net.model_time)
    net.drive_to_quiescence()
    assert len(fired) == 1
    assert sorted(map(str, fired[0])) == ["0", "1", "2", "a", "b", "flush"]
    assert len(net.marking["batch"]) == 0
    assert exhausted == []


@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("policy", _POLICIES)
@pytest.mark.parametrize("joined", [False, True])
def test_two_consume_all_arcs_on_one_place_warn_and_never_enable(guarded, policy, joined):
    # Each would drain what the other leaves. The shape is reported when the transition is
    # added, and it is settled before any search: the plain arc's 31 choices, all of them
    # useless, would otherwise run a limit of 5 out on every probe.
    inputs = [InputArc("batch"), InputArc("batch", consume_all=True), InputArc("batch", consume_all=True)]
    if joined:
        inputs += [InputArc("a", join_key=WID), InputArc("b", join_key=WID)]
    with pytest.warns(UserWarning, match="two consume_all input arcs on one place"):
        net, fired, exhausted = _drain_net(inputs, 30, policy, guarded)
    net.binding_search_limit = 5
    t = net.transitions["t"]
    assert not net._binding_exists(t, net.model_time)
    assert net._resolve_binding(t, net.model_time) is None
    net.drive_to_quiescence()
    assert fired == []
    assert len(net.marking["batch"]) == 31
    assert exhausted == []


@pytest.mark.parametrize("drain_first", [False, True])
def test_a_drain_on_a_shared_place_takes_the_rest_on_the_head_only_path(drain_first):
    # A `binding_priority_key` keeps a guard-free FIRST transition off the count-only path, so
    # `_head_binding` resolves it: the plain arc takes the head and the drain the rest.
    inputs = [InputArc("batch"), InputArc("batch", consume_all=True)]
    if drain_first:
        inputs.reverse()
    net = PetriNet(seed=0)
    net.add_place(Place("batch"))
    net.add_transition(
        Transition(name="t", inputs=inputs, outputs=[], action=lambda t: [], binding_priority_key=lambda t: 0)
    )
    for n in range(4):
        net.deposit("batch", Token(payload={"n": n}))
    binding = net._resolve_binding(net.transitions["t"], net.model_time)
    taken = [[token.payload["n"] for token in tokens] for _, tokens in binding]
    assert taken == ([[1, 2, 3], [0]] if drain_first else [[0], [1, 2, 3]])
    net.drive_to_quiescence()
    assert len(net.marking["batch"]) == 0


def test_draining_a_full_place_lets_the_transition_feeding_it_fire_again():
    # `q` holds one token at most, so `fill` waits while it is full. `flush` drains it at
    # firing time, which makes room for `fill` to fire again.
    net = PetriNet(seed=0)
    net.add_place(Place("src"))
    net.add_place(Place("q", bound=1))
    net.add_place(Place("go"))
    net.add_transition(Transition(name="fill", inputs=[InputArc("src")], outputs=[OutputArc("q")], action=_passthrough))
    net.add_transition(
        Transition(
            name="flush", inputs=[InputArc("q", consume_all=True), InputArc("go")], outputs=[], action=lambda t: []
        )
    )
    for n in range(2):
        net.deposit("src", Token(payload={"n": n}))
    net.drive_to_quiescence()
    assert (len(net.marking["src"]), len(net.marking["q"])) == (1, 1)
    net.deposit("go", Token(payload={}))
    net.drive_to_quiescence()
    assert (len(net.marking["src"]), len(net.marking["q"])) == (0, 1)


@pytest.mark.parametrize("drain_first", [False, True])
def test_a_failed_action_returns_every_token_of_a_drained_shared_place(drain_first):
    # The drain is read only when the transition fires, after the plain arc's token is taken.
    # Both arcs' tokens must be on record, so a failing action puts all of them back.
    def fail(tokens: list[Token]) -> list[Token]:
        raise RuntimeError("no")

    inputs = [InputArc("batch"), InputArc("batch", consume_all=True)]
    if drain_first:
        inputs.reverse()
    net = PetriNet(seed=0)
    net.add_place(Place("batch"))
    net.add_transition(Transition(name="t", inputs=inputs, outputs=[], action=fail))
    for n in range(4):
        net.deposit("batch", Token(payload={"n": n}))
    assert net.step() is True
    deadline = time.monotonic() + 5.0
    while len(net.marking["batch"]) < 4 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sorted(token.payload["n"] for token in net.marking["batch"]) == [0, 1, 2, 3]


def test_draining_the_rest_gives_one_drain_the_leftovers_and_refuses_two():
    from cpnx.engine import _draining_the_rest

    x, y, z = pool = [Token(payload={"n": n}) for n in range(3)]
    plain, drain = InputArc("p"), InputArc("p", consume_all=True)
    assert _draining_the_rest([(drain, pool), (plain, [y])], frozenset({"p"})) == [(drain, [x, z]), (plain, [y])]
    assert _draining_the_rest([(plain, [y]), (drain, pool), (drain, pool)], frozenset({"p"})) is None


def test_the_consume_all_fast_path_keeps_shared_arcs_apart():
    # A drain on a place of its own keeps the transition on the count-only path, which never
    # reads the drained place on a probe; the two `jobs` arcs must still take different tokens.
    net = PetriNet(seed=0)
    for name in ("jobs", "other", "out"):
        net.add_place(Place(name))
    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("jobs"), InputArc("jobs"), InputArc("other", consume_all=True)],
            outputs=[OutputArc("out", count=2)],
            action=lambda tokens: list(tokens[:2]),
        )
    )
    net.deposit("jobs", Token(payload={"n": 0}))
    net.deposit("jobs", Token(payload={"n": 1}))
    net.deposit("other", Token(payload={"n": 2}))
    binding = net._resolve_binding(net.transitions["t"], net.model_time)
    assert [[token.payload["n"] for token in tokens] for _, tokens in binding[:2]] == [[0], [1]]
    assert binding[2][1] is _DRAIN  # still the count-only path: `other` is not read until it fires
    net.drive_to_quiescence()
    assert len(net.marking["jobs"]) == 0
    assert len(net.marking["out"]) == 2


# -- join_key ----------------------------------------------------------------------------------


@pytest.mark.parametrize("policy", [BindingPolicy.FIRST, BindingPolicy.RANDOM, BindingPolicy.PRIORITY])
@pytest.mark.parametrize("certified", [True, False])
def test_a_self_join_pairs_distinct_tokens_with_equal_keys(policy, certified):
    table = {}
    key = WID if certified else (lambda t: table.get("x", t.payload["wid"]))
    same = InputArc("jobs", join_key=key)
    for inputs in ([same, same], [InputArc("jobs", join_key=key), InputArc("jobs", join_key=key)]):
        # wid 0 has tokens n=0 and n=1, wid 1 has n=2 and n=3, wid 2 has only n=4.
        net = _net_moving(inputs, n_tokens=5, binding_policy=policy)
        _assert_distinct(net)
        net.drive_to_quiescence()
        assert [token.payload["n"] for token in net.marking["jobs"]] == [4]
        out = [token.payload["wid"] for token in net.marking["out"]]
        assert sorted(out) == [0, 0, 1, 1]


def test_a_self_join_with_one_token_per_key_is_not_enabled():
    net = _net_moving([InputArc("jobs", join_key=N), InputArc("jobs", join_key=N)], n_tokens=3)
    t = net.transitions["t"]
    assert net._resolve_binding(t, net.model_time) is None
    assert not net._binding_exists(t, net.model_time)
    assert net.is_dead()


def test_a_self_join_probe_looks_past_a_key_with_one_token():
    # wid 0 has one token and wid 1 has two. A guard-free existence probe that checks only the
    # first key both places hold (as a single-key shortcut would) misses the pair under wid 1.
    net = PetriNet(seed=0)
    for name in ("jobs", "out"):
        net.add_place(Place(name))
    same = InputArc("jobs", join_key=WID)
    net.add_transition(
        Transition(name="t", inputs=[same, same], outputs=[OutputArc("out", count=2)], action=_passthrough)
    )
    for wid in (0, 1, 1):
        net.deposit("jobs", Token(payload={"wid": wid}))
    t = net.transitions["t"]
    # Repeated probes: the first registers the join indexes, the later ones read them.
    assert [net._binding_exists(t, net.model_time) for _ in range(3)] == [True, True, True]
    assert not net.is_dead()
    net.drive_to_quiescence()
    assert [token.payload["wid"] for token in net.marking["out"]] == [1, 1]
    assert net.is_dead()


# -- review follow-ups -------------------------------------------------------------------------


def _is_zero(token: Token) -> bool:
    return token.payload["n"] == 0


@pytest.mark.parametrize("guarded", [False, True])
def test_a_filtered_shared_arc_finds_the_assignment_greedy_misses(guarded):
    # The first arc's head is n=0, the only token the second arc's filter accepts. Taking the
    # heads greedily in arc order would leave the second arc nothing; the search binds n=1 then
    # n=0, with or without a guard.
    guard = {"guard": lambda tokens: True} if guarded else {}
    net = _net_moving([InputArc("jobs"), InputArc("jobs", filter=_is_zero)], n_tokens=2, **guard)
    t = net.transitions["t"]
    assert net._binding_exists(t, net.model_time)
    assert not net.is_dead()
    binding = net._resolve_binding(t, net.model_time)
    assert [tokens[0].payload["n"] for _, tokens in binding] == [1, 0]
    net.drive_to_quiescence()
    assert len(net.marking["jobs"]) == 0
    assert len(net.marking["out"]) == 2


def test_an_unfiltered_shared_arc_stays_head_only():
    net = _net_moving([InputArc("jobs"), InputArc("jobs", key=N)], n_tokens=2)
    t = net.transitions["t"]
    assert net._is_head_only(t, net._effective_policy(t))
    net = _net_moving([InputArc("jobs"), InputArc("jobs", filter=_is_zero)], n_tokens=2)
    t = net.transitions["t"]
    assert not net._is_head_only(t, net._effective_policy(t))


@pytest.mark.parametrize("certified", [True, False])
@pytest.mark.parametrize("guarded", [False, True])
def test_one_token_keys_do_not_use_up_a_self_joins_search_limit(certified, guarded):
    # More keys holding one token than the search limit, then one key holding a pair. Every
    # candidate of a one-token key reuses its token, so none may count against the limit.
    table = {}
    key = WID if certified else (lambda t: table.get("x", t.payload["wid"]))
    net = PetriNet(seed=0)
    for name in ("jobs", "out"):
        net.add_place(Place(name))
    same = InputArc("jobs", join_key=key)
    guard = {"guard": lambda tokens: True} if guarded else {}
    net.add_transition(
        Transition(name="t", inputs=[same, same], outputs=[OutputArc("out", count=2)], action=_passthrough, **guard)
    )
    net.binding_search_limit = 10
    exhausted = _record_exhaustion(net)
    for wid in range(net.binding_search_limit + 1):
        net.deposit("jobs", Token(payload={"wid": wid}))
    net.deposit("jobs", Token(payload={"wid": -1}))
    net.deposit("jobs", Token(payload={"wid": -1}))
    t = net.transitions["t"]
    assert [net._binding_exists(t, net.model_time) for _ in range(3)] == [True, True, True]
    assert net._resolve_binding(t, net.model_time) is not None
    net.drive_to_quiescence()
    assert [token.payload["wid"] for token in net.marking["out"]] == [-1, -1]
    assert net.is_dead()
    assert exhausted == []


# -- self-join match depth ---------------------------------------------------------------------


def test_a_depth_two_join_index_reports_a_key_once_it_holds_two_tokens():
    store = _TokenStore()
    store.register_join_index(1, WID, depth=2)
    source = ((1, 2),)

    def times(generation):
        got, every_head, changes = store.join_head_changes(source, generation)
        assert got == generation and every_head is None
        return {key: None if head is None else head[0] for key, head in changes.items()}

    first = Token(payload={"wid": 0, "n": 0})
    store.append(first)
    generation, every_head, changes = store.join_head_changes(source, None)
    assert every_head == {} and changes == {}
    timed = Token(payload={"wid": 0, "n": 1}, available_at=4.0)
    store.append(timed)
    assert times(generation) == {0: 4.0}
    # A third token behind the second changes nothing; one ahead of it does.
    store.append(Token(payload={"wid": 0, "n": 2}, available_at=9.0))
    assert times(generation) == {}
    ready = Token(payload={"wid": 0, "n": 3})
    store.append(ready)
    assert times(generation) == {0: float("-inf")}
    store.remove_identity([first, ready])
    assert times(generation) == {0: 9.0}
    store.remove_identity([timed])
    assert times(generation) == {0: None}


def _listed(net: PetriNet, t_limit: float) -> "tuple[list, list]":
    """The keys `t`'s match lists at `t_limit`: by when they bind, and in the order firing reads."""
    match = net._join_matches["t"]
    return list(match.iter_keys_at(t_limit)), [match.key_of(uid) for _, uid in match.ordered_keys(t_limit)]


def _self_join(n_single: int) -> PetriNet:
    net = PetriNet(seed=0)
    for name in ("jobs", "out"):
        net.add_place(Place(name))
    same = InputArc("jobs", join_key=WID)
    net.add_transition(
        Transition(name="t", inputs=[same, same], outputs=[OutputArc("out", count=2)], action=_passthrough)
    )
    for wid in range(n_single):
        net.deposit("jobs", Token(payload={"wid": wid}))
    return net


def test_a_self_joins_match_lists_only_keys_that_bind():
    # The match is what keeps a self-join's probe and firing costs off the backlog of keys with
    # one token: it lists a key only once the place holds a token for each join arc.
    net = _self_join(50)
    t = net.transitions["t"]
    assert not net._binding_exists(t, net.model_time)  # registers the indexes
    assert not net._binding_exists(t, net.model_time)
    assert _listed(net, net.model_time) == ([], [])
    net.deposit("jobs", Token(payload={"wid": 7}))
    assert net._binding_exists(t, net.model_time)
    assert _listed(net, net.model_time) == ([7], [7])
    net.drive_to_quiescence()
    assert [token.payload["wid"] for token in net.marking["out"]] == [7, 7]
    assert _listed(net, net.model_time) == ([], [])


def test_a_self_join_index_is_rebuilt_when_its_depth_changes():
    a, b = InputArc("jobs", join_key=WID), InputArc("other", join_key=WID)
    net = PetriNet(seed=0)
    for name in ("jobs", "other", "out"):
        net.add_place(Place(name))
    net.add_transition(Transition(name="t", inputs=[a, b], outputs=[OutputArc("out", count=2)], action=_passthrough))
    net.deposit("jobs", Token(payload={"wid": 0}))
    net.deposit("other", Token(payload={"wid": 0}))
    t = net.transitions["t"]
    assert [net._binding_exists(t, net.model_time) for _ in range(2)] == [True, True]
    b.place = "jobs"  # now a self-join on `jobs`, which holds one token of key 0
    assert [net._binding_exists(t, net.model_time) for _ in range(3)] == [False, False, False]
    net.deposit("jobs", Token(payload={"wid": 0}))
    assert [net._binding_exists(t, net.model_time) for _ in range(2)] == [True, True]


def _wid_is_zero(token: Token) -> bool:
    return token.payload["wid"] == 0


def test_a_join_place_shared_with_a_plain_arc_probes_past_the_first_key():
    # Key 0's only `jobs` token is also the only one the plain arc's filter accepts, so key 0
    # has no binding; key 1 does. The probe must not stop at whichever key the match lists first.
    for first_key in (0, 1):
        net = PetriNet(seed=0)
        for name in ("jobs", "other", "out"):
            net.add_place(Place(name))
        net.add_transition(
            Transition(
                name="t",
                inputs=[
                    InputArc("jobs", join_key=WID),
                    InputArc("other", join_key=WID),
                    InputArc("jobs", filter=_wid_is_zero),
                ],
                outputs=[OutputArc("out", count=3)],
                action=_passthrough,
            )
        )
        for wid in (first_key, 1 - first_key):
            net.deposit("jobs", Token(payload={"wid": wid}))
            net.deposit("other", Token(payload={"wid": wid}))
        t = net.transitions["t"]
        assert [net._binding_exists(t, net.model_time) for _ in range(3)] == [True, True, True]
        net.drive_to_quiescence()
        assert sorted(token.payload["wid"] for token in net.marking["out"]) == [0, 1, 1]


def _f(token: Token) -> object:
    return token.payload["f"]


def _g(token: Token) -> object:
    return token.payload["g"]


@pytest.mark.parametrize("certified", [True, False])
@pytest.mark.parametrize("guarded", [False, True])
def test_self_join_arcs_with_different_join_keys_need_one_token_each(certified, guarded):
    # Arcs with different `join_key` functions on one place match one token each, not N tokens
    # of one key: a (f=1) for the first arc and b (g=1) for the second is a binding, though no
    # key holds two tokens under either function.
    table = {}
    f = _f if certified else (lambda t: table.get("x", t.payload["f"]))
    guard = {"guard": lambda tokens: True} if guarded else {}
    net = PetriNet(seed=0)
    for name in ("jobs", "out"):
        net.add_place(Place(name))
    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("jobs", join_key=f), InputArc("jobs", join_key=_g)],
            outputs=[OutputArc("out", count=2)],
            action=_passthrough,
            **guard,
        )
    )
    net.deposit("jobs", Token(payload={"f": 1, "g": 2}))
    net.deposit("jobs", Token(payload={"f": 2, "g": 1}))
    t = net.transitions["t"]
    assert [net._binding_exists(t, net.model_time) for _ in range(3)] == [True, True, True]
    _assert_distinct(net)
    net.drive_to_quiescence()
    assert len(net.marking["out"]) == 2


@pytest.mark.parametrize("overlap_first", [True, False])
def test_a_token_matching_a_key_under_both_join_keys_does_not_end_the_probe(overlap_first):
    # x has f == g == 1, so key 1 is listed under both functions, but its only candidate is
    # (x, x), which reuses x. Key 2 binds through two tokens. The probe must look past key 1.
    net = PetriNet(seed=0)
    for name in ("jobs", "out"):
        net.add_place(Place(name))
    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("jobs", join_key=_f), InputArc("jobs", join_key=_g)],
            outputs=[OutputArc("out", count=2)],
            action=_passthrough,
        )
    )
    overlap = [Token(payload={"f": 1, "g": 1})]
    pair = [Token(payload={"f": 2, "g": 9}), Token(payload={"f": 8, "g": 2})]
    for token in overlap + pair if overlap_first else pair + overlap:
        net.deposit("jobs", token)
    t = net.transitions["t"]
    assert [net._binding_exists(t, net.model_time) for _ in range(3)] == [True, True, True]
    assert net._resolve_binding(t, net.model_time) is not None
    net.drive_to_quiescence()
    assert sorted(token.payload["f"] for token in net.marking["out"]) == [2, 8]
    assert [token.payload["f"] for token in net.marking["jobs"]] == [1]


# -- Hall's condition for mixed join_key functions (issue #78) ---------------------------------


@pytest.mark.parametrize("certified", [True, False])
@pytest.mark.parametrize("guarded", [False, True])
def test_keys_whose_only_token_matches_under_both_functions_do_not_use_up_the_search_limit(certified, guarded):
    # More keys than the search limit hold one token that carries the key under both functions,
    # so their only candidate reuses it: they fail Hall's condition (two arcs, one token) and
    # must not spend the limit. The pair after them binds key -1 through two tokens.
    table = {}
    f = _f if certified else (lambda t: table.get("x", t.payload["f"]))
    guard = {"guard": lambda tokens: True} if guarded else {}
    net = PetriNet(seed=0)
    for name in ("jobs", "out"):
        net.add_place(Place(name))
    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("jobs", join_key=f), InputArc("jobs", join_key=_g)],
            outputs=[OutputArc("out", count=2)],
            action=_passthrough,
            **guard,
        )
    )
    net.binding_search_limit = 10
    exhausted = _record_exhaustion(net)
    for k in range(net.binding_search_limit + 1):
        net.deposit("jobs", Token(payload={"f": k, "g": k}))
    net.deposit("jobs", Token(payload={"f": -1, "g": -9}))
    net.deposit("jobs", Token(payload={"f": -8, "g": -1}))
    t = net.transitions["t"]
    assert [net._binding_exists(t, net.model_time) for _ in range(3)] == [True, True, True]
    _assert_distinct(net)
    net.drive_to_quiescence()
    assert sorted(token.payload["f"] for token in net.marking["out"]) == [-8, -1]
    assert exhausted == []


def _h(token: Token) -> object:
    return token.payload["h"]


_FUNCTIONS = {"f": _f, "g": _g, "h": _h}


def _distinct_assignment_exists(functions: list[str], payloads: list[dict]) -> bool:
    """Brute force: some key and pairwise-distinct tokens, one per arc, each carrying the key."""
    for key in {payload[name] for payload in payloads for name in functions}:
        if _assign(functions, payloads, key, set()):
            return True
    return False


def _assign(functions: list[str], payloads: list[dict], key: object, used: set[int]) -> bool:
    if not functions:
        return True
    first, rest = functions[0], functions[1:]
    for i, payload in enumerate(payloads):
        if i not in used and payload[first] == key and _assign(rest, payloads, key, used | {i}):
            return True
    return False


_values = st.integers(min_value=0, max_value=2)


@settings(max_examples=150, deadline=None)
@given(
    functions=st.lists(st.sampled_from(sorted(_FUNCTIONS)), min_size=2, max_size=4),
    payloads=st.lists(st.fixed_dictionaries({"f": _values, "g": _values, "h": _values}), max_size=6),
    guarded=st.booleans(),
)
def test_a_self_join_binds_exactly_when_hall_condition_holds(functions, payloads, guarded):
    # The engine's existence probe and firing path must agree with each other and with a
    # brute-force search for distinct tokens (Hall's theorem: one exists iff every set of arcs
    # reaches at least as many tokens as it has arcs), whatever mix of join_key functions the
    # arcs on the place use.
    guard = {"guard": lambda tokens: True} if guarded else {}
    net = PetriNet(seed=0)
    for name in ("jobs", "out"):
        net.add_place(Place(name))
    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("jobs", join_key=_FUNCTIONS[name]) for name in functions],
            outputs=[OutputArc("out", count=len(functions))],
            action=_passthrough,
            **guard,
        )
    )
    for payload in payloads:
        net.deposit("jobs", Token(payload=dict(payload)))
    t = net.transitions["t"]
    expected = _distinct_assignment_exists(functions, payloads)
    assert net._binding_exists(t, net.model_time) is expected
    binding = net._resolve_binding(t, net.model_time)
    assert (binding is not None) is expected
    if binding is not None:
        flat = [token for _, tokens in binding for token in tokens]
        assert len({id(token) for token in flat}) == len(flat)
        assert len({_FUNCTIONS[name](token) for name, token in zip(functions, flat, strict=True)}) == 1


# -- incremental Hall state and the lazy key merge ---------------------------------------------


def _ident(token: Token) -> object:
    return token.payload["id"]


def _manager(token: Token) -> object:
    return token.payload["manager"]


def _org_chart(n_self_managed: int, *, guarded: bool = False) -> PetriNet:
    """`t` pairs a manager (`id`) with a report (`manager`) on one place, which holds a self-managed backlog.

    Each self-managed employee lists its key under both functions through one token, so every
    such key fails Hall's condition.
    """
    net = PetriNet(seed=0)
    for name in ("emp", "out"):
        net.add_place(Place(name))
    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("emp", join_key=_ident), InputArc("emp", join_key=_manager)],
            outputs=[OutputArc("out", count=2)],
            action=_passthrough,
            **({"guard": lambda tokens: True} if guarded else {}),
        )
    )
    for i in range(n_self_managed):
        net.deposit("emp", Token(payload={"id": i, "manager": i}))
    return net


def _spy_reads(monkeypatch) -> "tuple[list[int], list[int]]":
    """Record each Hall check and the number of keys each join-group lookup reads."""
    halls: list[int] = []
    looked_up: list[int] = []
    hall_time = places._hall_time
    available_groups = places._JoinIndex.available_groups

    def counting_hall_time(classes):
        halls.append(1)
        return hall_time(classes)

    def counting_available_groups(self, keys, *args):
        keys = None if keys is None else list(keys)
        looked_up.append(-1 if keys is None else len(keys))
        return available_groups(self, keys, *args)

    monkeypatch.setattr(places, "_hall_time", counting_hall_time)
    monkeypatch.setattr(places._JoinIndex, "available_groups", counting_available_groups)
    return halls, looked_up


@pytest.mark.parametrize("guarded", [False, True])
def test_a_backlog_of_keys_failing_hall_is_not_scanned(monkeypatch, guarded):
    # 2 000 self-managed employees: no key binds. Once the match is built, a probe pays for the
    # changes since the last one, not for the backlog, and a deposit rechecks only its own keys.
    net = _org_chart(2_000, guarded=guarded)
    t = net.transitions["t"]
    now = net.model_time
    assert not net._binding_exists(t, now)  # registers the indexes and builds the match
    halls, looked_up = _spy_reads(monkeypatch)
    for _ in range(3):
        assert not net._binding_exists(t, now)
        assert net._resolve_binding(t, now) is None
    assert halls == [] and looked_up == []
    assert _listed(net, now) == ([], [])
    boss = Token(payload={"id": 9_000, "manager": -1})
    report = Token(payload={"id": 9_001, "manager": 9_000})
    net.deposit("emp", boss)
    net.deposit("emp", report)
    assert net._binding_exists(t, now)
    assert len(halls) <= 4  # keys 9 000, 9 001 and -1, each read once at most
    assert _listed(net, now) == ([9_000], [9_000])
    binding = net._resolve_binding(t, now)
    assert [tokens[0] for _, tokens in binding] == [boss, report]
    assert sum(looked_up) <= 4 and min(looked_up) >= 1  # one key per arc and call, never a scan


@pytest.mark.parametrize("policy", [BindingPolicy.FIRST, BindingPolicy.RANDOM, BindingPolicy.PRIORITY])
def test_firing_a_chain_opens_only_the_keys_it_reaches(monkeypatch, policy):
    # A `manager == id` chain of 2 000: every key but the last binds. A guarded firing opens the
    # keys in order of their candidates' lower bounds as the merge reaches them, so FIRST reads two keys
    # and RANDOM and PRIORITY about one per candidate examined, not every key.
    net = _org_chart(0, guarded=True)
    net.binding_policy = policy
    net.binding_search_limit = 10
    for i in range(2_000):
        net.deposit("emp", Token(payload={"id": i, "manager": i - 1}))
    t = net.transitions["t"]
    assert net._resolve_binding(t, net.model_time) is not None
    _, looked_up = _spy_reads(monkeypatch)
    binding = net._resolve_binding(t, net.model_time)
    assert min(looked_up) == 1
    # The search pulls one candidate past the limit to learn it is exhausted, and each pull opens
    # at most one key ahead of the merge.
    bound = 2 if policy is BindingPolicy.FIRST else net.binding_search_limit + 2
    assert len(looked_up) <= 2 * bound
    if policy is BindingPolicy.FIRST:
        assert [tokens[0].payload["id"] for _, tokens in binding] == [0, 1]


def _two_function_store() -> _TokenStore:
    """A store with join indexes 1 (`f`) and 2 (`g`), each watching both arcs of an `[f, g]` place."""
    store = _TokenStore()
    store.register_join_index(1, _f, depth=2)
    store.register_join_index(2, _g, depth=2)
    return store


_F_AND_G = ((1, 1), (2, 1))


def test_a_mixed_report_times_hall_from_timed_entries():
    store = _two_function_store()
    x = Token(payload={"f": 1, "g": 1})
    store.append(x)
    generations, every_head, changes = store.join_head_changes(_F_AND_G, None)
    assert every_head == {} and changes == {}  # key 1's only token would serve both arcs
    store.append(Token(payload={"f": 1, "g": 9}, available_at=4.0))
    got, every_head, changes = store.join_head_changes(_F_AND_G, generations)
    assert got == generations and every_head is None
    # From 4.0 the `f` arc takes the timed token and the `g` arc takes x. Key 9 has no `f` token.
    assert changes == {1: (4.0, (0, 0.0, 0)), 9: None}
    store.append(Token(payload={"f": 7, "g": 1}, available_at=2.0))
    assert store.join_head_changes(_F_AND_G, generations)[2] == {1: (2.0, (0, 0.0, 0)), 7: None}


def test_a_mixed_report_drains_nothing_while_one_index_is_down():
    store = _two_function_store()
    generations, _, _ = store.join_head_changes(_F_AND_G, None)
    store.append(Token(payload={"f": 5}))  # keys under `f`, raises under `g`
    assert store.join_head_changes(_F_AND_G, generations) is None
    assert 5 in store.join_index(1)._dirty, "the serving index keeps its changes for the next report"


def test_a_mixed_report_is_full_when_one_index_is_replaced():
    store = _two_function_store()
    store.append(Token(payload={"f": 1, "g": 2}))
    store.append(Token(payload={"f": 2, "g": 1}))
    generations, every_head, _ = store.join_head_changes(_F_AND_G, None)
    assert set(every_head) == {1, 2}
    store.register_join_index(2, lambda token: token.payload["g"], depth=2)  # a new `g` index
    got, every_head, changes = store.join_head_changes(_F_AND_G, generations)
    assert got[0] == generations[0] and got[1] != generations[1]
    assert every_head is not None and set(every_head) == {1, 2} and changes == {}


def _registered_ids(net: PetriNet) -> set[int]:
    return {entry[3] for entry in net._join_registrations["t"].values()}


def _drained_ids(net: PetriNet) -> set[int]:
    return {index_id for _, pairs in net._join_matches["t"].sources for index_id, _ in pairs}


@pytest.mark.parametrize("arcs", [2, 3, 5])
def test_a_self_join_keys_each_deposit_once(arcs):
    # Join arcs reading one place with one function share an index, and the match drains it.
    net = _self_join(0)
    net.transitions["t"].inputs[:] = [InputArc("jobs", join_key=WID) for _ in range(arcs)]
    net.transitions["t"].outputs[0].count = arcs
    for _ in range(arcs):
        net.deposit("jobs", Token(payload={"wid": 1}))
    t = net.transitions["t"]
    assert net._binding_exists(t, net.model_time)
    assert len(net.places["jobs"]._store._join_indexes) == 1
    assert _registered_ids(net) == _drained_ids(net)


def test_a_reassigned_join_key_leaves_no_index_unread():
    # Arcs [f, g, g] read through one index per function. Setting arc 0's key to g moves it onto
    # the g index and unregisters the f one: every registered index is one the match drains, so
    # none collects changed keys that nothing reads.
    arcs = [InputArc("jobs", join_key=_f), InputArc("jobs", join_key=_g), InputArc("jobs", join_key=_g)]
    net = PetriNet(seed=0)
    for name in ("jobs", "out"):
        net.add_place(Place(name))
    net.add_transition(Transition(name="t", inputs=arcs, outputs=[OutputArc("out", count=3)], action=_passthrough))
    t = net.transitions["t"]
    store = net.places["jobs"]._store
    net.deposit("jobs", Token(payload={"f": 0, "g": 0}))
    assert not net._binding_exists(t, net.model_time)
    assert len(_registered_ids(net)) == 2 and _registered_ids(net) == _drained_ids(net)
    arcs[0].join_key = _g
    assert not net._binding_exists(t, net.model_time)
    assert len(_registered_ids(net)) == 1 and _registered_ids(net) == _drained_ids(net)
    assert set(store._join_indexes) == _registered_ids(net)
    for n in range(2_000):
        net.deposit("jobs", Token(payload={"f": -1, "g": 1_000 + n}))
    assert not net._binding_exists(t, net.model_time)
    assert all(not index._dirty for index in store._join_indexes.values())
    for _ in range(2):
        net.deposit("jobs", Token(payload={"f": -1, "g": 1_000}))
    assert net._binding_exists(t, net.model_time)  # key 1000 now holds three tokens


# The shared-head case: x is key 1's head under `f` and its only token under `g`, and y likewise
# for key 2, so the search keeps the first arc off each head and those keys' first candidates
# start late: (5, 0) and (6, 1). Key 3's candidate (z, w) = (2, 3) comes first in product
# order, though the match sorts it after keys 1 and 2 by their heads. The filler makes the
# property test's guard (n sum not 1 mod 3) accept (5, 0), so firing it first shows.
_CHANGES = st.one_of(
    st.none(),
    st.tuples(
        st.just("add"),
        st.fixed_dictionaries({"f": _values, "g": _values, "h": _values}),
        st.sampled_from([None, 1.0, 4.0]),
    ),
    st.tuples(st.just("remove"), st.integers(0, 9)),
)
"""A change to `jobs` before a probe: deposit a token, remove the token tagged `n`, or nothing."""


def _apply_change(change, nets: "list[PetriNet]", added) -> None:
    """Apply one `_CHANGES` draw to every net alike, so the match's views update between probes."""
    if change is None:
        return
    if change[0] == "add":
        _, payload, at = change
        n = next(added)
        for net in nets:
            net.deposit("jobs", Token(payload={**payload, "n": n}, **({} if at is None else {"available_at": at})))
        return
    for net in nets:
        place = net.places["jobs"]
        doomed = [token for token in place.peek(len(place), model_time=math.inf) if token.payload["n"] == change[1]]
        if doomed:
            place.retrieve_specific(doomed)


_SHARED_HEAD = [
    {"f": 1, "g": 1, "h": 0},  # x
    {"f": 2, "g": 2, "h": 0},  # y
    {"f": 3, "g": 0, "h": 0},  # z
    {"f": 9, "g": 3, "h": 0},  # w
    {"f": 6, "g": 6, "h": 0},  # a filler whose key fails Hall's condition, so n lines up below
    {"f": 1, "g": 8, "h": 0},  # key 1's second f-token
    {"f": 2, "g": 7, "h": 0},  # key 2's second f-token
]


def _shared_head_net(certified: bool, guard, limit: int) -> "tuple[PetriNet, list[str]]":
    """Arcs `[f, g]` on one place holding `_SHARED_HEAD`, each token tagged with its deposit index `n`."""
    f, g = (_f, _g) if certified else (_uncertified(_f), _uncertified(_g))
    net = PetriNet(seed=0)
    for name in ("jobs", "out"):
        net.add_place(Place(name))
    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("jobs", join_key=f), InputArc("jobs", join_key=g)],
            outputs=[OutputArc("out", count=2)],
            action=_passthrough,
            **({} if guard is None else {"guard": guard}),
        )
    )
    net.binding_search_limit = limit
    exhausted = _record_exhaustion(net)
    for n, payload in enumerate(_SHARED_HEAD):
        net.deposit("jobs", Token(payload={**payload, "n": n}))
    return net, exhausted


@pytest.mark.parametrize(
    ("guard", "limit", "expected"),
    [
        (None, 8, [2, 3]),
        (lambda tokens: True, 8, [2, 3]),
        # Product order examines (z, w) first: with a limit of one, rejecting it ends the search.
        (lambda tokens: tokens[0].payload["n"] != 2, 1, None),
        (lambda tokens: tokens[0].payload["n"] != 2, 2, [5, 0]),
    ],
)
def test_a_key_whose_head_the_search_skips_does_not_hide_an_earlier_key(guard, limit, expected):
    # Keys 1 and 2 sort first by their heads (x, y), but each head is also the key's only token
    # under `g`, so the first arc must skip it and their first candidates start at their second
    # `f` tokens (n=5, n=6). Key 3's (z, w) = (2, 3) comes first in product order. The merge must
    # treat the heads as lower bounds and open key 3 before yielding key 1's candidate.
    results = []
    for certified in (True, False):
        net, exhausted = _shared_head_net(certified, guard, limit)
        t = net.transitions["t"]
        binding = net._resolve_binding(t, net.model_time)
        got = None if binding is None else [tokens[0].payload["n"] for _, tokens in binding]
        results.append((got, net._binding_exists(t, net.model_time), len(exhausted) > 0))
    assert results[0] == results[1]
    assert results[0][0] == expected


def test_a_self_join_whose_index_is_down_falls_back_and_binds():
    # Both arcs of a self-join read one index. With it disabled behind its rebuild rate limit the
    # match does not serve, and the fallback must still find the pair (a declined index never
    # reads as "key gone").
    net = _self_join(10)
    net.deposit("jobs", Token(payload={"wid": 3}))
    t = net.transitions["t"]
    assert net._binding_exists(t, net.model_time)
    (index_id,) = _registered_ids(net)
    index = net.places["jobs"]._store._join_indexes[index_id]
    index.disable()
    index._last_build = net.places["jobs"]._store._next_seq  # rate-limited: no rebuild for now
    assert net.places["jobs"].peek_join(index_id, None, model_time=net.model_time) is None
    assert net._binding_exists(t, net.model_time)
    binding = net._resolve_binding(t, net.model_time)
    assert binding is not None and [tokens[0].payload["wid"] for _, tokens in binding] == [3, 3]
    assert binding[0][1][0] is not binding[1][1][0]


def _uncertified(fn):
    """`fn` behind a closure over a mutable table, so certification refuses it and the engine falls back."""
    table: dict = {}
    return lambda token: table.get("never", fn(token))


@settings(max_examples=150, deadline=None)
@given(
    functions=st.lists(st.sampled_from(sorted(_FUNCTIONS)), min_size=2, max_size=3),
    payloads=st.lists(
        st.tuples(
            st.fixed_dictionaries({"f": _values, "g": _values, "h": _values}),
            st.sampled_from([None, 1.0, 2.0, 4.0]),
        ),
        max_size=7,
    ),
    probes=st.lists(
        st.tuples(st.sampled_from([0.0, 1.0, 2.0, 3.0, 5.0]), st.booleans(), _CHANGES), min_size=1, max_size=6
    ),
    policy=st.sampled_from([BindingPolicy.FIRST, BindingPolicy.RANDOM, BindingPolicy.PRIORITY]),
    limit=st.integers(1, 8),
    lead=st.lists(st.tuples(st.sampled_from(["jobs", "side"]), st.integers(1, 2)), max_size=2),
    side=st.integers(0, 3),
)
@example(
    functions=["f", "g"],
    payloads=[(payload, None) for payload in _SHARED_HEAD],
    probes=[(0.0, False, None)],
    policy=BindingPolicy.FIRST,
    limit=8,
    lead=[],
    side=0,
)
@example(
    functions=["f", "g"],
    payloads=[(payload, None) for payload in _SHARED_HEAD],
    probes=[(0.0, False, None)],
    policy=BindingPolicy.FIRST,
    limit=2,
    lead=[],
    side=0,
)
@example(
    functions=["f", "g"],
    payloads=[(payload, None) for payload in _SHARED_HEAD],
    probes=[(0.0, False, None)],
    policy=BindingPolicy.FIRST,
    limit=8,
    lead=[("side", 1), ("jobs", 2)],
    side=2,
)
def test_the_key_match_searches_exactly_as_the_fallback_does(functions, payloads, probes, policy, limit, lead, side):
    # The match (certified keys) lists keys by Hall's condition as it tracks it, and the firing
    # path opens them lazily, sorted by a lower bound on their candidates. The fallback (uncertified keys)
    # reads every key and merges every product. Probes and firings at any time, moving back and
    # forth, and with the timing ignored, must pick the same bindings and report the same
    # exhaustion, under every policy and a small search limit. Arcs without a `join_key` may come
    # first, on another place (`side`) or on the join place, with `count` 1 or 2: the merge bounds
    # every key's candidates by those arcs' least positions, which must stay a lower bound.
    fallback = {name: _uncertified(fn) for name, fn in _FUNCTIONS.items()}

    def build(by_name):
        net = PetriNet(seed=3, binding_policy=policy)
        for name in ("jobs", "side", "out"):
            net.add_place(Place(name))
        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc(place, count=count) for place, count in lead]
                + [InputArc("jobs", join_key=by_name[name]) for name in functions],
                outputs=[OutputArc("out", count=len(functions) + sum(count for _, count in lead))],
                action=_passthrough,
                guard=lambda tokens: sum(token.payload["n"] for token in tokens) % 3 != 1,
            )
        )
        net.binding_search_limit = limit
        exhausted = _record_exhaustion(net)
        for n, (payload, at) in enumerate(payloads):
            net.deposit("jobs", Token(payload={**payload, "n": n}, **({} if at is None else {"available_at": at})))
        for n in range(side):
            net.deposit("side", Token(payload={"f": 0, "g": 0, "h": 0, "n": 100 + n}))
        return net, exhausted

    def ns(binding):
        return None if binding is None else [token.payload["n"] for _, tokens in binding for token in tokens]

    nets = [build(_FUNCTIONS), build(fallback)]
    added = itertools.count(len(payloads))
    for at, ignore_timing, change in probes:
        _apply_change(change, [net for net, _ in nets], added)
        seen = []
        for net, exhausted in nets:
            t = net.transitions["t"]
            exists = net._binding_exists(t, at, ignore_timing=ignore_timing)
            binding = ns(net._resolve_binding(t, at, ignore_timing=ignore_timing))
            seen.append((exists, binding, len(exhausted)))
        assert seen[0] == seen[1]
        # The certified net searches through the match, the other through the fallback.
        assert "t" not in nets[1][0]._join_matches
        if seen[0][0]:
            assert "t" in nets[0][0]._join_matches
    for net, _ in nets:
        net.drive_to_quiescence()
    assert [token.payload["n"] for token in nets[0][0].marking["out"]] == [
        token.payload["n"] for token in nets[1][0].marking["out"]
    ]
    assert len(nets[0][1]) == len(nets[1][1])


_STEPS = st.lists(
    st.one_of(
        st.tuples(
            st.just("add"),
            st.fixed_dictionaries({"f": _values, "g": _values, "h": _values}),
            st.sampled_from([None, 1.0, 3.0]),
        ),
        st.tuples(st.just("remove"), st.integers(0, 50)),
        st.tuples(st.just("probe"), st.sampled_from([0.0, 1.0, 2.0, 3.0, 5.0, None])),
    ),
    max_size=30,
)


def _expected_keys(functions: list[str], tokens: list[Token], t_limit: float) -> set:
    """By brute force: the keys for which the tokens available at `t_limit` give each arc its own."""
    available = [token.payload for token in tokens if token.available_at <= t_limit]
    keys = {payload[name] for payload in available for name in functions}
    return {key for key in keys if _assign(functions, available, key, set())}


def _expected_bound(net: PetriNet, function: str, key: object) -> tuple:
    """By brute force: the first entry of `key`'s group under `function`, in the index's read order."""
    store = net.places["jobs"]._store
    entries = [
        (0, 0.0, store._seq_of[id(token)])
        if token.available_at == 0.0
        else (1, token.available_at, store._seq_of[id(token)])
        for token in net.places["jobs"].peek(len(net.places["jobs"]), model_time=math.inf)
        if _FUNCTIONS[function](token) == key
    ]
    return min(entries)


@settings(max_examples=300, deadline=None)
@given(functions=st.lists(st.sampled_from(sorted(_FUNCTIONS)), min_size=2, max_size=3), steps=_STEPS)
def test_the_match_lists_exactly_the_keys_that_bind_by_their_bound(functions, steps):
    # Fuzz the match's own state as tokens come and go between probes, at times that move back
    # and forth and at the timing-ignoring horizon: after each probe, the keys listed by when
    # they bind (`iter_keys_at`) and the view firing reads (`ordered_keys`) must hold exactly the
    # keys that bind by brute force, the view sorted by each key's first entry under the first
    # arc's function.
    net = PetriNet(seed=0)
    for name in ("jobs", "out"):
        net.add_place(Place(name))
    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("jobs", join_key=_FUNCTIONS[name]) for name in functions],
            outputs=[OutputArc("out", count=len(functions))],
            action=_passthrough,
        )
    )
    t = net.transitions["t"]
    for i in range(len(functions)):  # enough tokens that the first probe builds the match, no key shared
        net.deposit("jobs", Token(payload={"f": 10 + i, "g": 20 + i, "h": 30 + i}))
    assert not net._binding_exists(t, 0.0)
    match = net._join_matches["t"]
    tokens = list(net.places["jobs"].peek(len(net.places["jobs"]), model_time=math.inf))
    for step in steps:
        if step[0] == "add":
            token = Token(payload=dict(step[1]), **({} if step[2] is None else {"available_at": step[2]}))
            net.deposit("jobs", token)
            tokens.append(token)
            continue
        if step[0] == "remove":
            if tokens:
                net.places["jobs"].retrieve_specific([tokens.pop(step[1] % len(tokens))])
            continue
        horizon = step[1] is None
        at = engine._LAST_FINITE_TIME if horizon else step[1]
        net._binding_exists(t, 0.0 if horizon else at, ignore_timing=horizon)
        assert net._join_matches.get("t") is match
        assert match.refresh()
        expected = _expected_keys(functions, tokens, at)
        assert set(match.iter_keys_at(at)) == expected
        ordered = match.ordered_keys(at)
        assert ordered == sorted(ordered)
        listed = {match.key_of(uid): first for first, uid in ordered}
        assert set(listed) == expected and len(listed) == len(ordered)
        for key, first in listed.items():
            assert first == _expected_bound(net, functions[0], key)


_ARC_SHAPES = {
    "plain": {},
    "even": {"filter": lambda t: t.payload["n"] % 2 == 0},
    "odd": {"filter": lambda t: t.payload["n"] % 2 == 1},
    "desc": {"key": NEG_N},
}


_DEAD = "dead end"


def _oracle_units(arcs: list[tuple[str, int]], ns: list[int]) -> list:
    """The distinct search's items, by brute force: candidates (lists of token indexes) and dead ends.

    Arcs in order, each option in its arc's order (`plain` and filters FIFO, `desc` by
    descending n, ties FIFO), the last arc varying fastest. An arc after the first on the place
    chooses among the tokens the earlier arcs left; an option of an arc under which the next arc
    has no option is one dead end. An arc with no option even on the full pool empties the
    product. No truncation: the engine's truncation must be loss-free.
    """

    def order(shape: str, free: list[int]) -> list[int]:
        if shape == "desc":
            return sorted(free, key=lambda j: -ns[j])
        if shape in ("even", "odd"):
            return [j for j in free if (ns[j] % 2 == 0) == (shape == "even")]
        return free

    def options(i: int, used: frozenset[int]) -> list[tuple[int, ...]]:
        shape, count = arcs[i]
        free = [j for j in range(len(ns)) if j not in used]
        return list(itertools.combinations(order(shape, free), count))

    units: list = []
    if any(not options(i, frozenset()) for i in range(len(arcs))):
        return units  # an arc with no option at all: the product is empty, as in `_lazy_product`

    def walk(i: int, used: frozenset[int], chosen: list[tuple[int, ...]]) -> None:
        for option in options(i, used):
            if i == len(arcs) - 1:
                units.append([j for group in [*chosen, option] for j in group])
                continue
            after = used | set(option)
            if not options(i + 1, after):
                units.append(_DEAD)
                continue
            walk(i + 1, after, [*chosen, option])

    walk(0, frozenset(), [])
    return units


@settings(max_examples=300, deadline=None)
@given(
    arcs=st.lists(st.tuples(st.sampled_from(sorted(_ARC_SHAPES)), st.integers(1, 2)), min_size=2, max_size=3),
    ns=st.lists(st.integers(0, 9), max_size=6),
    limit=st.integers(1, 6),
    guarded=st.booleans(),
)
def test_a_shared_place_search_matches_a_brute_force_oracle(arcs, ns, limit, guarded):
    # The search examines the oracle's items in order, `binding_search_limit` at most: FIRST
    # fires the first satisfying candidate among them, and with none it fires nothing and
    # signals exhaustion exactly when the oracle holds more items than the limit. Small limits,
    # `count=2`, `key` and `filter` make the per-arc truncation matter, so cutting options
    # before leaving out the taken tokens fails this.
    guard = (lambda tokens: sum(t.payload["n"] for t in tokens) % 2 == 0) if guarded else None
    net = PetriNet(seed=0)
    for name in ("jobs", "out"):
        net.add_place(Place(name))
    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("jobs", count=count, **_ARC_SHAPES[shape]) for shape, count in arcs],
            outputs=[OutputArc("out", count=sum(count for _, count in arcs))],
            action=_passthrough,
            **({"guard": guard} if guard else {}),
        )
    )
    net.binding_search_limit = limit
    exhausted = []
    signal = net._signal_search_exhausted
    net._signal_search_exhausted = lambda name: (exhausted.append(name), signal(name))  # type: ignore[method-assign]
    tokens = [Token(payload={"n": n, "j": j}) for j, n in enumerate(ns)]
    for token in tokens:
        net.deposit("jobs", token)
    t = net.transitions["t"]
    units = _oracle_units(arcs, ns)
    accept = guard or (lambda tokens: True)
    within = units[:limit]
    expected = next((unit for unit in within if unit is not _DEAD and accept([tokens[j] for j in unit])), None)
    head_only = net._is_head_only(t, net._effective_policy(t))
    binding = net._resolve_binding(t, net.model_time)
    if head_only:
        # Greedy heads, no search and no limit: binds whenever any distinct binding exists.
        assert (binding is not None) is any(unit is not _DEAD for unit in units)
        return
    got = None if binding is None else [token.payload["j"] for _, toks in binding for token in toks]
    assert got == expected
    assert exhausted == (["t"] if expected is None and len(units) > limit else [])
    assert net._binding_exists(t, net.model_time) is (expected is not None)


@pytest.mark.parametrize("guarded", [False, True])
def test_a_timed_self_join_waits_for_its_second_token(guarded):
    # The self-join's index keys availability on each key's second token (its depth), so one
    # ready and one cooling token of a key leave the transition disabled until the cooling one
    # is ready, and the drive then advances to that time and fires.
    net = PetriNet(seed=0)
    for name in ("jobs", "out"):
        net.add_place(Place(name))
    same = InputArc("jobs", join_key=WID)
    guard = {"guard": lambda tokens: True} if guarded else {}
    net.add_transition(
        Transition(name="t", inputs=[same, same], outputs=[OutputArc("out", count=2)], action=_passthrough, **guard)
    )
    net.advance_time(1.0)
    ready_at = net.model_time + 5.0
    net.deposit("jobs", Token(payload={"wid": 7}))
    net.deposit("jobs", Token(payload={"wid": 8}))
    net.deposit("jobs", Token(payload={"wid": 7}, available_at=ready_at))
    t = net.transitions["t"]
    assert not net._binding_exists(t, net.model_time)
    assert net._resolve_binding(t, net.model_time) is None
    assert net._binding_exists(t, net.model_time, ignore_timing=True)
    net.drive_to_quiescence()
    assert net.model_time >= ready_at
    assert [token.payload["wid"] for token in net.marking["out"]] == [7, 7]
    assert [token.payload["wid"] for token in net.marking["jobs"]] == [8]


@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("policy", _POLICIES)
def test_two_plain_arcs_on_one_place_wait_for_a_cooling_second_token(guarded, policy):
    # One ready and one cooling token do not enable `[p, p]`: only ready tokens are consumed,
    # on the head-only path (guard-free FIRST) and in the search alike. Ignoring timing it is
    # enabled, and the drive advances to the cooling token's time and fires then.
    guard = {"guard": lambda tokens: True} if guarded else {}
    net = _net_moving([InputArc("jobs"), InputArc("jobs")], n_tokens=0, binding_policy=policy, **guard)
    net.advance_time(1.0)
    ready_at = net.model_time + 5.0
    net.deposit("jobs", Token(payload={"n": 0}))
    net.deposit("jobs", Token(payload={"n": 1}, available_at=ready_at))
    t = net.transitions["t"]
    assert not net._binding_exists(t, net.model_time)
    assert net._resolve_binding(t, net.model_time) is None
    assert net._binding_exists(t, net.model_time, ignore_timing=True)
    assert net.step() is False
    net.drive_to_quiescence()
    assert net.model_time == ready_at
    assert sorted(token.payload["n"] for token in net.marking["out"]) == [0, 1]
    assert len(net.marking["jobs"]) == 0


def test_a_timed_self_join_binds_on_its_first_two_ready_tokens_while_a_third_cools():
    # The key's head is its first `depth` tokens, not all of them: a cooling third token must
    # not hide the pair that is ready now from the match's one-key probe.
    net = PetriNet(seed=0)
    for name in ("jobs", "out"):
        net.add_place(Place(name))
    same = InputArc("jobs", join_key=WID)
    net.add_transition(
        Transition(name="t", inputs=[same, same], outputs=[OutputArc("out", count=2)], action=_passthrough)
    )
    net.advance_time(1.0)
    net.deposit("jobs", Token(payload={"wid": 7, "n": 0}))
    net.deposit("jobs", Token(payload={"wid": 7, "n": 1}))
    net.deposit("jobs", Token(payload={"wid": 7, "n": 2}, available_at=net.model_time + 5.0))
    t = net.transitions["t"]
    assert [net._binding_exists(t, net.model_time) for _ in range(2)] == [True, True]
    binding = net._resolve_binding(t, net.model_time)
    assert sorted(tokens[0].payload["n"] for _, tokens in binding) == [0, 1]


@pytest.mark.parametrize("depth", [0, -1, 1.5, True, "2"])
def test_a_join_index_depth_must_be_a_positive_int(depth):
    with pytest.raises(ValueError, match="depth"):
        Place("jobs").register_join_index(1, WID, depth)
    with pytest.raises(ValueError, match="depth"):
        _TokenStore().register_join_index(1, WID, depth)


@pytest.mark.parametrize("guarded", [False, True])
def test_one_token_instance_in_two_shared_places_is_told_apart_per_place(guarded):
    # The same Token object sits in p and in q. Taking it from p must not hide it in q: the
    # binding p:(x, a), q:(b, x) exists.
    net = PetriNet(seed=0)
    for name in ("p", "q", "out"):
        net.add_place(Place(name))
    x = Token(payload={"n": 0})
    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("p"), InputArc("p"), InputArc("q", filter=lambda t: t is not x), InputArc("q")],
            outputs=[OutputArc("out")],
            action=lambda tokens: tokens[:1],
            **({"guard": lambda tokens: True} if guarded else {}),
        )
    )
    net.deposit("p", x)
    net.deposit("p", Token(payload={"n": 1}))
    net.deposit("q", Token(payload={"n": 2}))
    net.deposit("q", x)
    t = net.transitions["t"]
    assert net._binding_exists(t, net.model_time)
    binding = net._resolve_binding(t, net.model_time)
    assert [tokens[0].payload["n"] for _, tokens in binding] == [0, 1, 2, 0]


def test_a_wide_shared_place_search_does_not_recurse_per_arc():
    # 1 200 arcs on one place with a guard: one stack frame per arc would pass Python's
    # recursion limit.
    width = 1_200
    net = PetriNet(seed=0)
    for name in ("jobs", "out"):
        net.add_place(Place(name))
    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("jobs") for _ in range(width)],
            outputs=[OutputArc("out")],
            action=lambda tokens: tokens[:1],
            guard=lambda tokens: True,
        )
    )
    for n in range(width):
        net.deposit("jobs", Token(payload={"n": n}))
    t = net.transitions["t"]
    assert net._binding_exists(t, net.model_time)


@pytest.mark.parametrize("arcs", [2, 3, 4])
@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("policy", [BindingPolicy.FIRST, BindingPolicy.RANDOM, BindingPolicy.PRIORITY])
def test_a_self_join_with_a_deep_key_binds_whatever_its_width(arcs, guarded, policy):
    # One key holds more tokens than the search limit. A key's product repeats its group once
    # per arc, so its first tokens' self-pairings would use up the limit unless skipped unbuilt.
    net = PetriNet(seed=0)
    for name in ("jobs", "out"):
        net.add_place(Place(name))
    same = InputArc("jobs", join_key=WID)
    net.add_transition(
        Transition(
            name="t",
            inputs=[same] * arcs,
            outputs=[OutputArc("out", count=arcs)],
            action=_passthrough,
            binding_policy=policy,
            **({"guard": lambda tokens: True} if guarded else {}),
        )
    )
    net.binding_search_limit = 20
    exhausted = _record_exhaustion(net)
    for n in range(net.binding_search_limit + 30):
        net.deposit("jobs", Token(payload={"wid": 1, "n": n}))
    t = net.transitions["t"]
    assert net._binding_exists(t, net.model_time)
    _assert_distinct(net)
    # RANDOM and PRIORITY scan the bounded product, so they signal its truncation by design.
    assert exhausted == ([] if policy is BindingPolicy.FIRST else ["t"])
    net.drive_to_quiescence()
    assert len(net.marking["out"]) == arcs * ((net.binding_search_limit + 30) // arcs)


def test_hall_pair_check_fails_a_group_whose_length_outruns_its_tokens():
    # An index group read while the place changed without the engine lock can report a
    # length its iteration does not reach. The check must fail that key, not raise.
    from cpnx.engine import _pair_has_distinct_tokens

    class Stale(list):
        def __len__(self):
            return 1

    token = Token(payload={})
    assert not _pair_has_distinct_tokens(1, Stale(), 1, [(0, token)])
    assert not _pair_has_distinct_tokens(1, [(0, token)], 1, [(1, token)])
    assert _pair_has_distinct_tokens(1, [(0, token)], 1, [(1, Token(payload={}))])


def test_a_join_group_that_empties_mid_walk_is_a_dead_end_not_an_error():
    # `[p, p(filter), a:k, b:k]`: the first arc takes the token the filter needs, a dead end,
    # padded with each later arc's first position. A group emptied by a change to its place
    # outside the engine has no first position; the walk must still report the dead end.
    from cpnx.engine import _DeadPrefix, _join_plan, _key_combos, _Ordered, _shared_places

    x, y = Token(payload={"n": 0}), Token(payload={"n": 1})
    inputs = [InputArc("p"), InputArc("p", filter=_is_zero), InputArc("a", join_key=WID), InputArc("b", join_key=WID)]
    transition = Transition(name="t", inputs=inputs, outputs=[], action=_passthrough)
    plan = _join_plan(transition, _shared_places(transition), [_Ordered([], 1), _Ordered([], 1), None, None])
    lists = [_Ordered([x, y], 1), _Ordered([x], 1), [], [(0, Token(payload={"wid": 1}))]]
    walked = list(_key_combos(lists, plan.levels, 5))
    assert type(walked[0]) is _DeadPrefix  # x for the first arc leaves the filtered arc nothing
    assert [type(item) for item in walked] == [_DeadPrefix]  # y then x reaches the emptied group: no candidate


@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("policy", [BindingPolicy.FIRST, BindingPolicy.RANDOM, BindingPolicy.PRIORITY])
@pytest.mark.parametrize("in_c", [1, 2, 3])
def test_two_plain_arcs_on_one_place_of_a_join_take_their_own_tokens(guarded, policy, in_c):
    # The join reads a and b; c is read by two arcs without a `join_key`. Their options are
    # built independently, so a candidate handing c's one token to both must be skipped.
    net = PetriNet(seed=0)
    for name in ("a", "b", "c", "out"):
        net.add_place(Place(name))
    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("a", join_key=WID), InputArc("b", join_key=WID), InputArc("c"), InputArc("c")],
            outputs=[OutputArc("out", count=4)],
            action=_passthrough,
            binding_policy=policy,
            **({"guard": lambda tokens: True} if guarded else {}),
        )
    )
    net.deposit("a", Token(payload={"wid": 1}))
    net.deposit("b", Token(payload={"wid": 1}))
    for n in range(in_c):
        net.deposit("c", Token(payload={"n": n}))
    t = net.transitions["t"]
    enabled = in_c >= 2
    assert net._binding_exists(t, net.model_time) is enabled
    if enabled:
        _assert_distinct(net)
    else:
        assert net._resolve_binding(t, net.model_time) is None
    net.drive_to_quiescence()
    assert len(net.marking["out"]) == (4 if enabled else 0)
    assert len(net.marking["c"]) == in_c - (2 if enabled else 0)


@pytest.mark.parametrize(
    ("arcs", "kinds", "shortcut", "drained", "split"),
    [
        (["a:k", "b:k", "c", "c"], ["free", "free", "plain", "plain"], True, set(), 2),
        (["c", "a:k", "b:k", "c"], ["plain", "free", "free", "plain"], True, set(), 4),
        (["c", "a:k", "b:k", "c?"], ["plain", "free", "free", "plain"], False, set(), 4),
        (["c?", "a:k", "b:k", "c"], ["plain", "free", "free", "plain"], False, set(), 4),
        (["a:k", "c", "b:k", "c?"], ["free", "plain", "free", "plain"], False, set(), 4),
        (["a:k", "b:k", "c", "c?"], ["free", "free", "plain", "plain"], True, set(), 2),
        (["d?", "a:k", "b:k", "c", "c"], ["free", "free", "free", "plain", "plain"], True, set(), 3),
        (["a:k", "a:k"], ["join", "join"], True, set(), 2),
        (["a:k", "a:k", "a"], ["join", "join", "plain"], False, set(), 3),
        (["a", "a:k", "b:k"], ["plain", "join", "free"], False, set(), 3),
        (["a:k", "a:g"], ["join", "join"], True, set(), 2),
        (["a:k", "b:k", "c", "c*"], ["free", "free", "plain", "free"], True, {"c"}, 2),
        (["a:k", "b:k", "c", "c", "d"], ["free", "free", "plain", "plain", "free"], True, set(), 2),
        (["a:k", "b:k"], ["free", "free"], True, set(), 2),
    ],
)
def test_join_plan_levels_shortcut_and_drained_places(arcs, kinds, shortcut, drained, split):
    # Join arcs on a shared place and plain arcs there are enumerated without reusing a token;
    # only a consume_all arc keeps the whole pool, so its place is listed as drained, where it
    # is later given what the other arcs leave. The probe shortcut needs no plain arc on a
    # shared place to read a join place, and none ahead of a join arc on a place with a filter
    # (`c?`), where its dead ends would repeat per key. Trailing plain arcs off the key's
    # places are walked once for every key.
    from cpnx.engine import _join_plan, _Ordered, _shared_places

    functions = {"k": WID, "g": N}
    inputs = []
    for spec in arcs:
        drain = spec.endswith("*")
        place, _, fn = spec.rstrip("*?").partition(":")
        if fn:
            inputs.append(InputArc(place, join_key=functions[fn]))
        else:
            inputs.append(InputArc(place, consume_all=drain, filter=_is_zero if spec.endswith("?") else None))
    transition = Transition(name="t", inputs=inputs, outputs=[], action=_passthrough)
    shared = _shared_places(transition)
    # What _join_arc_options reads: None for a join arc, ordered tokens for a plain arc on a
    # shared place (not consume_all), a list of options otherwise.
    options = [
        None
        if arc.join_key is not None
        else _Ordered([], arc.count)
        if arc.place in shared and not arc.consume_all
        else [(0, [])]
        for arc in inputs
    ]
    plan = _join_plan(transition, shared, options)
    assert [level.kind for level in plan.levels] == kinds
    assert plan.shortcut is shortcut
    assert plan.drained == drained
    assert plan.split == (split if plan.distinct else len(arcs))


# -- every arc shape together, against brute force ---------------------------------------------

_JF = lambda t: t.payload["f"]  # noqa: E731
_JG = lambda t: t.payload["g"]  # noqa: E731
_EVEN = lambda t: t.payload["n"] % 2 == 0  # noqa: E731

_KINDS = {
    "plain": {},
    "even": {"filter": _EVEN},
    "desc": {"key": NEG_N},
    "pair": {"count": 2},
    "join_f": {"join_key": _JF},
    "join_g": {"join_key": _JG},
    "all": {"consume_all": True},
}
_JOIN_FN = {"join_f": _JF, "join_g": _JG}
_SIZED_KINDS = sorted(kind for kind in _KINDS if kind != "all")  # each takes a fixed number of tokens


def _moved_by(arcs: list[tuple[str, str]], marking: dict[str, list[Token]]) -> int:
    """How many tokens a firing of `arcs` removes: a drained place empties, the others lose their demand."""
    drained = {place for place, kind in arcs if kind == "all"}
    fixed = sum(2 if kind == "pair" else 1 for place, kind in arcs if place not in drained)
    return fixed + sum(len(marking[place]) for place in drained)


def _arc_accepts(kind: str, token: Token) -> bool:
    return kind != "even" or _EVEN(token)


def _distinct_binding_exists(arcs: list[tuple[str, str]], marking: dict[str, list[Token]], guard) -> bool:
    """Brute force: tokens per arc, none taken twice from one place, filters and the join key agreeing.

    A draining arc needs a token in its place, takes what the other arcs leave there, and
    cannot share its place with another draining arc.
    """
    drains = [place for place, kind in arcs if kind == "all"]
    if len(set(drains)) < len(drains) or any(not marking[place] for place in drains):
        return False

    def flat(chosen: list, used: frozenset[tuple[str, int]]) -> list[Token]:
        tokens: list[Token] = []
        for (place, _), group in zip(arcs, chosen, strict=True):
            tokens += [t for t in marking[place] if (place, id(t)) not in used] if group is None else group
        return tokens

    def go(i: int, used: frozenset[tuple[str, int]], join_value: object, chosen: list) -> bool:
        if i == len(arcs):
            return guard(flat(chosen, used))
        place, kind = arcs[i]
        if kind == "all":
            return go(i + 1, used, join_value, [*chosen, None])
        free = [t for t in marking[place] if (place, id(t)) not in used and _arc_accepts(kind, t)]
        for group in itertools.combinations(free, 2 if kind == "pair" else 1):
            value = join_value
            if kind in _JOIN_FN:
                value = _JOIN_FN[kind](group[0])
                if join_value is not _UNSET and value != join_value:
                    continue
            if go(i + 1, used | {(place, id(t)) for t in group}, value, [*chosen, list(group)]):
                return True
        return False

    return go(0, frozenset(), _UNSET, [])


_UNSET = object()


def _assert_valid_binding(binding, arcs, marking, guard) -> None:
    taken: set[tuple[str, int]] = set()
    join_values = set()
    flat = []
    for (arc, tokens), (place, kind) in zip(binding, arcs, strict=True):
        assert arc.place == place
        if tokens is _DRAIN:  # the count-only path: what the others leave, read when it fires
            others = {id(t) for a, ts in binding if a.place == place and ts is not _DRAIN for t in ts}
            tokens = [t for t in marking[place] if id(t) not in others]
        if kind != "all":
            assert len(tokens) == (2 if kind == "pair" else 1)
        for token in tokens:
            assert any(token is t for t in marking[place])
            assert _arc_accepts(kind, token)
            assert (place, id(token)) not in taken, "a token handed to two arcs"
            taken.add((place, id(token)))
        if kind in _JOIN_FN:
            join_values.add(_JOIN_FN[kind](tokens[0]))
        flat.extend(tokens)
    assert len(join_values) <= 1
    for place in {place for place, kind in arcs if kind == "all"}:  # a drained place is emptied
        assert sum(1 for taken_place, _ in taken if taken_place == place) == len(marking[place])
    assert guard(flat)


_token_payloads = st.fixed_dictionaries({"n": st.integers(0, 3), "f": st.integers(0, 2), "g": st.integers(0, 2)})


@settings(max_examples=400, deadline=None)
@example(  # two plain arcs on one place of a join: the probe's one-key shortcut and firing disagreed
    arcs=[("p", "desc"), ("p", "desc"), ("q", "join_f")],
    payloads={"p": [{"n": 0, "f": 0, "g": 0}] * 3, "q": [{"n": 0, "f": 0, "g": 0}, {"n": 0, "f": 1, "g": 0}]},
    guarded=False,
    policy=BindingPolicy.FIRST,
    small_limit=2,
)
@example(  # one token for two plain arcs beside a join: it was handed to both (issue #75 reopened)
    arcs=[("p", "join_f"), ("q", "plain"), ("q", "plain")],
    payloads={"p": [{"n": 0, "f": 0, "g": 0}], "q": [{"n": 0, "f": 0, "g": 0}]},
    guarded=False,
    policy=BindingPolicy.FIRST,
    small_limit=4,
)
@given(
    arcs=st.lists(st.tuples(st.sampled_from(["p", "q"]), st.sampled_from(sorted(_KINDS))), min_size=2, max_size=4),
    payloads=st.fixed_dictionaries(
        {"p": st.lists(_token_payloads, max_size=4), "q": st.lists(_token_payloads, max_size=4)}
    ),
    guarded=st.booleans(),
    policy=st.sampled_from([BindingPolicy.FIRST, BindingPolicy.RANDOM, BindingPolicy.PRIORITY]),
    small_limit=st.integers(1, 4),
)
def test_every_arc_shape_binds_exactly_when_distinct_tokens_exist(arcs, payloads, guarded, policy, small_limit):
    # Plain, filtered, keyed, count=2, draining and join arcs (two join_key functions) over two places,
    # with a guard or not, under every policy. With a limit too large to run out, the engine
    # binds exactly when brute force finds distinct tokens, its binding is valid, and firing
    # it moves those tokens. With a small limit, a miss must be signalled as exhaustion.
    guard = (lambda tokens: sum(t.payload["n"] for t in tokens) % 2 == 0) if guarded else (lambda tokens: True)

    def build(limit: int) -> tuple[PetriNet, dict[str, list[Token]], list]:
        net = PetriNet(seed=0)
        for name in ("p", "q", "out"):
            net.add_place(Place(name))
        transition = Transition(
            name="t",
            inputs=[InputArc(place, **_KINDS[kind]) for place, kind in arcs],
            outputs=[],
            action=lambda tokens: [],
            binding_policy=policy,
            **({"guard": guard} if guarded else {}),
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # two draining arcs on one place warn; here they are a case
            net.add_transition(transition)
        net.binding_search_limit = limit
        exhausted: list[str] = []
        signal = net._signal_search_exhausted
        net._signal_search_exhausted = lambda name: (exhausted.append(name), signal(name))  # type: ignore[method-assign]
        marking = {place: [Token(payload=dict(p)) for p in payloads[place]] for place in ("p", "q")}
        for place, tokens in marking.items():
            for token in tokens:
                net.deposit(place, token)
        return net, marking, exhausted

    expected = None
    net, marking, _ = build(100_000)
    expected = _distinct_binding_exists(arcs, marking, guard)
    t = net.transitions["t"]
    assert net._binding_exists(t, net.model_time) is expected
    binding = net._resolve_binding(t, net.model_time)
    assert (binding is not None) is expected
    if binding is not None:
        _assert_valid_binding(binding, arcs, marking, guard)
        before = {place: len(net.marking[place]) for place in ("p", "q")}
        net.step()
        moved = sum(before.values()) - len(net.marking["p"]) - len(net.marking["q"])
        assert moved == _moved_by(arcs, marking)

    net, marking, exhausted = build(small_limit)
    t = net.transitions["t"]
    binding = net._resolve_binding(t, net.model_time)
    assert net._binding_exists(t, net.model_time) is (binding is not None)
    if binding is not None:
        _assert_valid_binding(binding, arcs, marking, guard)
        net.step()
    elif expected:
        assert exhausted, "a distinct binding exists, yet the search neither found it nor ran out"


@pytest.mark.parametrize("guarded", [False, True])
def test_a_mixed_join_place_with_plain_arcs_between_skips_a_token_the_last_arc_needs(guarded):
    # x carries key 0 under both functions and is read first; y carries it under f only. Taking
    # x for the first arc leaves the last none, so every choice of the plain arcs between would
    # be a dead end, 1 000 x 1 000 of them. The residual Hall check skips x instead.
    f = lambda t: t.payload["f"]  # noqa: E731
    g = lambda t: t.payload["g"]  # noqa: E731
    net = PetriNet(seed=0)
    for name in ("p", "q", "r", "out"):
        net.add_place(Place(name))
    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("p", join_key=f), InputArc("q"), InputArc("r"), InputArc("p", join_key=g)],
            outputs=[OutputArc("out")],
            action=lambda tokens: tokens[:1],
            **({"guard": lambda tokens: True} if guarded else {}),
        )
    )
    exhausted = _record_exhaustion(net)
    net.deposit("p", Token(payload={"f": 0, "g": 0, "name": "x"}))
    net.deposit("p", Token(payload={"f": 0, "g": 7, "name": "y"}))
    for n in range(1_000):
        net.deposit("q", Token(payload={"n": n}))
        net.deposit("r", Token(payload={"n": n}))
    t = net.transitions["t"]
    started = time.perf_counter()
    assert net._binding_exists(t, net.model_time)
    binding = net._resolve_binding(t, net.model_time)
    assert time.perf_counter() - started < 2.0  # about 1 ms; the unbounded walk took seconds
    assert [tokens[0].payload["name"] for arc, tokens in binding if arc.place == "p"] == ["y", "x"]
    assert exhausted == []


def test_a_mixed_join_place_with_many_arcs_of_one_function_does_not_walk_their_orders():
    # One arc of f, then h of g. x, read first, carries key 0 under both; h - 1 more carry it
    # under g only, and y under f only. Taking x for f leaves h - 1 tokens for h arcs of g, which
    # a walk without the residual Hall check finds only after trying their (h - 1)! orders.
    f = lambda t: t.payload["f"]  # noqa: E731
    g = lambda t: t.payload["g"]  # noqa: E731
    width = 10
    net = PetriNet(seed=0)
    for name in ("p", "out"):
        net.add_place(Place(name))
    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("p", join_key=f)] + [InputArc("p", join_key=g) for _ in range(width)],
            outputs=[OutputArc("out")],
            action=lambda tokens: tokens[:1],
        )
    )
    net.binding_search_limit = 10
    exhausted = _record_exhaustion(net)
    net.deposit("p", Token(payload={"f": 0, "g": 0}))
    for _ in range(width - 1):
        net.deposit("p", Token(payload={"f": 9, "g": 0}))
    net.deposit("p", Token(payload={"f": 0, "g": 9}))
    t = net.transitions["t"]
    started = time.perf_counter()
    assert net._binding_exists(t, net.model_time)
    _assert_distinct(net)
    assert time.perf_counter() - started < 2.0  # about 1 ms; the unbounded walk took seconds
    assert exhausted == []


@pytest.mark.parametrize(
    "arcs",
    [
        ["a:k", "b:k", "p", "p2"],
        ["a:k", "b:k", "a", "a"],
        ["a", "a", "a:k", "b:k"],
    ],
)
@pytest.mark.parametrize("guarded", [False, True])
def test_plain_arcs_on_a_deep_shared_place_of_a_join_still_bind(arcs, guarded):
    # The place holds more tokens than the search limit. Each plain arc's options come from the
    # tokens the earlier arcs left, so the first token in every one of them cannot use up the
    # limit on candidates that reuse it.
    net = PetriNet(seed=0)
    for name in ("a", "b", "p", "out"):
        net.add_place(Place(name))
    inputs = []
    for spec in arcs:
        place, _, fn = spec.partition(":")
        if fn:
            inputs.append(InputArc(place, join_key=WID))
        elif place.endswith("2"):
            inputs.append(InputArc(place[:-1], count=2))
        else:
            inputs.append(InputArc(place))
    net.add_transition(
        Transition(
            name="t",
            inputs=inputs,
            outputs=[OutputArc("out")],
            action=lambda tokens: tokens[:1],
            **({"guard": lambda tokens: True} if guarded else {}),
        )
    )
    net.binding_search_limit = 20
    exhausted = _record_exhaustion(net)
    deep = net.binding_search_limit + 30
    net.deposit("b", Token(payload={"wid": 0}))
    for n in range(deep):
        net.deposit("a", Token(payload={"wid": 0, "n": n}))
        net.deposit("p", Token(payload={"n": n}))
    t = net.transitions["t"]
    assert net._binding_exists(t, net.model_time)
    _assert_distinct(net)
    assert exhausted == []
    before = sum(len(net.marking[name]) for name in ("a", "b", "p"))
    net.step()
    taken = sum(2 if spec == "p2" else 1 for spec in arcs)
    assert before - sum(len(net.marking[name]) for name in ("a", "b", "p")) == taken


_ALT = lambda t: t.payload["alt"]  # noqa: E731


def _deep_head_join(arcs: list[str], limit: int, **transition) -> tuple[PetriNet, list[str]]:
    """A join whose place `a` holds the matched key's only token at its head, `limit + 30` deep.

    `arcs` are specs: `"a"` a plain arc, `"a3"` one taking three tokens, `"a:k"` / `"a:alt"`
    a join arc keyed by `wid` / `alt`. Under `alt`, key 0's only token is the last in `a`.
    """
    net = PetriNet(seed=0)
    for name in ("a", "b", "out"):
        net.add_place(Place(name))
    inputs = []
    for spec in arcs:
        place, _, fn = spec.partition(":")
        if fn:
            inputs.append(InputArc(place, join_key=WID if fn == "k" else _ALT))
        else:
            inputs.append(InputArc(place[0], count=int(place[1:] or 1)))
    net.add_transition(
        Transition(name="t", inputs=inputs, outputs=[OutputArc("out")], action=lambda tokens: tokens[:1], **transition)
    )
    net.binding_search_limit = limit
    exhausted = _record_exhaustion(net)
    net.deposit("a", Token(payload={"wid": 0, "alt": 1, "n": -1}))
    for n in range(limit + 30):
        net.deposit("a", Token(payload={"wid": 100 + n, "alt": 100 + n, "n": n}))
    net.deposit("a", Token(payload={"wid": 50, "alt": 0, "n": 999}))
    net.deposit("b", Token(payload={"wid": 0}))
    return net, exhausted


def _taken_by(spec: str) -> int:
    place = spec.partition(":")[0]
    return int(place[1:] or 1)


@pytest.mark.parametrize(
    "arcs",
    [
        ["a", "a", "a:k", "b:k"],
        ["a2", "a:k", "b:k"],
        ["a", "a", "a:k", "a", "b:k"],
        ["a", "a", "a:k", "a:alt", "b:k"],  # only the check over both functions keeps the head
        ["a", "a:alt", "a", "a:k", "b:k"],
    ],
)
@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("policy", [BindingPolicy.FIRST, BindingPolicy.RANDOM])
@pytest.mark.parametrize("limit", [1, 20])
def test_a_plain_arc_before_a_join_arc_skips_the_token_the_key_needs(arcs, guarded, policy, limit):
    # The head of a deep place is the matched key's only token there. Plain arcs read the place
    # before the join arc, so every option that takes the head strands the join arc: more than
    # the search limit of them, ahead of the first binding. The residual Hall check keeps plain
    # arcs off the head, so the search binds without counting them. Each shape has two plain
    # choices ahead of a join arc, so without that check it would signal exhaustion instead.
    # At a limit of 1 the very first candidate must bind: not one dead end may come before it.
    net, exhausted = _deep_head_join(
        arcs, limit, binding_policy=policy, **({"guard": lambda tokens: True} if guarded else {})
    )
    t = net.transitions["t"]
    assert net._binding_exists(t, net.model_time)
    binding = net._resolve_binding(t, net.model_time)
    assert binding is not None
    _assert_distinct(net)
    if policy is BindingPolicy.FIRST:  # RANDOM samples from more candidates than the limit, and says so
        assert exhausted == []
    taken = {id(token) for _, tokens in binding for token in tokens}
    assert len(taken) == sum(len(tokens) for _, tokens in binding)
    before = len(net.marking["a"]) + len(net.marking["b"])
    net.step()
    assert before - len(net.marking["a"]) - len(net.marking["b"]) == sum(_taken_by(spec) for spec in arcs)


@pytest.mark.parametrize("count", [3, 4])
def test_a_plain_arc_taking_several_tokens_before_a_join_arc_reads_only_valid_options(count, monkeypatch):
    # At the default limit, the combinations holding the head number C(1 030, count - 1): the
    # search must step over them, not read and discard them, or it holds the lock for minutes.
    # So count the tokens the walk considers: the head once, then the tokens of its one option.
    from cpnx import engine

    considered = []
    admits = engine._admits

    def counting(token_id, *rest):
        considered.append(token_id)
        return admits(token_id, *rest)

    monkeypatch.setattr(engine, "_admits", counting)
    net, exhausted = _deep_head_join([f"a{count}", "a:k", "b:k"], PetriNet(seed=0).binding_search_limit)
    t = net.transitions["t"]
    assert net._binding_exists(t, net.model_time)
    assert len(considered) == count + 1
    binding = net._resolve_binding(t, net.model_time)
    assert len(considered) == 2 * (count + 1)
    assert binding is not None
    assert [token.payload["n"] for token in binding[0][1]] == list(range(count))
    assert exhausted == []


def test_a_join_arc_leaves_a_later_function_every_token_it_needs():
    # Key 0's f-group is [t0, t1] and its g-group [t0, t2], with two g arcs after the f arc.
    # Taking t0 for f leaves g one token for two arcs: the residual check skips it, so the
    # first candidate binds within a limit of 1, where a dead end there would use it up.
    net = PetriNet(seed=0)
    net.add_place(Place("p"))
    inputs = [InputArc("p", join_key=_JF), InputArc("p", join_key=_JG), InputArc("p", join_key=_JG)]
    net.add_transition(Transition(name="t", inputs=inputs, outputs=[], action=_passthrough, guard=lambda t: True))
    net.binding_search_limit = 1
    exhausted = _record_exhaustion(net)
    t0 = Token(payload={"f": 0, "g": 0})
    t1 = Token(payload={"f": 0, "g": 9})
    t2 = Token(payload={"f": 8, "g": 0})
    for token in (t0, t1, t2):
        net.deposit("p", token)
    binding = net._resolve_binding(net.transitions["t"], net.model_time)
    assert binding is not None
    assert [tokens[0] for _, tokens in binding] == [t1, t0, t2]
    assert exhausted == []


@pytest.mark.parametrize("limit", [5, 1_000])
@pytest.mark.parametrize("c_first", [False, True])
@pytest.mark.parametrize("then_d", [False, True])
def test_dead_ends_count_toward_the_search_limit(limit, c_first, then_d):
    # One token in `c` for two arcs: under every key the second arc on `c` has nothing, a dead
    # end. They count toward the limit, so 30 keys' worth of them run it out at 5 and are
    # signalled, while a limit above them settles that no binding exists. With both arcs on
    # `c` last, their walk is shared by every key; with one first, each key walks it. A last
    # arc on `d` is then the shared walk, and each key's dead ends come from its own.
    net = PetriNet(seed=0)
    for name in ("a", "b", "c", "d"):
        net.add_place(Place(name))
    inputs = [InputArc("a", join_key=WID), InputArc("b", join_key=WID), InputArc("c")]
    inputs.insert(0 if c_first else 3, InputArc("c"))
    if then_d:
        inputs.append(InputArc("d"))
    net.deposit("d", Token(payload={"wid": -2}))
    net.add_transition(Transition(name="t", inputs=inputs, outputs=[], action=_passthrough, guard=lambda t: True))
    net.binding_search_limit = limit
    exhausted = _record_exhaustion(net)
    for k in range(30):
        net.deposit("a", Token(payload={"wid": k}))
        net.deposit("b", Token(payload={"wid": k}))
    net.deposit("c", Token(payload={"wid": -1}))
    t = net.transitions["t"]
    assert net._resolve_binding(t, net.model_time) is None
    assert net._binding_exists(t, net.model_time) is False
    assert bool(exhausted) is (limit < 30)


@settings(max_examples=300, deadline=None)
@given(
    arcs=st.lists(st.tuples(st.sampled_from(["p", "q"]), st.sampled_from(_SIZED_KINDS)), min_size=2, max_size=4),
    payloads=st.fixed_dictionaries(
        {"p": st.lists(_token_payloads, max_size=4), "q": st.lists(_token_payloads, max_size=4)}
    ),
    guarded=st.booleans(),
)
def test_first_binds_what_the_equality_guard_search_binds(arcs, payloads, guarded):
    # Under FIRST with room to search, a join transition fires the first candidate, in product
    # order, whose join arcs agree on their key. Its twin, with each join arc a plain arc and the
    # key equality moved into the guard, walks the same product, so it binds the same tokens.
    sizes = [2 if kind == "pair" else 1 for _, kind in arcs]
    starts = list(itertools.accumulate([0, *sizes]))
    keyed = [(starts[i], _JOIN_FN[kind]) for i, (_, kind) in enumerate(arcs) if kind in _JOIN_FN]
    parity = (lambda tokens: sum(t.payload["n"] for t in tokens) % 2 == 0) if guarded else (lambda tokens: True)

    def agree(tokens: list[Token]) -> bool:
        return len({fn(tokens[at]) for at, fn in keyed}) <= 1 and parity(tokens)

    def bound(twin: bool) -> "list[list[int]] | None":
        net = PetriNet(seed=0)
        for name in ("p", "q"):
            net.add_place(Place(name))
        inputs = [
            InputArc(place, **(_KINDS["plain"] if twin and kind in _JOIN_FN else _KINDS[kind])) for place, kind in arcs
        ]
        guard = agree if twin else parity
        net.add_transition(
            Transition(
                name="t",
                inputs=inputs,
                outputs=[],
                action=_passthrough,
                binding_policy=BindingPolicy.FIRST,
                guard=guard,
            )
        )
        net.binding_search_limit = 100_000
        for place in ("p", "q"):
            for n, payload in enumerate(payloads[place]):
                net.deposit(place, Token(payload={**payload, "i": n}))
        binding = net._resolve_binding(net.transitions["t"], net.model_time)
        return None if binding is None else [[token.payload["i"] for token in tokens] for _, tokens in binding]

    assert bound(twin=False) == bound(twin=True)


def test_a_plain_arc_taking_two_tokens_gives_back_the_room_of_a_token_it_drops():
    # Key 0's group in `a` is [x, y] and one join arc needs it, so the plain arc may take one
    # of the two. The guard rejects every pick of x. Backing out of x must give that room
    # back, or y looks unaffordable too and the one binding, [y, z] with x for the join, is missed.
    net = PetriNet(seed=0)
    for name in ("a", "b"):
        net.add_place(Place(name))
    x, y, z = (Token(payload={"wid": wid, "n": n}) for n, wid in enumerate((0, 0, 9)))
    inputs = [InputArc("a", count=2), InputArc("a", join_key=WID), InputArc("b", join_key=WID)]
    guard = lambda tokens: all(token is not x for token in tokens[:2])  # noqa: E731
    net.add_transition(Transition(name="t", inputs=inputs, outputs=[], action=_passthrough, guard=guard))
    for token in (x, y, z):
        net.deposit("a", token)
    net.deposit("b", Token(payload={"wid": 0}))
    binding = net._resolve_binding(net.transitions["t"], net.model_time)
    assert binding is not None
    assert [tokens for _, tokens in binding][:2] == [[y, z], [x]]


def _first_binding_at_limit_one(inputs: list[InputArc], a: list[dict]) -> "tuple[list[list[int]] | None, list[str]]":
    net = PetriNet(seed=0)
    for name in ("a", "b"):
        net.add_place(Place(name))
    net.add_transition(Transition(name="t", inputs=inputs, outputs=[], action=_passthrough, guard=lambda t: True))
    net.binding_search_limit = 1
    exhausted = _record_exhaustion(net)
    for n, payload in enumerate(a):
        net.deposit("a", Token(payload={**payload, "n": n}))
    net.deposit("b", Token(payload={"wid": 0}))
    binding = net._resolve_binding(net.transitions["t"], net.model_time)
    return (None if binding is None else [[t.payload.get("n", -1) for t in tokens] for _, tokens in binding]), exhausted


def test_a_plain_arc_leaves_the_one_function_of_two_that_has_no_token_to_spare():
    # Key 0 has one token under `wid`, the head, and five under `alt`. Only the `wid` function
    # alone is tight: both together have tokens to spare. The plain arc must still skip the head.
    inputs = [InputArc("a"), InputArc("a", join_key=WID), InputArc("a", join_key=_ALT), InputArc("b", join_key=WID)]
    a = [{"wid": 0, "alt": 0}, *({"wid": 100 + n, "alt": 0} for n in range(4))]
    assert _first_binding_at_limit_one(inputs, a) == ([[1], [0], [2], [-1]], [])


def test_a_plain_arc_counts_a_token_already_taken_from_the_group_it_must_spare():
    # Key 0's group is the first two tokens. The first plain arc may take one; the second then
    # sees a group of two with one gone, and must leave the other for the join arc.
    inputs = [InputArc("a"), InputArc("a"), InputArc("a", join_key=WID), InputArc("b", join_key=WID)]
    a = [{"wid": 0}, {"wid": 0}, {"wid": 7}, {"wid": 8}]
    assert _first_binding_at_limit_one(inputs, a) == ([[0], [2], [1], [-1]], [])


def test_a_plain_arc_has_no_option_when_the_later_join_arcs_are_already_short():
    # Not reachable from a net, whose earlier arcs are pruned so the later ones keep their
    # tokens: two join arcs with one untaken token between them leave the plain arc nothing.
    from cpnx.engine import _join_level_options, _JoinLevel, _Ordered, _plain_level_options

    tokens = [Token(payload={"n": n}) for n in range(4)]
    group = list(enumerate(tokens[:2]))
    for count in (1, 2):
        level = _JoinLevel("plain", "a", count, rest=((2, 1),))
        lists = [_Ordered(tokens, count), group]
        assert list(_plain_level_options(lists, level, 0, {("a", id(tokens[0]))}, 5)) == []
        assert list(_plain_level_options(lists, level, 0, set(), 5))  # two tokens for two arcs: fine
    # The same for a join arc ahead of them: with a token gone it has no option either.
    level = _JoinLevel("join", "a", 1, rest=((2, 1),))
    lists = [list(enumerate(tokens)), group]
    assert list(_join_level_options(lists, level, 0, {("a", id(tokens[0]))})) == []
    assert [index for index, _ in _join_level_options(lists, level, 0, set())] == [2, 3]


# -- how deep a plain arc reads a shared place of a join ----------------------------------------


@pytest.mark.parametrize("limit", [1, 3])
def test_a_plain_arc_on_a_join_place_reads_the_search_limit_plus_the_place_demand(limit):
    # `a` holds key 0's only token first, which the plain arc must leave for the join arc, then
    # `limit + 1` others: that many candidates, one past the limit, so a guard rejecting all
    # of them runs the search out and it is signalled. Reading one token less would find
    # `limit` candidates, reject them, and report "no binding" as if it had seen them all.
    for others, signalled in ((limit + 1, ["t"]), (limit, [])):
        net = PetriNet(seed=0)
        for name in ("a", "b"):
            net.add_place(Place(name))
        inputs = [InputArc("a"), InputArc("a", join_key=WID), InputArc("b", join_key=WID)]
        net.add_transition(Transition(name="t", inputs=inputs, outputs=[], action=_passthrough, guard=lambda t: False))
        net.binding_search_limit = limit
        exhausted = _record_exhaustion(net)
        for wid in (0, *range(100, 100 + others)):
            net.deposit("a", Token(payload={"wid": wid}))
        net.deposit("b", Token(payload={"wid": 0}))
        assert net._resolve_binding(net.transitions["t"], net.model_time) is None
        assert exhausted == signalled  # with `limit` others every candidate is seen: nothing to signal


def test_a_plain_arcs_first_options_never_reach_past_its_read_depth():
    # The cap on a plain arc's options is loss-free only if its first `limit + 1` options over
    # the whole place lie within `_plain_read_depth` tokens. Random small places: tokens taken
    # by earlier arcs, later join arcs under one or two functions with small groups, any order.
    from cpnx.engine import _JoinLevel, _Ordered, _plain_level_options, _plain_read_depth

    rng = random.Random(75)
    tight = 0
    for _ in range(3_000):
        limit, count = rng.randint(1, 3), rng.randint(1, 3)
        tokens = [Token(payload={"n": n}) for n in range(rng.randint(count, 14))]
        rng.shuffle(tokens)
        later = [rng.randint(1, 2) for _ in range(rng.randint(0, 2))]  # join arcs per later function
        groups = [list(enumerate(rng.sample(tokens, min(len(tokens), arcs + rng.randint(0, 3))))) for arcs in later]
        here = rng.sample(tokens, rng.randint(0, min(2, len(tokens) - count)))
        demand = {"a": len(here) + count + sum(later)}
        level = _JoinLevel("plain", "a", count, rest=tuple((arcs, j + 1) for j, arcs in enumerate(later)))
        taken = {("a", id(token)) for token in here}
        depth = _plain_read_depth(InputArc("a", count=count), limit, demand)

        def first_options(read: int, at=(tokens, count, groups, level, taken, limit)) -> list[tuple]:
            tokens, count, groups, level, taken, limit = at
            lists = [_Ordered(tokens[:read], count), *groups]
            return [indexes for indexes, _ in _plain_level_options(lists, level, 0, taken, limit + 1)]

        assert first_options(depth) == first_options(len(tokens))
        tight += first_options(depth - 1) != first_options(len(tokens))
    assert tight  # and the depth is not slack: somewhere, one token less loses an option


# -- the probe's one-key shortcut against firing, with more keys than the limit -----------------


def _c_before_the_join(c_arcs: list[InputArc], keys: int, limit: int, **place_kwargs) -> PetriNet:
    """`[c, a:k, b:k, c]` with `keys` matched keys; `c` holds an odd token, then an even one."""
    net = PetriNet(seed=0)
    for name in ("a", "b", "c"):
        net.add_place(Place(name, **place_kwargs))
    inputs = [c_arcs[0], InputArc("a", join_key=WID), InputArc("b", join_key=WID), c_arcs[1]]
    net.add_transition(Transition(name="t", inputs=inputs, outputs=[], action=_passthrough))
    net.binding_search_limit = limit
    for n in (1, 2):
        net.deposit("c", Token(payload={"n": n}))
    for wid in range(keys):
        net.deposit("a", Token(payload={"wid": wid}))
        net.deposit("b", Token(payload={"wid": wid}))
    return net


def _odd(token: Token) -> bool:
    return token.payload["n"] % 2 == 1


@pytest.mark.parametrize(("keys", "limit"), [(3, 2), (5, 3), (50, 10), (50, 1_000)])
def test_a_plain_arc_that_dead_ends_before_the_join_arcs_turns_the_probe_shortcut_off(keys, limit):
    # The first arc on `c` takes the odd token, which the last arc's filter needs: a dead end,
    # and in firing's order it comes once per key before the first arc moves on. With more
    # keys than the limit, firing runs out. A probe reading one key would see one dead end and
    # then a binding, and report a transition enabled that never fires.
    net = _c_before_the_join([InputArc("c"), InputArc("c", filter=_odd)], keys, limit)
    t = net.transitions["t"]
    fires = keys <= limit
    assert (net._resolve_binding(t, net.model_time) is not None) is fires
    assert net._binding_exists(t, net.model_time) is fires


def test_a_net_whose_search_runs_out_on_per_key_dead_ends_is_dead_not_spinning():
    # The same shape on the full-scan path (a settle window elsewhere puts the net on it):
    # a probe that disagreed with firing left `run()` looping on a transition it could not fire.
    net = _c_before_the_join([InputArc("c"), InputArc("c", filter=_odd)], 50, 10)
    net.add_place(Place("z"))
    net.add_transition(Transition(name="u", inputs=[InputArc("z", settle_secs=1.0)], outputs=[], action=_passthrough))
    assert net.step() is False
    assert net.is_dead()
    assert net.is_quiescent()


def test_a_plain_arc_before_the_join_arcs_keeps_the_probe_shortcut_without_a_filter():
    # With no filter on `c`, both arcs choose among the same tokens and neither can run out
    # while the other has an option: no dead end, so one key answers the probe, as firing does.
    net = _c_before_the_join([InputArc("c"), InputArc("c", key=NEG_N)], 50, 1)
    t = net.transitions["t"]
    assert net._binding_exists(t, net.model_time)
    assert net._resolve_binding(t, net.model_time) is not None


_PROBE_ARCS = {
    "k": {"join_key": WID},
    "alt": {"join_key": _ALT},
    "plain": {},
    "even": {"filter": _EVEN},
    "newest": {"key": NEG_N},
    "two": {"count": 2},
    "all": {"consume_all": True},
}


@settings(max_examples=400, deadline=None)
@example(arcs=[("c", "plain"), ("a", "k"), ("b", "k"), ("c", "even")], keys=5, extra=[2, 1], limit=3)
@example(arcs=[("c", "newest"), ("a", "k"), ("c", "even")], keys=6, extra=[1, 2], limit=2)
@example(arcs=[("c", "two"), ("c", "even"), ("b", "k")], keys=6, extra=[3, 2, 1], limit=2)
@given(
    arcs=st.lists(st.tuples(st.sampled_from("abcc"), st.sampled_from(sorted(_PROBE_ARCS))), min_size=2, max_size=5),
    keys=st.integers(1, 12),
    extra=st.lists(st.integers(0, 9), max_size=4),
    limit=st.integers(1, 4),
)
def test_the_guard_free_probe_agrees_with_firing_past_the_search_limit(arcs, keys, extra, limit):
    # One token per key in `a` and `b`, more keys than the limit, and a few tokens in `c`: the
    # probe, which may read one key, and firing, which merges them all and stops at the limit,
    # must give the same answer whatever the arcs are and wherever they sit.
    net = PetriNet(seed=0)
    for name in ("a", "b", "c"):
        net.add_place(Place(name))
    inputs = [InputArc(place, **_PROBE_ARCS[kind]) for place, kind in arcs]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # two draining arcs on one place warn; here they are a case
        net.add_transition(Transition(name="t", inputs=inputs, outputs=[], action=_passthrough))
    net.binding_search_limit = limit
    for wid in range(keys):
        net.deposit("a", Token(payload={"wid": wid, "alt": wid // 2, "n": wid}))
        net.deposit("b", Token(payload={"wid": wid, "alt": wid, "n": wid + 1}))
    for n in extra:
        net.deposit("c", Token(payload={"wid": n, "alt": n, "n": n}))
    t = net.transitions["t"]
    fires = net._resolve_binding(t, net.model_time) is not None
    assert net._binding_exists(t, net.model_time) is fires


@pytest.mark.parametrize("swap", [False, True])
def test_a_token_in_two_tight_sets_counts_toward_both(swap):
    # b belongs to both sets, each with room for one token: an option may hold b alone of
    # either set, so [a, b] and [b, c] are out and every other pair stays, in order. Either
    # set may be the one that has run out when b comes up, so both orders are checked.
    from cpnx.engine import _walk_options

    a, b, c, d = tokens = [Token(payload={"n": n}) for n in range(4)]
    constraints = [({id(a), id(b)}, 1), ({id(b), id(c)}, 1)]
    if swap:
        constraints.reverse()
    options = [indexes for indexes, _ in _walk_options(tokens, set(), 2, constraints)]
    assert options == [(0, 2), (0, 3), (1, 3), (2, 3)]
