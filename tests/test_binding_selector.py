"""`Transition.binding_selector`: outside logic chooses the binding a transition fires (#80, ADR 0013)."""

import os
import subprocess
import sys
import time
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
    indexes = net._selector_indexes.get("dispatch", {}).get(0, {})
    assert bool(indexes) is (selector is match_certified)


@pytest.mark.parametrize("key", [skill, opaque_skill], ids=["indexed", "scanned"])
def test_a_group_supports_len_index_and_iteration(key, monkeypatch):
    from cpnx.places import _JoinGroup

    reads = {"n": 0}
    real_token_at = _JoinGroup.token_at

    def counting_token_at(self, position):
        reads["n"] += 1
        return real_token_at(self, position)

    monkeypatch.setattr(_JoinGroup, "token_at", counting_token_at)
    seen = {}
    asks = {"n": 0}

    def select(view: BindingView):
        asks["n"] += 1
        group = view.group(0, key, 1)
        if asks["n"] == 1:
            return None  # the first sighting of a key is scanned; the second is indexed
        seen["len"] = len(group)
        seen["items"] = [t.payload["i"] for t in group]
        seen["last"] = group[-1].payload["i"]
        seen["slice"] = [t.payload["i"] for t in group[1:]]
        seen["missing"] = len(view.group(0, key, 99))
        for position in (3, -4):
            with pytest.raises(IndexError):
                group[position]
        return None

    net = PetriNet(seed=1, places=[Place("jobs"), Place("out")])
    net.add_transition(Transition("t", [InputArc("jobs")], [OutputArc("out")], _identity, binding_selector=select))
    for i in range(6):
        net.deposit("jobs", Token(payload={"i": i, "skill": i % 2}))
    with net:
        net.step()
        net.step()
    assert seen == {"len": 3, "items": [1, 3, 5], "last": 5, "slice": [3, 5], "missing": 0}
    assert (reads["n"] > 0) is (key is skill), "the indexed group reads through the place index"


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


_CROSS_PROCESS_SCRIPT = """
from cpnx import BindingView, InputArc, OutputArc, PetriNet, Place, Token, Transition


def keep(tokens):
    return tokens


def pick(view: BindingView):
    tokens = list(view.available(0))
    return [tokens[view.random.randrange(len(tokens))]]


def order(seed):
    net = PetriNet(seed=seed, places=[Place("in"), Place("out")])
    net.add_transition(Transition("dispatch", [InputArc("in")], [OutputArc("out")], keep, binding_selector=pick))
    for i in range(50):
        net.deposit("in", Token(payload={"i": i}))
    with net:
        net.drive_to_quiescence()
    return [t.payload["i"] for t in net.marking["out"]]


print([order(seed) for seed in range(3)])
"""


