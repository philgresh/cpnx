"""`InputArc.join_key`: an opt-in equi-join between input arcs (issue #64, ADR 0010).

A transition whose `join_key` arcs must agree on their key considers only the product's
candidates that agree, in the product's own order. So a net that adds `join_key` to a guard
already testing that equality fires exactly the same bindings, under every policy, while a
join with no match costs lookups instead of a walk over the product.
"""

import random

import pytest

from cpnx import BindingPolicy, InputArc, OutputArc, PetriNet, Place, ResourcePlace, Token, Transition, places
from cpnx.places import _TokenStore

WID = lambda t: t.payload["wid"]  # noqa: E731


def _same_wid(tokens: list[Token]) -> bool:
    return tokens[0].payload["wid"] == tokens[-1].payload["wid"]


# -- construction ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, named",
    [
        ({"count": 2}, "count != 1"),
        ({"consume_all": True}, "consume_all"),
        ({"key": lambda t: t.created_at}, "key"),
        ({"filter": lambda t: True}, "filter"),
    ],
)
def test_join_key_rejects_shapes_it_does_not_model(kwargs, named):
    with pytest.raises(ValueError, match=named):
        InputArc("jobs", join_key=WID, **kwargs)


def test_join_key_reassignment_is_checked_and_leaves_the_arc_unchanged():
    arc = InputArc("jobs", join_key=WID)
    with pytest.raises(ValueError, match="count != 1"):
        arc.count = 3
    assert arc.count == 1
    plain = InputArc("jobs", count=2)
    with pytest.raises(ValueError):
        plain.join_key = WID
    assert plain.join_key is None
    arc.join_key = None
    arc.count = 3
    assert arc.count == 3


def test_join_key_certification_is_tracked():
    assert InputArc("jobs", join_key=WID)._join_key_inline_safe
    table = {}
    assert not InputArc("jobs", join_key=lambda t: table.get(t.payload["wid"]))._join_key_inline_safe


def test_join_key_rejects_a_string():
    with pytest.raises(TypeError):
        InputArc("jobs", join_key="payload['wid']")


# -- parity with the guard-only search --------------------------------------------------------


def _parity_net(seed: int, *, join: bool, policy: BindingPolicy, timed: bool, join_key=WID):
    rng = random.Random(seed)
    net = PetriNet(seed=seed, binding_search_limit=1_000_000)
    for name in ("jobs", "tools", "workers", "out"):
        net.add_place(Place(name))
    fired: list[tuple[int, ...]] = []

    def act(tokens: list[Token]) -> list[Token]:
        fired.append(tuple(t.payload["n"] for t in tokens))
        return [Token(payload={"n": -1, "wid": -1})]

    jk = join_key if join else None
    net.add_transition(
        Transition(
            name="match",
            inputs=[InputArc("jobs", join_key=jk), InputArc("tools"), InputArc("workers", join_key=jk)],
            outputs=[OutputArc("out")],
            guard=lambda toks: (
                toks[0].payload["wid"] == toks[2].payload["wid"]
                and (toks[0].payload["n"] + toks[1].payload["n"]) % 3 != 0
            ),
            binding_policy=policy,
            action=act,
        )
    )
    net.advance_time(1.0)
    n = 0
    for place, count in (("jobs", 40), ("workers", 25), ("tools", 4)):
        for _ in range(count):
            n += 1
            at = 1.0 + rng.uniform(0, 20) if timed and rng.random() < 0.5 else 0.0
            net.deposit(place, Token(payload={"n": n, "wid": rng.randrange(8)}, available_at=at))
    net.drive_to_quiescence()
    return fired


@pytest.mark.parametrize("policy", [BindingPolicy.FIRST, BindingPolicy.RANDOM, BindingPolicy.PRIORITY])
@pytest.mark.parametrize("timed", [False, True])
def test_join_key_fires_what_the_equality_guard_fires(policy, timed):
    for seed in range(8):
        expected = _parity_net(seed, join=False, policy=policy, timed=timed)
        assert expected
        assert _parity_net(seed, join=True, policy=policy, timed=timed) == expected


