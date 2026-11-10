"""Two input arcs on one place take distinct tokens (issue #75).

In CPN terms, two arcs from one place take two tokens, as one arc with `count=2` does. A
binding that hands one token to both arcs is not a binding: firing it would remove the token
twice, and `retrieve_specific` raised `ValueError`. Every binding path must keep the arcs'
tokens apart: the head-only path, the product search (eager and lazy), the `join_key`
search, and the `consume_all` count-only path.
"""

import itertools

import pytest
from hypothesis import given, settings
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


def test_a_consume_all_arc_beside_another_arc_on_its_place_is_never_enabled():
    for inputs in (
        [InputArc("jobs", consume_all=True), InputArc("jobs")],
        [InputArc("jobs"), InputArc("jobs", consume_all=True)],
    ):
        net = _net_moving(inputs, n_tokens=3)
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
    exhausted = []
    net.on_binding_search_exhausted = exhausted.append
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
    exhausted = []
    net.on_binding_search_exhausted = exhausted.append
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