def test_view_random_is_the_same_across_processes(tmp_path):
    script = tmp_path / "selector_order.py"
    script.write_text(_CROSS_PROCESS_SCRIPT)
    outputs = set()
    for hash_seed in ("1", "2"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed, "PYTHONPATH": str(ROOT / "src")}
        done = subprocess.run(
            [sys.executable, "-W", "ignore", str(script)],
            cwd=tmp_path,
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


def test_a_repeated_failure_is_reported_once_and_again_when_it_changes():
    message = {"text": "no server"}

    def flaky(view: BindingView):
        raise LookupError(message["text"])

    net = _one_arc_net(flaky)
    errors: list[BaseException] = []
    net.on_error = lambda _name, error, _token: errors.append(error)
    with net:
        for _ in range(3):
            assert net.step() is False
            net.is_quiescent()
            net.is_dead()
        assert len(errors) == 1
        message["text"] = "no queue"
        assert net.step() is False
    assert len(errors) == 2 and "no queue" in str(errors[1])


def test_a_failure_reports_again_after_the_transition_fires():
    mode = {"fail": True}

    def switchable(view: BindingView):
        if mode["fail"]:
            raise LookupError("not now")
        return [next(view.available(0))]

    net = _one_arc_net(switchable)
    errors: list[BaseException] = []
    net.on_error = lambda _name, error, _token: errors.append(error)
    with net:
        assert net.step() is False
        assert net.step() is False
        assert len(errors) == 1
        mode["fail"] = False
        assert net.step() is True
        net._await_inflight()
        mode["fail"] = True
        assert net.step() is False
    assert len(errors) == 2


def test_a_failure_reports_again_after_the_selector_recovers_without_firing():
    mode = {"fail": True}

    def switchable(view: BindingView):
        if mode["fail"]:
            raise LookupError("not now")
        return None

    net = _one_arc_net(switchable)
    errors: list[BaseException] = []
    net.on_error = lambda _name, error, _token: errors.append(error)
    with net:
        net.step()
        mode["fail"] = False
        net.step()
        mode["fail"] = True
        net.step()
    assert len(errors) == 2


def counts_what_it_sees(view: BindingView):
    raise LookupError(f"sees {len(list(view.available(0)))}")


def test_timed_and_timing_ignoring_failures_each_report_once():
    net = PetriNet(seed=1, places=[Place("in"), Place("out")])
    net.add_transition(
        Transition("t", [InputArc("in")], [OutputArc("out")], _identity, binding_selector=counts_what_it_sees)
    )
    net.advance_time(10.0)
    net.deposit("in", Token(payload={"i": 0}))
    net.deposit("in", Token(payload={"i": 1}, available_at=20.0))
    errors: list[BaseException] = []
    net.on_error = lambda _name, error, _token: errors.append(error)
    with net:
        for _ in range(5):
            net.step()
            net.is_quiescent()
            net.is_dead()
    messages = sorted(str(e.__cause__) for e in errors)
    assert messages == ["sees 1", "sees 2"]


def _run_with_deadline(fn, secs: float = 10.0):
    import threading

    box: dict[str, object] = {}
    worker = threading.Thread(target=lambda: box.setdefault("value", fn()), daemon=True)
    worker.start()
    worker.join(secs)
    assert not worker.is_alive(), "the net deadlocked"
    return box["value"]


def test_a_selector_calling_back_into_the_net_is_reported_not_deadlocked():
    holder: dict[str, PetriNet] = {}

    def nosy(view: BindingView):
        holder["net"].marking  # noqa: B018 -- the call back into the net is the point
        return None

    net = _one_arc_net(nosy)
    holder["net"] = net
    errors: list[BaseException] = []
    net.on_error = lambda _name, error, _token: errors.append(error)
    with net:
        assert _run_with_deadline(net.step) is False
        assert len(net.marking["in"]) == 5, "the lock is restored after the call"
    assert errors and "called back into the net" in str(errors[0])


def test_another_thread_waits_on_the_lock_while_a_selector_runs():
    import threading

    entered = threading.Event()
    release = threading.Event()

    def slow(view: BindingView):
        entered.set()
        release.wait(5)
        return None

    net = _one_arc_net(slow)
    seen: dict[str, object] = {}

    def reader():
        entered.wait(5)
        seen["marking"] = len(net.marking["in"])

    with net:
        stepper = threading.Thread(target=net.step, daemon=True)
        stepper.start()
        assert entered.wait(5), "the selector is running, holding the engine lock"
        other = threading.Thread(target=reader, daemon=True)
        other.start()
        other.join(0.3)
        assert other.is_alive() and "marking" not in seen, "the other thread waits for the engine lock"
        release.set()
        other.join(5)
        stepper.join(5)
    assert seen == {"marking": 5}


def test_a_selector_assigned_on_a_live_net_is_asked_not_its_cached_search_binding():
    net = PetriNet(seed=1, places=[Place("in"), Place("out")])
    net.add_transition(Transition("t", [InputArc("in")], [OutputArc("out")], _identity))
    for i in range(3):
        net.deposit("in", Token(payload={"i": i}))
    with net:
        assert net.is_quiescent() is False
        assert "t" in net._enabled_bindings, "the scheduler cached the search's binding"
        net.transitions["t"].binding_selector = newest
        net.drive_to_quiescence()
        assert "t" in net._volatile_transitions
    assert _payloads(net, "out") == [2, 1, 0], "every firing is the selector's choice"


def test_a_selector_assigned_on_a_live_net_releases_the_old_join_index():
    net = PetriNet(seed=1, places=[Place("in"), Place("out")])
    net.add_transition(Transition("t", [InputArc("in", join_key=_key)], [OutputArc("out")], _identity))
    for i in range(3):
        net.deposit("in", Token(payload={"i": i}))
    with net:
        assert net.is_quiescent() is False
        assert net.places["in"]._store._join_indexes
        transition = net.transitions["t"]
        transition.inputs[0].join_key = None
        transition.binding_selector = newest
        net.drive_to_quiescence()
    assert _payloads(net, "out") == [2, 1, 0]
    assert not net.places["in"]._store._join_indexes


def test_a_selector_index_is_released_when_its_arc_moves_to_another_place():
    net = PetriNet(seed=1, places=[Place("jobs"), Place("other"), Place("out")])
    net.add_transition(
        Transition("t", [InputArc("jobs")], [OutputArc("out")], _identity, binding_selector=match_certified_one)
    )
    for i in range(6):
        net.deposit("jobs", Token(payload={"i": i, "skill": 1}))
    with net:
        net.is_dead()
        net.is_dead()
        assert net.places["jobs"]._store._join_indexes
        net.transitions["t"].inputs[0].place = "other"
        net.deposit("other", Token(payload={"i": 9, "skill": 1}))
        net.is_dead()
    assert not net.places["jobs"]._store._join_indexes


def _count_certifications(monkeypatch) -> dict[str, int]:
    import cpnx.selection as selection

    calls = {"n": 0}
    real = selection.is_inline_safe

    def counting(key):
        calls["n"] += 1
        return real(key)

    monkeypatch.setattr(selection, "is_inline_safe", counting)
    return calls


def test_inline_keys_are_scanned_never_certified_or_indexed(monkeypatch):
    calls = _count_certifications(monkeypatch)

    def inline_key(view: BindingView):
        group = view.group(0, lambda t: t.payload["skill"], 0)
        return [group[0]] if group else None

    net = _jobs_net(inline_key)
    with net:
        net.drive_to_quiescence()
        for _ in range(20):
            net.is_dead()
    assert _payloads(net, "out") == [0, 3, 6, 9]
    assert calls["n"] == 0
    assert not net.places["jobs"]._store._join_indexes
    assert len(net._selector_keys) == 0


def _payload_key(field: str):
    def key(token: Token) -> object:
        return token.payload[field]

    return key


_BY_SKILL = _payload_key("skill")
_BY_A = _payload_key("a")


def test_two_keys_from_one_factory_are_each_certified_once(monkeypatch):
    calls = _count_certifications(monkeypatch)

    def both(view: BindingView):
        view.groups(0, _BY_SKILL)
        view.groups(0, _BY_A)
        return None

    net = _jobs_net(both)
    with net:
        for _ in range(10):
            net.is_dead()
    assert calls["n"] == 2
    assert len(net.places["jobs"]._store._join_indexes) == 2


def test_a_callable_does_not_borrow_another_functions_verdict():
    from cpnx.certification import is_inline_safe
    from cpnx.selection import KeyCertifications

    class Pretender:
        __func__ = staticmethod(skill)

        def __call__(self, token: Token) -> object:
            return token.payload["skill"]

    cache = KeyCertifications()
    assert cache.inline_safe(skill) is True
    pretender = Pretender()
    assert cache.inline_safe(pretender) is is_inline_safe(pretender)


def test_binding_priority_key_set_after_a_selector_is_rejected():
    transition = Transition("t", [InputArc("in")], [OutputArc("out")], _identity, binding_selector=newest)
    with pytest.raises(ValueError, match="binding_priority_key"):
        transition.binding_priority_key = _key


def clock_pick(view: BindingView):
    tokens = list(view.available(0))
    return [tokens[int(time.time()) % len(tokens)]]


def test_risk_report_lints_the_selector():
    with pytest.warns(Warning, match="binding_selector"):
        net = _one_arc_net(clock_pick)
    roles = {f["role"] for entry in net.risk_report()["findings"] for f in entry["findings"]}
    assert "binding_selector" in roles


def test_a_selector_transition_keeps_the_incremental_scheduler_under_a_search_policy():
    from cpnx import BindingPolicy

    net = PetriNet(seed=1, binding_policy=BindingPolicy.RANDOM, places=[Place("in"), Place("out")])
    net.add_transition(Transition("t", [InputArc("in")], [OutputArc("out")], _identity, binding_selector=newest))
    assert net._incremental_eligible
    net.binding_policy = BindingPolicy.PRIORITY
    assert net._incremental_eligible
    net.transitions["t"].binding_selector = None
    net.deposit("in", Token(payload={"i": 0}))
    with net:
        net.drive_to_quiescence()
    assert not net._incremental_eligible, "the transition searches again once its selector is cleared"


def _jobs_net(selector) -> PetriNet:
    net = PetriNet(seed=1, places=[Place("jobs"), Place("out")])
    net.add_transition(Transition("t", [InputArc("jobs")], [OutputArc("out")], _identity, binding_selector=selector))
    for i in range(12):
        net.deposit("jobs", Token(payload={"i": i, "skill": i % 3, "a": i % 2, "b": i % 4, "c": i % 5}))
    return net


def test_clearing_a_selector_releases_its_place_indexes():
    net = _jobs_net(match_certified_one)
    with net:
        for _ in range(2):  # an index is built once the same key is passed a second time
            net.step()
            net._await_inflight()
        assert net.places["jobs"]._store._join_indexes
        net.transitions["t"].binding_selector = None
        net.step()
        net._await_inflight()
    assert net._selector_indexes == {}
    assert not net.places["jobs"]._store._join_indexes


def match_certified_one(view: BindingView):
    group = view.group(0, skill, 0)
    return [group[0]] if group else None


def key_a(token: Token) -> object:
    return token.payload["a"]


def key_b(token: Token) -> object:
    return token.payload["b"]


def key_c(token: Token) -> object:
    return token.payload["c"]


def key_i(token: Token) -> object:
    return token.payload["i"]


_ROTATION = (skill, key_a, key_b, key_c, key_i)


def test_a_selector_arc_keeps_at_most_four_place_indexes():
    turn = {"n": 0}

    def rotating(view: BindingView):
        key = _ROTATION[turn["n"] % len(_ROTATION)]
        turn["n"] += 1
        view.groups(0, key)
        return None

    net = _jobs_net(rotating)
    place = net.places["jobs"]
    with net:
        for _ in range(5):  # first sighting of each key: scanned, no index yet
            net.is_dead()
        assert not place._store._join_indexes
        for _ in range(4):
            net.is_dead()
        first_ids = set(place._store._join_indexes)
        assert len(first_ids) == 4
        net.is_dead()
    registered = net._selector_indexes["t"][0]
    assert len(registered) == 4 and len(place._store._join_indexes) == 4
    assert len(first_ids - set(place._store._join_indexes)) == 1, "the oldest index was unregistered"
    assert {entry[0] for entry in registered.values()} == set(_ROTATION[1:])


def cooling(view: BindingView):
    return [view.arcs and _COOLING["token"]]


_COOLING: dict[str, Token] = {}


def test_choosing_a_cooling_token_is_rejected():
    net = PetriNet(seed=1, places=[Place("in"), Place("out")])
    net.add_transition(Transition("t", [InputArc("in")], [OutputArc("out")], _identity, binding_selector=cooling))
    net.advance_time(10.0)
    net.deposit("in", Token(payload={"i": 0}))
    late = Token(payload={"i": 1}, available_at=20.0)
    net.deposit("in", late)
    _COOLING["token"] = late
    errors: list[BaseException] = []
    net.on_error = lambda _name, error, _token: errors.append(error)
    with net:
        assert net.step() is False
    assert errors and "not available" in str(errors[0])


def by_mapping(view: BindingView):
    return {1: next(view.available(1)), 0: list(view.available(0))[-2:]}


def test_a_mapping_choice_and_a_multi_token_arc_fire():
    net = PetriNet(seed=1, places=[Place("jobs"), Place("servers"), Place("out")])
    net.add_transition(
        Transition(
            "t",
            [InputArc("jobs", count=2), InputArc("servers")],
            [OutputArc("out", count=3)],
            _identity,
            binding_selector=by_mapping,
        )
    )
    for i in range(4):
        net.deposit("jobs", Token(payload={"i": i}))
    net.deposit("servers", Token(payload={"i": 100}))
    with net:
        assert net.step() is True
        net._await_inflight()
    assert sorted(_payloads(net, "out")) == [2, 3, 100]
    assert _payloads(net, "jobs") == [0, 1]


def test_available_pages_through_a_deep_place_in_order():
    seen: dict[str, list[int]] = {}

    def everything(view: BindingView):
        seen["ids"] = [t.payload["i"] for t in view.available(0)]
        return None

    net = _one_arc_net(everything, n=300)
    with net:
        net.is_dead()
    assert seen["ids"] == list(range(300))


_FLAKY = {"fail": True}


def flaky_everywhere(view: BindingView):
    if _FLAKY["fail"]:
        raise LookupError("down")
    return None


def test_a_failure_reports_again_after_the_timed_view_recovers_while_a_token_was_cooling():
    _FLAKY["fail"] = True
    net = PetriNet(seed=1, places=[Place("in"), Place("out")])
    net.add_transition(
        Transition("t", [InputArc("in")], [OutputArc("out")], _identity, binding_selector=flaky_everywhere)
    )
    net.advance_time(10.0)
    net.deposit("in", Token(payload={"i": 0}))
    net.deposit("in", Token(payload={"i": 1}, available_at=20.0))
    errors: list[BaseException] = []
    net.on_error = lambda _name, error, _token: errors.append(error)
    with net:
        for _ in range(3):
            net.step()
            net.is_quiescent()
            net.is_dead()
        assert len(errors) == 1, "both views fail the same way: one report"
        net.advance_time(30.0)  # the cooling token matures, so only the timed view is asked
        _FLAKY["fail"] = False
        for _ in range(3):
            net.step()
            net.is_dead()
        _FLAKY["fail"] = True
        net.step()
    assert len(errors) == 2, "the recurrence reports, though the other view's latch is stale"


@pytest.mark.parametrize(
    "choice",
    [[None], [42], {0: 5}, [], {1: 5}],
    ids=["none", "int", "mapping-int", "too-few-choices", "mapping-wrong-arc"],
)
def test_a_choice_item_that_is_not_a_token_is_an_invalid_selection(choice):
    from cpnx import InvalidSelection

    net = _one_arc_net(lambda view: choice)
    errors: list[BaseException] = []
    net.on_error = lambda _name, error, _token: errors.append(error)
    with net:
        assert net.step() is False
    assert errors and isinstance(errors[0].__cause__, InvalidSelection)


def test_a_conflict_releases_the_selectors_place_indexes():
    net = _jobs_net(match_certified_one)
    errors: list[BaseException] = []
    net.on_error = lambda _name, error, _token: errors.append(error)
    with net:
        net.is_dead()
        net.is_dead()
        assert net.places["jobs"]._store._join_indexes
        net.transitions["t"].inputs[0].key = _key
        net.is_dead()
    assert not net.places["jobs"]._store._join_indexes
    assert errors and "key" in str(errors[0])


def test_an_arc_moved_to_an_empty_place_releases_the_old_index():
    net = PetriNet(seed=1, places=[Place("jobs"), Place("empty"), Place("out")])
    net.add_transition(
        Transition("t", [InputArc("jobs")], [OutputArc("out")], _identity, binding_selector=match_certified_one)
    )
    for i in range(4):
        net.deposit("jobs", Token(payload={"i": i, "skill": 0}))
    with net:
        net.is_dead()
        net.is_dead()
        assert net.places["jobs"]._store._join_indexes
        net.transitions["t"].inputs[0].place = "empty"
        net.is_dead()
    assert not net.places["jobs"]._store._join_indexes


def test_replacing_a_selector_transition_releases_its_place_indexes():
    net = _jobs_net(match_certified_one)
    with net:
        net.is_dead()
        net.is_dead()
        assert net.places["jobs"]._store._join_indexes
        net.add_transition(Transition("t", [InputArc("jobs")], [OutputArc("out")], _identity))
    assert not net.places["jobs"]._store._join_indexes
    assert net._selector_indexes == {}


def other_skill_one(view: BindingView):
    group = view.group(0, key_a, 0)
    return [group[0]] if group else None


def test_swapping_the_selector_function_releases_the_old_ones_indexes_and_latches():
    net = _jobs_net(match_certified_one)
    errors: list[BaseException] = []
    net.on_error = lambda _name, error, _token: errors.append(error)
    with net:
        net.is_dead()
        net.is_dead()
        old_ids = set(net.places["jobs"]._store._join_indexes)
        assert old_ids
        net.transitions["t"].binding_selector = nothing
        net.is_dead()
        assert not net.places["jobs"]._store._join_indexes, "the new selector never reads the old indexes"
        net.transitions["t"].binding_selector = raises
        net.step()
        net.step()
        assert len(errors) == 1, "a repeated failure reports once"
        net.transitions["t"].binding_selector = raises_the_same
        net.step()
    assert len(errors) == 2, "a swap starts with a clean latch, so the same failure from another function reports"
    assert str(errors[0].__cause__) == str(errors[1].__cause__)


def raises_the_same(view: BindingView):
    raise LookupError("no server")


def first_of_each(view: BindingView):
    tokens = list(view.available(0))
    return [tokens[0], tokens[1]]


def _shared_place_net(n_tokens: int) -> PetriNet:
    net = PetriNet(seed=1, places=[Place("in"), Place("out")])
    net.add_transition(
        Transition(
            "t",
            [InputArc("in"), InputArc("in")],
            [OutputArc("out", count=2)],
            _identity,
            binding_selector=first_of_each,
        )
    )
    for i in range(n_tokens):
        net.deposit("in", Token(payload={"i": i}))
    return net


def test_arcs_sharing_a_place_are_not_asked_until_it_holds_enough_for_all():
    net = _shared_place_net(1)
    errors: list[BaseException] = []
    net.on_error = lambda _name, error, _token: errors.append(error)
    with net:
        assert net.step() is False
        assert net.is_dead()
    assert errors == []


def test_arcs_sharing_a_place_fire_disjoint_choices():
    net = _shared_place_net(4)
    with net:
        net.drive_to_quiescence()
    assert sorted(_payloads(net, "out")) == [0, 1, 2, 3]


def test_a_group_read_raises_index_error_for_a_token_that_left_the_place():
    from collections import OrderedDict

    from cpnx.places import _JoinGroup

    place = Place("jobs")
    tokens = [Token(payload={"i": i, "skill": 0}) for i in range(3)]
    for token in tokens:
        place.deposit(token)
    place.register_join_index(1, skill)
    group = place.peek_join(1, [0])[0]
    place.retrieve_specific([tokens[1]])  # changed directly, outside any engine lock
    assert group.token_at(1) is tokens[2], "the live list shifted under the view"
    with pytest.raises(IndexError):
        group.token_at(2)

    entry = (0, 0.0, 7)  # an index entry whose token is no longer stored
    orphan = _JoinGroup([entry], 1, entry, OrderedDict(), {})
    with pytest.raises(IndexError, match="left the place"):
        orphan.token_at(0)


def first_group(view: BindingView):
    groups = view.groups(0, skill)
    return [groups[next(iter(groups))][0]]


def _first_group_order(probes: int) -> list[int]:
    net = PetriNet(seed=1, places=[Place("jobs"), Place("out")])
    net.add_transition(Transition("t", [InputArc("jobs")], [OutputArc("out")], _identity, binding_selector=first_group))
    for i, s in enumerate(["A", "B", "A", "B", "A"]):
        net.deposit("jobs", Token(payload={"i": i, "skill": s}))
    with net:
        for _ in range(probes):
            net.is_dead()
        while net.step():
            net._await_inflight()
    return _payloads(net, "out")


def test_groups_key_order_does_not_depend_on_how_often_the_selector_is_asked():
    # The first ask of a key scans the place; later asks read an index whose groups were
    # created in another order once a group's oldest token is consumed. Both must agree.
    orders = {probes: _first_group_order(probes) for probes in (0, 1, 3)}
    assert orders[0] == orders[1] == orders[3] == [0, 1, 2, 3, 4], "always the oldest remaining job"


def test_a_failure_reports_again_once_its_tokens_have_drained():
    def rejects_bad(view: BindingView):
        if any(t.payload.get("bad") for t in view.available(0)):
            raise LookupError("bad token")
        return None

    net = PetriNet(seed=1, places=[Place("in"), Place("out")])
    net.add_transition(Transition("t", [InputArc("in")], [OutputArc("out")], _identity, binding_selector=rejects_bad))
    errors: list[BaseException] = []
    net.on_error = lambda _name, error, _token: errors.append(error)
    first = Token(payload={"i": 0, "bad": True})
    net.deposit("in", first)
    with net:
        net.step()
        net.step()
        assert len(errors) == 1
        net.places["in"].retrieve_specific([first])  # the user removes the bad token
        net.step()
        net.deposit("in", Token(payload={"i": 1, "bad": True}))
        net.step()
    assert len(errors) == 2, "the same failure over new tokens reports again"


def job_and_slot(view: BindingView):
    jobs = view.group(0, skill, 1)
    slots = list(view.available(1))
    return [jobs[-1], slots[0]] if jobs and slots else None


def test_a_selector_can_choose_from_a_resource_place():
    from cpnx import ResourcePlace

    net = PetriNet(seed=1, places=[Place("jobs"), ResourcePlace("slots", capacity=2), Place("done")])
    net.add_transition(
        Transition(
            "t",
            [InputArc("jobs"), InputArc("slots")],
            [OutputArc("done")],
            _identity,
            binding_selector=job_and_slot,
        )
    )
    for i in range(6):
        net.deposit("jobs", Token(payload={"i": i, "skill": i % 2}))
    with net:
        net.drive_to_quiescence()
    assert _payloads(net, "done") == [5, 3, 1], "the selector's skill-1 jobs, newest first"
    assert len(net.marking["slots"]) == 2, "each borrowed slot was returned"