def test_uncertified_join_key_fires_the_same_bindings():
    offset = [0]
    uncertified = lambda t: t.payload["wid"] + offset[0]  # noqa: E731
    assert not InputArc("x", join_key=uncertified)._join_key_inline_safe
    for seed in range(4):
        expected = _parity_net(seed, join=False, policy=BindingPolicy.FIRST, timed=True)
        assert _parity_net(seed, join=True, policy=BindingPolicy.FIRST, timed=True, join_key=uncertified) == expected


def test_join_on_a_place_subclass_falls_back_to_the_same_bindings():
    net = PetriNet(seed=1)
    net.add_place(Place("jobs"))
    net.add_place(ResourcePlace("workers", capacity=0))
    net.add_place(Place("out"))
    net.add_transition(
        Transition(
            name="match",
            inputs=[InputArc("jobs", join_key=WID), InputArc("workers", join_key=WID)],
            outputs=[OutputArc("out")],
            action=lambda toks: [toks[0]],
        )
    )
    net.deposit("workers", Token(color="resource", payload={"wid": 2}))
    for wid in (1, 2, 3):
        net.deposit("jobs", Token(payload={"wid": wid}))
    binding = net._resolve_binding(net.transitions["match"], net.model_time)
    assert [tokens[0].payload["wid"] for _, tokens in binding] == [2, 2]


# -- behaviour --------------------------------------------------------------------------------


def _join_net(*, guard=None, limit: int = 1000) -> PetriNet:
    net = PetriNet(seed=0, binding_search_limit=limit)
    for name in ("jobs", "workers", "out"):
        net.add_place(Place(name))
    net.add_transition(
        Transition(
            name="match",
            inputs=[InputArc("jobs", join_key=WID), InputArc("workers", join_key=WID)],
            outputs=[OutputArc("out")],
            guard=guard,
            action=lambda toks: [toks[0]],
        )
    )
    return net


def test_a_guard_free_join_is_not_resolved_on_the_heads():
    net = _join_net()
    net.deposit("workers", Token(payload={"wid": 3}))
    net.deposit("jobs", Token(payload={"wid": 1, "n": 0}))
    net.deposit("jobs", Token(payload={"wid": 3, "n": 1}))
    assert net.drive_to_quiescence().steps == 1
    assert [t.payload["n"] for t in net.places["out"].tokens] == [1]
    assert [t.payload["n"] for t in net.places["jobs"].tokens] == [0]


def test_a_join_with_no_match_evaluates_no_guard():
    calls = []

    def guard(tokens):
        calls.append(1)
        return True

    net = _join_net(guard=guard)
    for wid in range(8):
        net.deposit("workers", Token(payload={"wid": wid}))
    for i in range(2_000):
        net.deposit("jobs", Token(payload={"wid": 8 + i % 8}))
    transition = net.transitions["match"]
    assert not net._binding_exists(transition, net.model_time)
    assert net._resolve_binding(transition, net.model_time) is None
    assert calls == []


def test_first_takes_the_earliest_job_with_a_matching_worker():
    net = _join_net()
    for wid in (5, 4):
        net.deposit("workers", Token(payload={"wid": wid}))
    for n, wid in enumerate([9, 4, 5, 4]):
        net.deposit("jobs", Token(payload={"wid": wid, "n": n}))
    binding = net._resolve_binding(net.transitions["match"], net.model_time)
    assert [tokens[0].payload for _, tokens in binding] == [{"wid": 4, "n": 1}, {"wid": 4}]


def test_timed_tokens_join_once_they_mature():
    net = _join_net()
    net.advance_time(1.0)
    net.deposit("workers", Token(payload={"wid": 1}))
    net.deposit("jobs", Token(payload={"wid": 2, "n": 1}))
    net.deposit("jobs", Token(payload={"wid": 1, "n": 0}, available_at=5.0))
    transition = net.transitions["match"]
    assert not net._binding_exists(transition, net.model_time)
    assert net._binding_exists(transition, net.model_time, ignore_timing=True)
    assert not net.is_quiescent()
    net.drive_to_quiescence()
    assert net.model_time == 5.0
    assert [t.payload["n"] for t in net.places["out"].tokens] == [0]


