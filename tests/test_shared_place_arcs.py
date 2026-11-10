"""Two input arcs on one place take distinct tokens (issue #75).

In CPN terms, two arcs from one place take two tokens, as one arc with `count=2` does. A
binding that hands one token to both arcs is not a binding: firing it would remove the token
twice, and `retrieve_specific` raised `ValueError`. Every binding path must keep the arcs'
tokens apart: the head-only path, the product search (eager and lazy), the `join_key`
search, and the `consume_all` count-only path.
"""

import itertools
import time

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from cpnx import BindingPolicy, InputArc, OutputArc, PetriNet, Place, Token, Transition
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


@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("policy", [BindingPolicy.FIRST, BindingPolicy.RANDOM, BindingPolicy.PRIORITY])
def test_a_consume_all_arc_beside_another_arc_on_its_place_is_never_enabled(guarded, policy):
    # Guard-free FIRST takes the head-only path; a guard or another policy searches, where only
    # the reuse check on the drain's place keeps the two arcs apart.
    guard = {"guard": lambda tokens: True} if guarded else {}
    for inputs in (
        [InputArc("jobs", consume_all=True), InputArc("jobs")],
        [InputArc("jobs"), InputArc("jobs", consume_all=True)],
    ):
        net = _net_moving(inputs, n_tokens=3, binding_policy=policy, **guard)
        net.transitions["t"].outputs[0].count = 1
        t = net.transitions["t"]
        assert net._resolve_binding(t, net.model_time) is None
        assert not net._binding_exists(t, net.model_time)
        net.drive_to_quiescence()
        assert len(net.marking["jobs"]) == 3


def test_the_consume_all_fast_path_keeps_shared_arcs_apart():
    # A drain on another place sends the transition down the count-only path, which resolves
    # each arc on its own; the two `jobs` arcs must still take different tokens.
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
    _assert_distinct(net)
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
    first = Token(payload={"wid": 0, "n": 0})
    store.append(first)
    generation, every_head, changes = store.join_head_changes(1, None)
    assert every_head == {} and changes == {}
    timed = Token(payload={"wid": 0, "n": 1}, available_at=4.0)
    store.append(timed)
    assert store.join_head_changes(1, generation) == (generation, None, {0: 4.0})
    # A third token behind the second changes nothing; one ahead of it does.
    store.append(Token(payload={"wid": 0, "n": 2}, available_at=9.0))
    assert store.join_head_changes(1, generation) == (generation, None, {})
    ready = Token(payload={"wid": 0, "n": 3})
    store.append(ready)
    assert store.join_head_changes(1, generation) == (generation, None, {0: float("-inf")})
    store.remove_identity([first, ready])
    assert store.join_head_changes(1, generation) == (generation, None, {0: 9.0})
    store.remove_identity([timed])
    assert store.join_head_changes(1, generation) == (generation, None, {0: None})


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
    assert net._join_matches["t"].keys_at(net.model_time) == []
    net.deposit("jobs", Token(payload={"wid": 7}))
    assert net._binding_exists(t, net.model_time)
    assert net._join_matches["t"].keys_at(net.model_time) == [7]
    net.drive_to_quiescence()
    assert [token.payload["wid"] for token in net.marking["out"]] == [7, 7]
    assert net._join_matches["t"].keys_at(net.model_time) == []


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
    ("arcs", "kinds", "shortcut", "reusable"),
    [
        (["a:k", "b:k", "c", "c"], ["free", "free", "plain", "plain"], True, set()),
        (["c", "a:k", "b:k", "c"], ["plain", "free", "free", "plain"], False, set()),
        (["a:k", "a:k"], ["join", "join"], True, set()),
        (["a:k", "a:k", "a"], ["join", "join", "plain"], False, set()),
        (["a:k", "a:g"], ["join", "join"], True, set()),
        (["a:k", "b:k", "c", "c*"], ["free", "free", "plain", "free"], True, {"c"}),
        (["a:k", "b:k"], ["free", "free"], True, set()),
    ],
)
def test_join_plan_levels_shortcut_and_reusable_places(arcs, kinds, shortcut, reusable):
    # Join arcs on a shared place and plain arcs there are enumerated without reusing a token;
    # only a consume_all arc keeps the whole pool, so its place is checked for reuse. The probe
    # shortcut needs every plain arc on a shared place after all join arcs, off join places.
    from cpnx.engine import _join_plan, _shared_places

    functions = {"k": WID, "g": N}
    inputs = []
    for spec in arcs:
        drain = spec.endswith("*")
        place, _, fn = spec.rstrip("*").partition(":")
        inputs.append(InputArc(place, join_key=functions[fn]) if fn else InputArc(place, consume_all=drain))
    transition = Transition(name="t", inputs=inputs, outputs=[], action=_passthrough)
    plan = _join_plan(transition, _shared_places(transition))
    assert [level.kind for level in plan.levels] == kinds
    assert plan.shortcut is shortcut
    assert plan.reusable == reusable


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
}
_JOIN_FN = {"join_f": _JF, "join_g": _JG}


