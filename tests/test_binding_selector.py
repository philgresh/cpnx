"""`Transition.binding_selector`: outside logic chooses the binding a transition fires (#80, ADR 0013)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cpnx import BindingView, InputArc, OutputArc, PetriNet, Place, Token, Transition

ROOT = Path(__file__).resolve().parent.parent


def _identity(tokens: list[Token]) -> list[Token]:
    return tokens


def newest(view: BindingView):
    return [list(view.available(0))[-1]]


def nothing(view: BindingView):
    return None


def _one_arc_net(selector, n: int = 5, **transition_kwargs) -> PetriNet:
    net = PetriNet(seed=1, places=[Place("in"), Place("out")])
    net.add_transition(
        Transition("t", [InputArc("in")], [OutputArc("out")], _identity, binding_selector=selector, **transition_kwargs)
    )
    for i in range(n):
        net.deposit("in", Token(payload={"i": i}))
    return net


def _payloads(net: PetriNet, place: str) -> list[int]:
    return [t.payload["i"] for t in net.marking[place]]


def test_the_selectors_choice_is_what_fires():
    net = _one_arc_net(newest)
    with net:
        result = net.drive_to_quiescence()
    assert result.steps == 5
    assert _payloads(net, "out") == [4, 3, 2, 1, 0]


def test_a_selector_returning_none_leaves_the_transition_disabled():
    net = _one_arc_net(nothing)
    errors: list[BaseException] = []
    net.on_error = lambda _name, error, _token: errors.append(error)
    with net:
        assert net.is_dead()
        assert net.step() is False
    assert errors == []
    assert len(net.marking["in"]) == 5


def stranger(view: BindingView):
    return [Token(payload={"i": -1})]


def twice(view: BindingView):
    token = next(view.available(0))
    return {0: [token, token]}


def wrong_count(view: BindingView):
    return [list(view.available(0))[:2]]


def wrong_shape(view: BindingView):
    return "token"


def raises(view: BindingView):
    raise LookupError("no server")


def odd_only(tokens: list[Token]) -> bool:
    return tokens[0].payload["i"] % 2 == 1


def first(view: BindingView):
    return [next(view.available(0))]


@pytest.mark.parametrize(
    ("selector", "count", "kwargs", "message"),
    [
        (stranger, 1, {}, "not available"),
        (twice, 2, {}, "more than once"),
        (wrong_count, 1, {}, "takes 1 token"),
        (wrong_shape, 1, {}, "returns None, a sequence or a mapping"),
        (raises, 1, {}, "no server"),
        (first, 1, {"guard": odd_only}, "guard rejects"),
    ],
)
def test_an_invalid_choice_is_reported_and_does_not_fire(selector, count, kwargs, message):
    net = PetriNet(seed=1, places=[Place("in"), Place("out")])
    net.add_transition(
        Transition(
            "t",
            [InputArc("in", count=count)],
            [OutputArc("out", count=count)],
            _identity,
            binding_selector=selector,
            **kwargs,
        )
    )
    for i in range(4):
        net.deposit("in", Token(payload={"i": i}))
    errors: list[BaseException] = []
    net.on_error = lambda _name, error, _token: errors.append(error)
    with net:
        assert net.step() is False
    assert len(net.marking["in"]) == 4 and net.marking["out"] == ()
    assert errors, "the invalid choice is reported"
    assert "binding_selector of transition 't'" in str(errors[0])
    assert message in str(errors[0])


def test_a_cooling_token_is_not_available_to_choose():
    net = PetriNet(seed=1, places=[Place("in"), Place("out")])
    net.add_transition(Transition("t", [InputArc("in")], [OutputArc("out")], _identity, binding_selector=newest))
    net.advance_time(10.0)
    net.deposit("in", Token(payload={"i": 0}))
    net.deposit("in", Token(payload={"i": 1}, available_at=20.0))
    with net:
        result = net.drive_to_quiescence()
    assert result.ticks == 1
    assert _payloads(net, "out") == [0, 1], "the cooling token fires only once it is available"


def _key(token: Token) -> object:
    return token.payload["i"]


@pytest.mark.parametrize(
    ("arc", "kwargs", "message"),
    [
        (InputArc("in", join_key=_key), {}, "join_key"),
        (InputArc("in", key=_key), {}, "key"),
        (InputArc("in", filter=odd_only), {}, "filter"),
        (InputArc("in", consume_all=True), {}, "consume_all"),
        (InputArc("in"), {"binding_priority_key": _key}, "binding_priority_key"),
    ],
)
def test_settings_a_selector_does_not_apply_are_rejected(arc, kwargs, message):
    with pytest.raises(ValueError, match=message):
        Transition("t", [arc], [OutputArc("out")], _identity, binding_selector=newest, **kwargs)


def test_a_non_callable_selector_is_rejected():
    with pytest.raises(TypeError, match="binding_selector must be a callable"):
        Transition("t", [InputArc("in")], [OutputArc("out")], _identity, binding_selector="newest")


def test_an_arc_changed_after_construction_is_reported_not_fired():
    net = _one_arc_net(newest)
    net.transitions["t"].inputs[0].filter = odd_only
    errors: list[BaseException] = []
    net.on_error = lambda _name, error, _token: errors.append(error)
    with net:
        assert net.step() is False
    assert errors and "filter" in str(errors[0])


def skill(token: Token) -> object:
    return token.payload["skill"]


def opaque_skill(token: Token) -> object:
    return dict(token.payload)["skill"] if callable(print) else None


def _match_by(key):
    def select(view: BindingView):
        jobs = view.groups(0, key)
        for server in view.available(1):
            group = jobs.get(server.payload["skill"])
            if group:
                return [group[len(group) - 1], server]
        return None

    return select


match_certified = _match_by(skill)
match_uncertified = _match_by(opaque_skill)


@pytest.mark.parametrize("selector", [match_certified, match_uncertified], ids=["indexed", "scanned"])
def test_groups_serve_indexed_and_scanned_keys_alike(selector):
    net = PetriNet(seed=1, places=[Place("jobs"), Place("servers"), Place("done")])
    net.add_transition(
        Transition(
            "dispatch",
            [InputArc("jobs"), InputArc("servers")],
            [OutputArc("done", count=2)],
            _identity,
            binding_selector=selector,
        )
    )
    for i in range(9):
        net.deposit("jobs", Token(payload={"i": i, "skill": i % 3}))
    net.deposit("servers", Token(payload={"i": 100, "skill": 2}))
    net.deposit("servers", Token(payload={"i": 101, "skill": 0}))
    with net:
        net.drive_to_quiescence()
    assert _payloads(net, "done") == [8, 100, 6, 101]
    indexes = net._selector_indexes.get(("dispatch", 0), {})
    assert bool(indexes) is (selector is match_certified)


def test_a_group_supports_len_index_and_iteration():
    seen = {}

    def select(view: BindingView):
        group = view.group(0, skill, 1)
        seen["len"] = len(group)
        seen["items"] = [t.payload["i"] for t in group]
        seen["last"] = group[-1].payload["i"]
        seen["slice"] = [t.payload["i"] for t in group[1:]]
        seen["missing"] = len(view.group(0, skill, 99))
        return None

    net = PetriNet(seed=1, places=[Place("jobs"), Place("out")])
    net.add_transition(Transition("t", [InputArc("jobs")], [OutputArc("out")], _identity, binding_selector=select))
    for i in range(6):
        net.deposit("jobs", Token(payload={"i": i, "skill": i % 2}))
    with net:
        net.step()
    assert seen == {"len": 3, "items": [1, 3, 5], "last": 5, "slice": [3, 5], "missing": 0}


def random_pick(view: BindingView):
    tokens = list(view.available(0))
    return [tokens[view.random.randrange(len(tokens))]]


def _random_order(seed: int, *, probes: int = 0) -> list[int]:
    net = PetriNet(seed=seed, places=[Place("in"), Place("out")])
    net.add_transition(Transition("t", [InputArc("in")], [OutputArc("out")], _identity, binding_selector=random_pick))
    for i in range(30):
        net.deposit("in", Token(payload={"i": i}))
    with net:
        while True:
            for _ in range(probes):
                net.is_dead()
                net.is_quiescent()
            if not net.step():
                break
            net._await_inflight()
    return _payloads(net, "out")


def test_view_random_is_seeded_and_ignores_how_often_the_selector_is_asked():
    order = _random_order(7)
    assert sorted(order) == list(range(30))
    assert order != list(range(30))
    assert _random_order(7) == order
    assert _random_order(7, probes=3) == order, "extra probes between firings do not shift the stream"
    assert _random_order(8) != order


def test_view_random_is_the_same_across_processes():
    script = (
        "import sys; sys.path.insert(0, 'benchmarks'); import bench_dispatch_selector as b; "
        "print([b._first_pick(seed, 'selector') for seed in range(5)])"
    )
    outputs = set()
    for hash_seed in ("1", "2"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed, "PYTHONPATH": str(ROOT / "src")}
        done = subprocess.run(
            [sys.executable, "-W", "ignore", "-c", script],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        outputs.add(done.stdout.strip())
    assert len(outputs) == 1


def test_a_selector_does_not_draw_from_the_nets_own_stream():
    net = _one_arc_net(random_pick, n=10)
    state = net._rng.getstate()
    with net:
        net.is_dead()
        net.is_quiescent()
    assert net._rng.getstate() == state