def test_a_probe_with_more_candidates_than_the_limit_agrees_with_firing():
    # 3 workers x 3 jobs share wid 0, so 9 candidates; the guard holds only on the last one.
    last = {}

    def guard(tokens):
        return tokens[0] is last["job"] and tokens[1] is last["worker"]

    net = _join_net(guard=guard, limit=4)
    for _ in range(3):
        net.deposit("workers", Token(payload={"wid": 0}))
        net.deposit("jobs", Token(payload={"wid": 0}))
    last["job"], last["worker"] = net.places["jobs"].tokens[-1], net.places["workers"].tokens[-1]
    transition = net.transitions["match"]
    assert net._resolve_binding(transition, net.model_time) is None
    assert not net._binding_exists(transition, net.model_time)


def test_a_raising_join_key_disables_the_arc_and_reports_once():
    errors = []

    def wid_or_raise(token):
        return token.payload["wid"]

    net = PetriNet(seed=0)
    net.on_error = lambda name, exc, tok: errors.append((name, exc))
    for name in ("jobs", "workers", "out"):
        net.add_place(Place(name))
    net.add_transition(
        Transition(
            name="match",
            inputs=[InputArc("jobs", join_key=wid_or_raise), InputArc("workers", join_key=wid_or_raise)],
            outputs=[OutputArc("out")],
            action=lambda toks: [toks[0]],
        )
    )
    net.deposit("workers", Token(payload={"wid": 1}))
    net.deposit("jobs", Token(payload={"wid": 1}))
    net.deposit("jobs", Token(payload={}))  # cannot be keyed
    assert net.drive_to_quiescence().steps == 0
    assert not net.step()
    assert len(errors) == 1
    assert errors[0][0] == "match"


def test_an_unhashable_join_key_makes_the_arc_unsatisfiable():
    net = PetriNet(seed=0)
    for name in ("jobs", "workers", "out"):
        net.add_place(Place(name))
    key = lambda t: [t.payload["tags"]]  # noqa: E731
    net.add_transition(
        Transition(
            name="match",
            inputs=[InputArc("jobs", join_key=key), InputArc("workers", join_key=key)],
            outputs=[OutputArc("out")],
            action=lambda toks: [toks[0]],
        )
    )
    net.deposit("workers", Token(payload={"tags": ["a"]}))
    net.deposit("jobs", Token(payload={"tags": ["a"]}))
    assert net.drive_to_quiescence().steps == 0


# -- the index --------------------------------------------------------------------------------


def _groups(store: _TokenStore, t_limit: float) -> dict:
    groups = store.join_groups(1, None, t_limit)
    assert groups is not None
    return {key: [token.payload["n"] for _, token in entries] for key, entries in groups.items()}


def test_index_tracks_deposits_removals_and_read_order():
    store = _TokenStore()
    store.register_join_index(1, WID)
    tokens = [
        Token(payload={"wid": 0, "n": 0}, available_at=3.0),
        Token(payload={"wid": 0, "n": 1}),
        Token(payload={"wid": 1, "n": 2}, available_at=1.0),
        Token(payload={"wid": 0, "n": 3}, available_at=2.0),
        Token(payload={"wid": 1, "n": 4}),
    ]
    for token in tokens:
        store.append(token)
    assert _groups(store, 0.5) == {0: [1], 1: [4]}
    assert _groups(store, 2.5) == {0: [1, 3], 1: [4, 2]}
    assert _groups(store, 10.0) == {0: [1, 3, 0], 1: [4, 2]}
    store.remove_identity([tokens[1], tokens[2]])
    assert _groups(store, 10.0) == {0: [3, 0], 1: [4]}
    assert store.take_available(1, 10.0) == [tokens[4]]
    assert _groups(store, 10.0) == {0: [3, 0]}
    assert store.join_groups(1, [1, 0, 7], 2.5) is not None
    assert {k: [t.payload["n"] for _, t in v] for k, v in store.join_groups(1, [1, 0, 7], 2.5).items()} == {0: [3]}