def _arc_accepts(kind: str, token: Token) -> bool:
    return kind != "even" or _EVEN(token)


def _distinct_binding_exists(arcs: list[tuple[str, str]], marking: dict[str, list[Token]], guard) -> bool:
    """Brute force: tokens per arc, none taken twice from one place, filters and the join key agreeing."""

    def go(i: int, used: frozenset[tuple[str, int]], join_value: object, chosen: list[Token]) -> bool:
        if i == len(arcs):
            return guard(chosen)
        place, kind = arcs[i]
        free = [t for t in marking[place] if (place, id(t)) not in used and _arc_accepts(kind, t)]
        for group in itertools.combinations(free, 2 if kind == "pair" else 1):
            value = join_value
            if kind in _JOIN_FN:
                value = _JOIN_FN[kind](group[0])
                if join_value is not _UNSET and value != join_value:
                    continue
            if go(i + 1, used | {(place, id(t)) for t in group}, value, chosen + list(group)):
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
    # Plain, filtered, keyed, count=2 and join arcs (two join_key functions) over two places,
    # with a guard or not, under every policy. With a limit too large to run out, the engine
    # binds exactly when brute force finds distinct tokens, its binding is valid, and firing
    # it moves those tokens. With a small limit, a miss must be signalled as exhaustion.
    guard = (lambda tokens: sum(t.payload["n"] for t in tokens) % 2 == 0) if guarded else (lambda tokens: True)

    def build(limit: int) -> tuple[PetriNet, dict[str, list[Token]], list]:
        net = PetriNet(seed=0)
        for name in ("p", "q", "out"):
            net.add_place(Place(name))
        net.add_transition(
            Transition(
                name="t",
                inputs=[InputArc(place, **_KINDS[kind]) for place, kind in arcs],
                outputs=[OutputArc("out", count=sum(2 if kind == "pair" else 1 for _, kind in arcs))],
                action=_passthrough,
                binding_policy=policy,
                **({"guard": guard} if guarded else {}),
            )
        )
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
        assert moved == sum(2 if kind == "pair" else 1 for _, kind in arcs)

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
    assert time.perf_counter() - started < 0.25
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
    assert time.perf_counter() - started < 0.25
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


@pytest.mark.parametrize(
    "arcs",
    [
        ["a", "a", "a:k", "b:k"],
        ["a2", "a:k", "b:k"],
        ["a", "a:k", "a", "b:k"],
        ["a", "a:k", "a:alt", "b:k"],
        ["a", "a:alt", "a", "a:k", "b:k"],
    ],
)
@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("policy", [BindingPolicy.FIRST, BindingPolicy.RANDOM])
def test_a_plain_arc_before_a_join_arc_skips_the_token_the_key_needs(arcs, guarded, policy):
    # The head of a deep place is the matched key's only token there. Plain arcs read the place
    # before the join arc, so every option that takes the head strands the join arc: more than
    # the search limit of them, ahead of the first binding. The residual Hall check skips the
    # head for plain arcs too, so the search binds without counting them.
    net = PetriNet(seed=0)
    for name in ("a", "b", "out"):
        net.add_place(Place(name))
    inputs = []
    for spec in arcs:
        place, _, fn = spec.partition(":")
        if fn:
            inputs.append(InputArc(place, join_key=WID if fn == "k" else _ALT))
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
            binding_policy=policy,
            **({"guard": lambda tokens: True} if guarded else {}),
        )
    )
    net.binding_search_limit = 20
    exhausted = _record_exhaustion(net)
    net.deposit("a", Token(payload={"wid": 0, "alt": 1, "n": -1}))
    for n in range(net.binding_search_limit + 30):
        net.deposit("a", Token(payload={"wid": 100 + n, "alt": 100 + n, "n": n}))
    net.deposit("a", Token(payload={"wid": 50, "alt": 0, "n": 999}))  # the alt function's key-0 token, last
    net.deposit("b", Token(payload={"wid": 0}))
    t = net.transitions["t"]
    started = time.perf_counter()
    assert net._binding_exists(t, net.model_time)
    binding = net._resolve_binding(t, net.model_time)
    assert binding is not None
    assert time.perf_counter() - started < 0.25
    _assert_distinct(net)
    if policy is BindingPolicy.FIRST:  # RANDOM samples from more candidates than the limit, and says so
        assert exhausted == []
    taken = {id(token) for _, tokens in binding for token in tokens}
    assert len(taken) == sum(len(tokens) for _, tokens in binding)
    before = len(net.marking["a"]) + len(net.marking["b"])
    net.step()
    assert before - len(net.marking["a"]) - len(net.marking["b"]) == sum(2 if s == "a2" else 1 for s in arcs)
