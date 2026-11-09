"""Two input arcs on one place take distinct tokens (issue #75).

In CPN terms, two arcs from one place take two tokens, as one arc with `count=2` does. A
binding that hands one token to both arcs is not a binding: firing it would remove the token
twice, and `retrieve_specific` raised `ValueError`. Every binding path must keep the arcs'
tokens apart: the head-only path, the product search (eager and lazy), the `join_key`
search, and the `consume_all` count-only path.
"""

import pytest

from cpnx import BindingPolicy, InputArc, OutputArc, PetriNet, Place, Token, Transition

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


def test_rejected_candidates_count_toward_the_search_limit():
    exhausted = []
    net = _net_moving([InputArc("jobs"), InputArc("jobs")], n_tokens=2, guard=lambda tokens: True)
    net.binding_search_limit = 1
    net.on_binding_search_exhausted = exhausted.append
    # The first candidate hands token 0 to both arcs; with a limit of 1 the search stops there.
    assert net._resolve_binding(net.transitions["t"], net.model_time) is None
    net.binding_search_limit = 2
    assert net._resolve_binding(net.transitions["t"], net.model_time) is not None


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