def test_index_back_fills_and_rebuilds_on_a_new_function():
    store = _TokenStore()
    for n in range(4):
        store.append(Token(payload={"wid": n % 2, "n": n}))
    store.register_join_index(1, WID)
    assert _groups(store, 0.0) == {0: [0, 2], 1: [1, 3]}
    store.register_join_index(1, lambda t: t.payload["n"] < 3)
    assert _groups(store, 0.0) == {True: [0, 1, 2], False: [3]}


def test_a_disabled_index_declines_then_recovers_once_the_place_empties():
    store = _TokenStore()
    store.register_join_index(1, WID)
    bad = Token(payload={})
    store.append(Token(payload={"wid": 0, "n": 0}))
    store.append(bad)
    assert store.join_groups(1, None, 0.0) is None
    store.take_available(2, 0.0)
    store.append(Token(payload={"wid": 5, "n": 1}))
    assert _groups(store, 0.0) == {5: [1]}


def test_a_disabled_index_rearms_once_the_bad_token_leaves_a_non_empty_store(monkeypatch):
    monkeypatch.setattr(places, "_KEY_INDEX_REARM_MIN_DEPOSITS", 4)
    store = _TokenStore()
    store.register_join_index(1, WID)
    keep = [Token(payload={"wid": n % 2, "n": n}) for n in range(3)]
    for token in keep:
        store.append(token)
    first_bad, second_bad = Token(payload={}), Token(payload={})
    store.append(first_bad)
    store.append(second_bad)
    assert store.join_groups(1, None, 0.0) is None  # `first_bad` is still stored
    store.remove_identity([first_bad])
    assert store.join_groups(1, None, 0.0) is None  # the rebuild meets `second_bad`
    store.remove_identity([second_bad])
    assert store.join_groups(1, None, 0.0) is None  # too soon after that rebuild
    for n in range(3, 7):
        store.append(Token(payload={"wid": n % 2, "n": n}))
    assert _groups(store, 0.0) == {0: [0, 2, 4, 6], 1: [1, 3, 5]}


def test_a_disabled_index_does_not_rebuild_while_its_bad_token_stays(monkeypatch):
    monkeypatch.setattr(places, "_KEY_INDEX_REARM_MIN_DEPOSITS", 4)
    calls = []

    def counting_wid(token: Token) -> object:
        calls.append(token)
        return token.payload["wid"]

    store = _TokenStore()
    store.register_join_index(1, counting_wid)
    store.append(Token(payload={}))
    for n in range(20):
        store.append(Token(payload={"wid": n % 2, "n": n}))
        assert store.join_groups(1, None, 0.0) is None
    assert len(calls) == 1  # only the bad token was ever keyed


def test_a_group_view_skips_entries_shifted_back_by_a_concurrent_insert():
    store = _TokenStore()
    store.register_join_index(1, WID)
    for n, at in ((0, 3.0), (1, 4.0)):
        store.append(Token(payload={"wid": 0, "n": n}, available_at=at))
    groups = store.join_groups(1, [0], 5.0)
    assert groups is not None
    reader = iter(groups[0])
    assert next(reader)[1].payload["n"] == 0
    # An earlier-maturing token lands in front of both, shifting them one place right.
    store.append(Token(payload={"wid": 0, "n": 2}, available_at=2.0))
    assert [token.payload["n"] for _, token in reader] == []


def _index_id(net: PetriNet, position: int = 0, transition: str = "match") -> int:
    """The id of the join index the engine registered for that input of that transition."""
    return net._join_registrations[transition][position][3]


def test_resetting_join_key_releases_the_place_index():
    net = _two_place_join_net()
    arc = net.transitions["match"].inputs[0]
    net.drive_to_quiescence()
    jobs = net.places["jobs"]
    index_id = _index_id(net)
    assert jobs.peek_join(index_id, None) is not None
    arc.join_key = None
    net.transitions["match"].inputs[1].join_key = None
    net.drive_to_quiescence()
    assert jobs.peek_join(index_id, None) is None
    assert not jobs._store._join_indexes


def test_a_replaced_place_gets_the_join_index_and_the_old_one_drops_it():
    net = _two_place_join_net()
    net.drive_to_quiescence()
    old = net.places["jobs"]
    assert old.peek_join(_index_id(net), None) is not None
    new = Place("jobs")
    net.places["jobs"] = new
    new.deposit(Token(payload={"wid": 1, "n": 5}))
    net.deposit("workers", Token(payload={"wid": 1}))
    assert net.drive_to_quiescence().steps == 1
    assert not old._store._join_indexes
    assert new._store._join_indexes


def _two_place_join_net() -> PetriNet:
    net = PetriNet(seed=0)
    for name in ("jobs", "workers", "out"):
        net.add_place(Place(name))
    net.add_transition(
        Transition(
            name="match",
            inputs=[InputArc("jobs", join_key=WID), InputArc("workers", join_key=WID)],
            outputs=[OutputArc("out")],
            action=lambda toks: [toks[0]],
        )
    )
    net.deposit("jobs", Token(payload={"wid": 9, "n": 0}))
    net.deposit("workers", Token(payload={"wid": 1}))
    return net


def test_a_net_gets_its_join_index_back_after_a_bad_token_is_consumed(monkeypatch):
    monkeypatch.setattr(places, "_KEY_INDEX_REARM_MIN_DEPOSITS", 4)
    errors = []
    net = PetriNet(seed=0)
    net.on_error = lambda name, exc, tok: errors.append(exc)
    for name in ("jobs", "workers", "out", "trash"):
        net.add_place(Place(name))
    net.add_transition(
        Transition(
            name="match",
            inputs=[InputArc("jobs", join_key=WID), InputArc("workers", join_key=WID)],
            outputs=[OutputArc("out")],
            action=lambda toks: [toks[0]],
        )
    )
    for n in range(3):
        net.deposit("jobs", Token(payload={"wid": 9, "n": n}))
    net.deposit("workers", Token(payload={"wid": 1}))
    bad = Token(payload={})
    net.deposit("jobs", bad)
    assert net.drive_to_quiescence().steps == 0
    assert net.places["jobs"].peek_join(_index_id(net), None) is None
    net.places["jobs"].retrieve_specific([bad])
    for n in range(3, 8):
        net.deposit("jobs", Token(payload={"wid": 9, "n": n}))
    assert net.places["jobs"].peek_join(_index_id(net), [9]) is not None
    net.deposit("jobs", Token(payload={"wid": 1, "n": 99}))
    assert net.drive_to_quiescence().steps == 1
    assert [t.payload["n"] for t in net.places["out"].tokens] == [99]
    assert len(errors) == 1


def test_restore_resets_join_index_entries():
    place = Place("jobs", initial_marking=[Token(payload={"wid": i % 3, "n": i}) for i in range(6)])
    place.register_join_index(1, WID)
    snapshot = place.tokens
    place.retrieve(4)
    place._restore_tokens(snapshot)
    groups = place.peek_join(1, None)
    assert {k: [t.payload["n"] for _, t in v] for k, v in groups.items()} == {0: [0, 3], 1: [1, 4], 2: [2, 5]}


# -- the cross-place key match (ADR 0011) -----------------------------------------------------


def _busy_net(seed: int, *, join: bool, policy: BindingPolicy) -> tuple[list[tuple[int, int]], list[tuple[bool, bool]]]:
    """Jobs each need the worker with their `wid`, who is then busy (a timed token) for a while.

    Everything starts ready: a place holding several timed tokens before the drive starts is
    #68, which this does not test.
    """
    rng = random.Random(seed)
    net = PetriNet(seed=seed, binding_search_limit=1_000_000)
    for name in ("jobs", "workers", "out"):
        net.add_place(Place(name))
    fired: list[tuple[int, int]] = []
    busy = random.Random(seed + 1)

    def act(tokens: list[Token]) -> list[Token]:
        job, worker = tokens
        fired.append((job.payload["n"], worker.payload["wid"]))
        return [job, worker.evolve(available_at=net.model_time + busy.uniform(0.5, 3.0))]

    net.add_transition(
        Transition(
            name="match",
            inputs=[
                InputArc("jobs", join_key=WID if join else None),
                InputArc("workers", join_key=WID if join else None),
            ],
            outputs=[OutputArc("out"), OutputArc("workers")],
            guard=None if join else _same_wid,
            binding_policy=policy,
            action=act,
        )
    )
    net.advance_time(1.0)
    for wid in range(12):
        net.deposit("workers", Token(payload={"wid": wid}))
    states: list[tuple[bool, bool]] = []
    for batch in range(3):
        for n in range(50 * batch, 50 * batch + 50):
            net.deposit("jobs", Token(payload={"n": n, "wid": rng.randrange(16)}))
        states.append((net.is_dead(), net.is_quiescent()))
        net.drive_to_quiescence()
        states.append((net.is_dead(), net.is_quiescent()))
    return fired, states


@pytest.mark.parametrize("policy", [BindingPolicy.FIRST, BindingPolicy.RANDOM, BindingPolicy.PRIORITY])
def test_the_key_match_fires_what_the_guard_fires_as_heads_come_and_go(policy):
    for seed in range(6):
        expected = _busy_net(seed, join=False, policy=policy)
        assert len(expected[0]) > 100
        assert expected[1][-1] == (True, True)
        assert _busy_net(seed, join=True, policy=policy) == expected


def test_a_probe_looks_up_only_the_keys_every_place_holds(monkeypatch):
    looked_up: list[int | None] = []
    original = places._JoinIndex.available_groups

    def spy(self, keys, *args):
        looked_up.append(None if keys is None else len(list(keys)))
        return original(self, keys, *args)

    monkeypatch.setattr(places._JoinIndex, "available_groups", spy)
    net = _join_net()
    for wid in range(500):
        net.deposit("workers", Token(payload={"wid": wid}))
    net.deposit("jobs", Token(payload={"wid": 1_000, "n": 0}))
    transition = net.transitions["match"]
    assert net._resolve_binding(transition, net.model_time) is None  # registers the indexes
    looked_up.clear()
    assert net._resolve_binding(transition, net.model_time) is None
    assert looked_up == []
    net.deposit("jobs", Token(payload={"wid": 7, "n": 1}))
    assert net._binding_exists(transition, net.model_time)
    binding = net._resolve_binding(transition, net.model_time)
    assert [tokens[0].payload["wid"] for _, tokens in binding] == [7, 7]
    assert looked_up and max(looked_up) == 1


def test_the_key_match_follows_a_timed_head_and_a_reassigned_key():
    net = _join_net()
    net.advance_time(1.0)
    net.deposit("workers", Token(payload={"wid": 1, "n": 10}, available_at=4.0))
    net.deposit("jobs", Token(payload={"wid": 1, "n": 0}, available_at=2.0))
    transition = net.transitions["match"]
    assert not net._binding_exists(transition, 3.0)
    assert net._binding_exists(transition, 4.0)
    net.deposit("workers", Token(payload={"wid": 1, "n": 11}))
    assert net._binding_exists(transition, 3.0)
    assert not net._binding_exists(transition, 1.5)
    for arc in transition.inputs:
        arc.join_key = lambda t: t.payload["n"] % 10
    assert net._binding_exists(transition, 4.0)
    assert not net._binding_exists(transition, 3.0)


def _times(heads):
    """A head report's `(when, first)` values, cut to `when`."""
    return None if heads is None else {key: None if head is None else head[0] for key, head in heads.items()}


def test_head_changes_report_each_head_change_once():
    store = _TokenStore()
    store.register_join_index(1, WID)
    source = ((1, 1),)
    store.append(Token(payload={"wid": 0, "n": 0}, available_at=5.0))
    generation, every_head, changes = store.join_head_changes(source, None)
    assert _times(every_head) == {0: 5.0} and changes == {}
    late = Token(payload={"wid": 0, "n": 1}, available_at=9.0)
    store.append(late)
    assert store.join_head_changes(source, generation) == (generation, None, {})
    early = Token(payload={"wid": 0, "n": 2})
    store.append(early)
    store.append(Token(payload={"wid": 3, "n": 3}, available_at=1.0))
    got, every_head, changes = store.join_head_changes(source, generation)
    assert got == generation and every_head is None and _times(changes) == {0: float("-inf"), 3: 1.0}
    assert changes[0][1] == (0, 0.0, 2)  # the first entry: the ready token, seq 2
    store.remove_identity([late])
    assert store.join_head_changes(source, generation) == (generation, None, {})
    store.remove_identity([early])
    assert _times(store.join_head_changes(source, generation)[2]) == {0: 5.0}
    store.drop_all()
    new_generation, every_head, changes = store.join_head_changes(source, generation)
    assert new_generation != generation and every_head == {} and changes == {}
    store.append(Token(payload={}))
    assert store.join_head_changes(source, new_generation) is None


def test_two_transitions_sharing_a_join_arc_each_see_every_change():
    # Each transition reads the shared arc through its own index, so neither drains the
    # other's head changes.
    shared = InputArc("workers", join_key=WID)
    net = PetriNet(seed=0)
    for name in ("jobs", "other", "workers", "out1", "out2"):
        net.add_place(Place(name))
    for name, source, out in (("t1", "jobs", "out1"), ("t2", "other", "out2")):
        net.add_transition(
            Transition(
                name=name,
                inputs=[InputArc(source, join_key=WID), shared],
                outputs=[OutputArc(out)],
                action=lambda toks: [toks[0]],
            )
        )
    net.deposit("other", Token(payload={"wid": 98}))
    net.deposit("workers", Token(payload={"wid": 1}))
    net.drive_to_quiescence()
    net.deposit("jobs", Token(payload={"wid": 5}))  # t1 probes again, so both have a match
    net.drive_to_quiescence()
    net.deposit("workers", Token(payload={"wid": 98}))
    net.drive_to_quiescence()
    assert len(net.places["out2"]) == 1
    assert net.is_dead()


def test_the_key_match_keeps_its_state_while_an_index_is_down_and_rebuilds_after(monkeypatch):
    monkeypatch.setattr(places, "_KEY_INDEX_REARM_MIN_DEPOSITS", 4)
    full_reports = []
    original = places._TokenStore.join_head_changes

    def spy(self, sources, generations):
        report = original(self, sources, generations)
        if report is not None and report[1] is not None:
            full_reports.append(report[1])
        return report

    monkeypatch.setattr(places._TokenStore, "join_head_changes", spy)
    net = _join_net()
    net.on_error = lambda *args: None
    for wid in range(50):
        net.deposit("workers", Token(payload={"wid": wid}))
    net.deposit("jobs", Token(payload={"wid": 100, "n": 0}))
    transition = net.transitions["match"]
    for _ in range(2):  # the first probe registers the indexes, the second builds the match
        assert not net._binding_exists(transition, net.model_time)
    built = len(full_reports)
    assert built == 2
    bad = Token(payload={})
    net.deposit("workers", bad)  # disables the index of the later join place
    for _ in range(5):
        assert not net._binding_exists(transition, net.model_time)
    assert "match" in net._join_matches
    assert len(full_reports) == built, "a probe that falls back must not rebuild the match"
    net.places["workers"].retrieve_specific([bad])
    for wid in range(50, 54):
        net.deposit("workers", Token(payload={"wid": wid}))
    net.deposit("jobs", Token(payload={"wid": 7, "n": 9}))
    assert net._binding_exists(transition, net.model_time)
    assert len(full_reports) > built, "the rearmed index reports every head again"
    binding = net._resolve_binding(transition, net.model_time)
    assert [tokens[0].payload["wid"] for _, tokens in binding] == [7, 7]
