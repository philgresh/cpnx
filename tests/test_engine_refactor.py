from collections import deque
from unittest.mock import patch

import pytest

from cpnx.engine import PetriNet, _enact_planned_deposits, _return_leftover_resources, _rollback_failed_transition
from cpnx.places import Place
from cpnx.tokens import Token
from cpnx.transitions import OutputArc, Transition


def test_invoke_transition_callbacks_base_exception():
    net = PetriNet()
    transition = Transition(name="t", inputs=[], outputs=[], action=lambda t: t)

    error_called = False

    def mock_on_error(t_name, exc, token):
        nonlocal error_called
        error_called = True

    net.on_error = mock_on_error

    net._invoke_transition_callbacks(transition, False, 0.1, Exception("test"), [Token()], [], [])
    assert error_called

    error_called = False
    net._invoke_transition_callbacks(transition, False, 0.1, KeyboardInterrupt("test"), [Token()], [], [])
    assert not error_called


def test_invoke_transition_callbacks_zero_dead_letters():
    net = PetriNet()
    transition = Transition(name="t", inputs=[], outputs=[], action=lambda t: t)

    dl_called = False

    def mock_on_dl(t_name, token):
        nonlocal dl_called
        dl_called = True

    net.on_token_dead_lettered = mock_on_dl

    net._invoke_transition_callbacks(transition, False, 0.1, Exception("test"), [], [Token()], [])
    assert dl_called

    dl_called = False
    net._invoke_transition_callbacks(transition, False, 0.1, Exception("test"), [], [], [])
    assert not dl_called


def test_plan_and_validate_deposits_unregistered_place():
    net = PetriNet()
    transition = Transition(name="t", inputs=[], outputs=[OutputArc("p_out")], action=lambda t: t)

    out_arc = transition.outputs[0]
    active_outputs = [(out_arc, False)]
    res_deque = deque()
    out_deque = deque([Token()])

    planned_deposits, plan_error = net._plan_and_validate_deposits(transition, active_outputs, res_deque, out_deque)
    assert plan_error is not None
    assert "Place 'p_out' is not registered." in str(plan_error)
    assert planned_deposits == []


def test_plan_and_validate_deposits_resource_demand_exceeds_supply():
    net = PetriNet()
    out_arc = OutputArc("rp")
    transition = Transition(name="t", inputs=[], outputs=[out_arc], action=lambda t: t)
    active_outputs = [(out_arc, True)]  # is_res=True
    res_deque = deque()  # no resource tokens supplied
    out_deque = deque()

    _, plan_error = net._plan_and_validate_deposits(transition, active_outputs, res_deque, out_deque)
    assert isinstance(plan_error, ValueError)
    assert "resource output arcs require" in str(plan_error)


def test_plan_and_validate_deposits_data_demand_exceeds_supply():
    net = PetriNet()
    p_out = Place("p_out")
    net.add_place(p_out)
    out_arc = OutputArc("p_out")
    transition = Transition(name="t", inputs=[], outputs=[out_arc], action=lambda t: t)
    active_outputs = [(out_arc, False)]
    res_deque = deque()
    out_deque = deque()  # no data tokens supplied

    _, plan_error = net._plan_and_validate_deposits(transition, active_outputs, res_deque, out_deque)
    assert isinstance(plan_error, ValueError)
    assert "action returned" in str(plan_error)


def test_plan_and_validate_deposits_can_accept_fails():
    net = PetriNet()
    p_out = Place("p_out", color_set={"red"})
    net.add_place(p_out)
    out_arc = OutputArc("p_out")
    transition = Transition(name="t", inputs=[], outputs=[out_arc], action=lambda t: t)
    active_outputs = [(out_arc, False)]
    res_deque = deque()
    out_deque = deque([Token(color="blue")])  # wrong color for this place

    _, plan_error = net._plan_and_validate_deposits(transition, active_outputs, res_deque, out_deque)
    assert isinstance(plan_error, TypeError)
    assert "cannot accept token" in str(plan_error)


def test_plan_and_validate_deposits_k_bound_exceeded():
    net = PetriNet()
    p_out = Place("p_out", bound=1)
    net.add_place(p_out)
    p_out.deposit(Token())  # already at capacity

    out_arc = OutputArc("p_out")
    transition = Transition(name="t", inputs=[], outputs=[out_arc], action=lambda t: t)
    active_outputs = [(out_arc, False)]
    res_deque = deque()
    out_deque = deque([Token()])

    _, plan_error = net._plan_and_validate_deposits(transition, active_outputs, res_deque, out_deque)
    assert isinstance(plan_error, ValueError)
    assert "would exceed its bound" in str(plan_error)


def test_enact_planned_deposits():
    deposited_calls: list[tuple[str, Token]] = []

    token = Token()
    planned = [("p_out", token)]
    out_arc = OutputArc("p_out")
    active_outputs = [(out_arc, False)]
    res_deque = deque()
    out_deque = deque([token])

    deposited = _enact_planned_deposits(
        planned,
        active_outputs,
        res_deque,
        out_deque,
        deposit=lambda name, tok: deposited_calls.append((name, tok)),
    )

    assert deposited == [("p_out", token)]
    assert deposited_calls == [("p_out", token)]
    assert len(out_deque) == 0  # drained after deposit


def test_return_leftover_resources():
    deposited_calls: list[tuple[str, Token]] = []

    resource_token = Token(color="resource")
    token_sources = [("p_in", resource_token)]
    res_deque = deque([resource_token])

    returned = _return_leftover_resources(
        res_deque,
        token_sources,
        deposit=lambda name, tok: deposited_calls.append((name, tok)),
    )

    assert returned == [("p_in", resource_token)]
    assert deposited_calls == [("p_in", resource_token)]


def test_execute_transition_action():
    net = PetriNet()

    def action(tokens):
        return tokens

    transition = Transition(name="t", inputs=[], outputs=[], action=action)
    token = Token()
    token_sources = [("p_in", token)]

    success, output_tokens, error = net._execute_transition_action(transition, [token], token_sources)
    assert success is True
    assert error is None
    assert output_tokens == [token]

    def failing_action(tokens):
        raise ValueError("fail")

    transition2 = Transition(name="t2", inputs=[], outputs=[], action=failing_action)
    success, output_tokens, error = net._execute_transition_action(transition2, [token], token_sources)
    assert success is False
    assert isinstance(error, ValueError)
    assert output_tokens == []


def test_execute_transition_rolls_back_on_unexpected_commit_error():
    """When _try_commit_transition raises unexpectedly, _execute_transition must still roll back tokens."""
    net = PetriNet()
    p_src = Place("p_src")
    net.add_place(p_src)

    token = Token()
    consumed_tokens = [token]
    token_sources = [("p_src", token)]
    transition = Transition(name="t", inputs=[], outputs=[], action=lambda t: t)

    def blow_up(*args, **kwargs):
        raise RuntimeError("unexpected deposit error")

    net._plan_and_validate_deposits = blow_up
    net._running_count = 1  # prevent underflow in finally

    net._execute_transition(transition, consumed_tokens, token_sources)

    # Token must have been rolled back to p_src (retry path)
    assert len(p_src) == 1
    assert p_src.tokens[0].attempts == 1


def test_try_commit_transition_happy_path():
    net = PetriNet()
    p_out = Place("p_out")
    net.add_place(p_out)

    out_arc = OutputArc("p_out")
    transition = Transition(name="t", inputs=[], outputs=[out_arc], action=lambda t: t)
    token = Token()

    with net._lock:
        success, error, data_tokens, dl_data, deposited = net._try_commit_transition(transition, [token], [token], [])

    assert success is True
    assert error is None
    assert data_tokens == []
    assert dl_data == []
    assert len(deposited) == 1
    assert deposited[0] == ("p_out", token)
    assert len(p_out) == 1


def test_try_commit_transition_planned_error():
    net = PetriNet()
    # Output arc targets an unregistered place → plan validation returns an error
    out_arc = OutputArc("unregistered")
    transition = Transition(name="t", inputs=[], outputs=[out_arc], action=lambda t: t)
    token = Token()

    with net._lock:
        success, error, data_tokens, dl_data, deposited = net._try_commit_transition(transition, [token], [token], [])

    assert success is False
    assert error is not None
    assert deposited == []


def test_rollback_failed_transition():
    # No PetriNet, no lock — all dependencies are injected explicitly.
    deposited_calls: list[tuple[str, Token]] = []

    def fake_deposit(place_name: str, token: Token) -> None:
        deposited_calls.append((place_name, token))

    token = Token(payload={"data": 1})
    res_token = Token(color="resource")
    transition = Transition(name="t", inputs=[], outputs=[], action=lambda t: t, max_retries=1)

    # Resource token first so deposited order is deterministic
    token_sources = [("p_in", res_token), ("p_in", token)]

    deposited, dl_data, data_tokens = _rollback_failed_transition(
        transition, token_sources, deposit=fake_deposit, retry_delay=0.0, error_place="__error__", ref_time=10.0
    )

    assert len(deposited) == 2
    assert len(dl_data) == 0
    assert len(data_tokens) == 1
    assert data_tokens[0] == token
    assert deposited[0][0] == "p_in"  # resource token returned to source
    assert deposited[1][0] == "p_in"  # data token retried
    assert deposited[1][1].available_at == 10.0  # scheduled off ref_time, not the wall clock
    assert deposited_calls == deposited  # fake_deposit was called for every deposit

    # Exhausted token → dead-lettered
    deposited_calls.clear()
    token2 = token.evolve(attempts=1)
    token_sources2 = [("p_in", token2)]

    deposited2, dl_data2, data_tokens2 = _rollback_failed_transition(
        transition, token_sources2, deposit=fake_deposit, retry_delay=0.0, error_place="__error__", ref_time=10.0
    )

    assert len(deposited2) == 1
    assert deposited2[0][0] == "__error__"
    assert len(dl_data2) == 1
    assert len(data_tokens2) == 1


def test_evaluate_output_guards():
    net = PetriNet()
    p_data = Place("p_data")
    net.add_place(p_data)

    arc_always = OutputArc("p_data")
    arc_true = OutputArc("p_data", condition=lambda tokens: len(tokens) > 0)
    arc_false = OutputArc("p_data", condition=lambda tokens: False)

    transition = Transition(
        "t",
        inputs=[],
        outputs=[arc_always, arc_true, arc_false],
        action=lambda t: t,
    )

    token = Token()
    active = net._evaluate_output_guards(transition, [token])

    assert len(active) == 2
    # OutputArc.condition has compare=False, so use identity checks
    active_arcs = [a for a, _ in active]
    assert any(a is arc_always for a in active_arcs)
    assert any(a is arc_true for a in active_arcs)
    assert not any(a is arc_false for a in active_arcs)

    # With no tokens the callable guard also evaluates to False
    active_empty = net._evaluate_output_guards(transition, [])
    assert len(active_empty) == 1
    assert active_empty[0][0] is arc_always

    # Callable condition
    arc_callable = OutputArc("p_data", condition=lambda tokens: len(tokens) == 0)
    t2 = Transition("t2", inputs=[], outputs=[arc_callable], action=lambda t: t)
    assert len(net._evaluate_output_guards(t2, [token])) == 0
    assert len(net._evaluate_output_guards(t2, [])) == 1


def test_resolve_binding():
    from cpnx.engine import _flatten_binding
    from cpnx.transitions import InputArc

    net = PetriNet()
    p_in = Place("p_in")
    net.add_place(p_in)

    t1 = Transition("t1", inputs=[InputArc("p_in")], outputs=[], action=lambda t: t)
    # No tokens → not enabled
    assert net._resolve_binding(t1, 0.0) is None

    # Unregistered place → not enabled
    t2 = Transition("t2", inputs=[InputArc("unknown")], outputs=[], action=lambda t: t)
    assert net._resolve_binding(t2, 0.0) is None

    # Token available → binding pairs the arc with that token
    token = Token()
    p_in.deposit(token)
    binding = net._resolve_binding(t1, 0.0)
    assert binding is not None
    assert _flatten_binding(binding) == [token]


def test_is_settle_time_met():
    from cpnx.transitions import InputArc

    net = PetriNet()
    p = Place("p")
    arc = InputArc("p", settle_secs=1.0)

    # 0 settle time always met
    assert net._is_settle_time_met(p, InputArc("p", settle_secs=0.0))

    # Real time — not met then met
    p.last_deposit_time = 0.0
    with patch("time.monotonic", return_value=0.5):
        assert not net._is_settle_time_met(p, arc)
    with patch("time.monotonic", return_value=1.5):
        assert net._is_settle_time_met(p, arc)

    # Model time
    net._model_time = 2.0
    p.last_deposit_time_model = 1.5
    assert not net._is_settle_time_met(p, arc)
    net._model_time = 3.0
    assert net._is_settle_time_met(p, arc)


def test_resolve_input_tokens():
    from cpnx.transitions import InputArc

    net = PetriNet()
    t1, t2 = Token(), Token()
    available = [t1, t2]

    # consume_all
    arc = InputArc("p", consume_all=True)
    assert net._resolve_input_tokens(arc, available) == available

    # a filter that changes nothing still returns the (unchanged) pool
    arc2 = InputArc("p", filter=lambda t: True)
    res = net._resolve_input_tokens(arc2, available)
    assert res == available[:1]  # count is 1 by default

    # exception in key
    arc3 = InputArc("p", key=lambda t: 1 / 0)
    assert net._resolve_input_tokens(arc3, available) is None

    # exception in filter
    arc4 = InputArc("p", filter=lambda t: 1 / 0)
    assert net._resolve_input_tokens(arc4, available) is None

    # no key and no filter → FIFO passthrough
    arc5 = InputArc("p")
    assert net._resolve_input_tokens(arc5, available) == available[:1]

    # key-only: consumed in ascending key order (min-first), not insertion order
    hi = Token(payload={"priority": 2})
    lo = Token(payload={"priority": 1})
    arc_key = InputArc("p", count=2, key=lambda t: t.payload["priority"])
    assert net._resolve_input_tokens(arc_key, [hi, lo]) == [lo, hi]

    # filter-only: ineligible tokens are excluded, eligible ones keep insertion order
    keep = Token(payload={"ok": True})
    drop = Token(payload={"ok": False})
    arc_filter = InputArc("p", count=1, filter=lambda t: t.payload["ok"])
    assert net._resolve_input_tokens(arc_filter, [drop, keep]) == [keep]

    # both filter and key: filter first, then order what survives
    a = Token(payload={"ok": True, "priority": 3})
    b = Token(payload={"ok": False, "priority": 1})
    c = Token(payload={"ok": True, "priority": 1})
    arc_both = InputArc("p", count=2, filter=lambda t: t.payload["ok"], key=lambda t: t.payload["priority"])
    assert net._resolve_input_tokens(arc_both, [a, b, c]) == [c, a]

    # count not met (after filtering) → None, even though enough tokens are present overall
    arc_short = InputArc("p", count=2, filter=lambda t: t.payload["ok"])
    assert net._resolve_input_tokens(arc_short, [a, b]) is None


def test_check_output_capacity():
    net = PetriNet()
    p_out = Place("p_out", bound=1)
    net.add_place(p_out)

    t = Transition("t", inputs=[], outputs=[OutputArc("p_out")], action=lambda t: t)

    # capacity OK
    assert net._check_output_capacity(t)

    # capacity full
    p_out.deposit(Token())
    assert not net._check_output_capacity(t)

    # guarded arc ignores capacity
    t2 = Transition("t", inputs=[], outputs=[OutputArc("p_out", condition=lambda tokens: True)], action=lambda t: t)
    assert net._check_output_capacity(t2)


def test_check_transition_guard():
    net = PetriNet()
    t = Transition("t", inputs=[], outputs=[], action=lambda t: t)

    # no guard
    assert net._check_transition_guard(t, [])

    # callable guard (certified)
    t2 = Transition("t", inputs=[], outputs=[], action=lambda t: t, guard=lambda tokens: len(tokens) > 0)
    assert not net._check_transition_guard(t2, [])
    assert net._check_transition_guard(t2, [Token()])

    # callable guard
    t3 = Transition("t", inputs=[], outputs=[], action=lambda t: t, guard=lambda t: len(t) > 0)
    assert not net._check_transition_guard(t3, [])
    assert net._check_transition_guard(t3, [Token()])

    # exception in guard
    t4 = Transition("t", inputs=[], outputs=[], action=lambda t: t, guard=lambda t: 1 / 0)
    assert not net._check_transition_guard(t4, [Token()])


def test_verify_token_demand():
    # Not enough resources
    assert PetriNet._verify_token_demand("t", [(OutputArc("p", count=1), True)], 0, 1) is not None
    # Not enough data
    assert PetriNet._verify_token_demand("t", [(OutputArc("p", count=1), False)], 1, 0) is not None
    # Multi-count arc: demand=2, supply=1 → error
    assert PetriNet._verify_token_demand("t", [(OutputArc("p", count=2), False)], 0, 1) is not None
    # Multi-count arc: demand=2, supply=2 → OK
    assert PetriNet._verify_token_demand("t", [(OutputArc("p", count=2), False)], 0, 2) is None
    # No arcs at all → OK
    assert PetriNet._verify_token_demand("t", [], 0, 0) is None


def test_build_deposit_plan():
    r1, r2 = Token(color="resource"), Token(color="resource")
    d1, d2 = Token(), Token()
    res_deque = deque([r1, r2])
    out_deque = deque([d1, d2])

    plan = PetriNet._build_deposit_plan(
        [(OutputArc("p1", count=2), True), (OutputArc("p2", count=2), False)],
        res_deque,
        out_deque,
    )
    assert len(plan) == 4
    assert [name for name, _ in plan] == ["p1", "p1", "p2", "p2"]
    assert [tok for _, tok in plan] == [r1, r2, d1, d2]
    # Input deques must not be mutated
    assert list(res_deque) == [r1, r2]
    assert list(out_deque) == [d1, d2]


def test_verify_deposit_constraints():
    net = PetriNet()
    p = Place("p1", bound=1)
    net.add_place(p)
    # unregistered
    assert isinstance(net._verify_deposit_constraints([("unreg", Token())]), KeyError)
    # color violation
    from cpnx.places import ResourcePlace

    p2 = ResourcePlace("p2", capacity=0)
    net.add_place(p2)
    assert isinstance(net._verify_deposit_constraints([("p2", Token())]), TypeError)
    # bound violation
    assert isinstance(net._verify_deposit_constraints([("p1", Token()), ("p1", Token())]), ValueError)
    # OK
    assert net._verify_deposit_constraints([("p1", Token())]) is None


def test_dispatch_callbacks():
    net = PetriNet()
    fired_called = False
    error_called = False
    dl_called = False
    deposit_called = False

    def on_fired(n, d):
        nonlocal fired_called
        fired_called = True

    def on_err(n, e, t):
        nonlocal error_called
        error_called = True

    def on_dl(n, t):
        nonlocal dl_called
        dl_called = True

    def on_dep(n, t):
        nonlocal deposit_called
        deposit_called = True

    net.on_transition_fired = on_fired
    net.on_error = on_err
    net.on_token_dead_lettered = on_dl
    net.on_token_deposited = on_dep

    net._dispatch_transition_fired("t", 0.1)
    assert fired_called
    net._dispatch_transition_error("t", Exception(), [Token()])
    assert error_called
    net._dispatch_dead_letters("t", [Token()])
    assert dl_called
    net._dispatch_deposits([("p", Token())])
    assert deposit_called


def test_map_sockets_to_ports():
    assert PetriNet._map_sockets_to_ports({"p1": "s1", "p2": "s1"}) == {"s1": ["p1", "p2"]}
    assert PetriNet._map_sockets_to_ports({}) == {}
    assert PetriNet._map_sockets_to_ports({"p1": "s1", "p2": "s2"}) == {"s1": ["p1"], "s2": ["p2"]}


def test_verify_port_socket_boundaries():
    with pytest.raises(ValueError):
        PetriNet._verify_port_socket_boundaries([("s2", Token())], {"s1": ["p1"]})
    # No error when socket is mapped
    PetriNet._verify_port_socket_boundaries([("s1", Token())], {"s1": ["p1"]})
    # Empty token sources — never raises
    PetriNet._verify_port_socket_boundaries([], {})


def test_deposit_into_subnet():
    subnet = PetriNet()
    subnet.add_place(Place("p1"))
    src_token = Token()
    PetriNet._deposit_into_subnet(subnet, [("s1", src_token)], {"s1": ["p1"]})
    assert len(subnet.places["p1"]) == 1
    # evolve() is called — deposited token has a new id
    assert subnet.places["p1"].tokens[0].id != src_token.id


def test_subnet_clock_is_isolated_from_parent():
    """A subnet runs on its own wall clock — the parent's logical clock never crosses the port
    boundary. (This replaces an earlier `test_sync_subnet_time`, which asserted the opposite:
    that the parent pushed its logical time onto the subnet. That coupling stranded tokens under
    `drive_to_quiescence` because a subnet fires once per binding and the repeated push moved the
    subnet clock backward-or-equal, which `advance_time` rejects. See `tests/test_subnet.py`.)"""
    import time

    from cpnx.transitions import InputArc, SubstitutionTransition

    subnet = PetriNet(places=[Place("port_in"), Place("port_out")])
    subnet.add_transition(
        Transition(name="pass", inputs=[InputArc("port_in")], outputs=[OutputArc("port_out")], action=lambda t: t)
    )
    net = PetriNet(
        places=[Place("socket_in"), Place("socket_out")],
        transitions=[
            SubstitutionTransition(
                name="sub",
                inputs=[InputArc("socket_in")],
                outputs=[OutputArc("socket_out")],
                action=lambda t: t,
                subnet=subnet,
                port_socket_map={"port_in": "socket_in", "port_out": "socket_out"},
            )
        ],
    )
    # Parent on the logical clock; firing the subnet must not couple or advance the subnet's clock.
    net.advance_time(1000.0)
    with net:
        net.deposit("socket_in", Token())
        net.run(deadline=time.monotonic() + 2.0)
    assert subnet._model_time is None, "subnet clock was coupled to the parent's logical clock"
    assert len(net.places["socket_out"].tokens) == 1


def test_retrieve_subnet_outputs():
    net = PetriNet()
    subnet = PetriNet()
    subnet.add_place(Place("p1"))
    subnet.add_place(Place("p2"))
    t = Token()
    subnet.deposit("p1", t)
    subnet.deposit("p2", Token())

    # Only p1 maps to a socket in parent_outputs — p2 should be excluded
    out = net._retrieve_subnet_outputs(subnet, {"p1": "s1", "p2": "s_internal"}, ["s1"])
    assert len(out) == 1
    assert out[0] == t

    # Port not present in subnet — no error, just skipped
    out2 = net._retrieve_subnet_outputs(subnet, {"missing": "s1"}, ["s1"])
    assert out2 == []


def test_dispatch_transition_error_base_exception():
    net = PetriNet()
    called = False

    def on_err(n, e, t):
        nonlocal called
        called = True

    net.on_error = on_err

    # BaseException (not Exception) — on_error must NOT be called
    net._dispatch_transition_error("t", KeyboardInterrupt(), [Token()])
    assert not called

    # Regular Exception — on_error must be called
    net._dispatch_transition_error("t", ValueError("oops"), [Token()])
    assert called


def test_dispatch_transition_error_no_data_tokens():
    """When data_tokens is empty, on_error is called once with None as the token."""
    net = PetriNet()
    received_tokens: list = []

    net.on_error = lambda n, e, t: received_tokens.append(t)
    net._dispatch_transition_error("t", Exception("fail"), [])

    assert received_tokens == [None]


def test_dispatch_callbacks_none_handlers():
    """Dispatchers must be silent no-ops when the relevant handler is not set."""
    net = PetriNet()
    net._dispatch_transition_fired("t", 0.1)
    net._dispatch_transition_error("t", Exception(), [Token()])
    net._dispatch_dead_letters("t", [Token()])
    net._dispatch_deposits([("p", Token())])


def test_select_transition_to_fire():
    net = PetriNet()
    assert net._select_transition_to_fire() is None

    t1 = Transition("t1", inputs=[], outputs=[], action=lambda t: t, priority=1)
    t2 = Transition("t2", inputs=[], outputs=[], action=lambda t: t, priority=2)
    net.add_transition(t1)
    net.add_transition(t2)

    # Priority 1 should be selected over priority 2 (lower number = higher precedence).
    # _select_transition_to_fire now returns a (transition, binding) pair.
    selected = net._select_transition_to_fire()
    assert selected is not None
    transition, _binding = selected
    assert transition == t1


def test_consume_binding():
    net = PetriNet()
    p = Place("p")
    net.add_place(p)
    t = Token()
    p.deposit(t)

    from cpnx.transitions import InputArc

    arc = InputArc("p", count=1)
    trans = Transition("trans", inputs=[arc], outputs=[], action=lambda t: t)
    net.add_transition(trans)

    binding = net._resolve_binding(trans, None)
    consumed, sources = net._consume_binding(binding, None)
    assert consumed == [t]
    assert sources == [("p", t)]
    assert len(p) == 0


def test_consume_binding_rollback_on_error():
    net = PetriNet()
    p1 = Place("p1")
    p2 = Place("p2")
    net.add_place(p1)
    net.add_place(p2)
    t1 = Token()
    p1.deposit(t1)

    from cpnx.transitions import InputArc

    arc1 = InputArc("p1", count=1)
    arc2 = InputArc("p2", count=1)
    # A binding whose second arc names a token that is not actually present in p2 — the
    # retrieve_specific for arc2 raises after arc1's token has already been consumed.
    ghost = Token()
    binding = [(arc1, [t1]), (arc2, [ghost])]

    with pytest.raises(ValueError):
        net._consume_binding(binding, None)

    # t1 (consumed from p1 before arc2 failed) must be rolled back to p1.
    assert len(p1) == 1
    assert p1.tokens[0].id == t1.id


def test_wait_for_work():
    import threading

    net = PetriNet()
    # Check that it returns when deadline is past
    with patch("time.monotonic", return_value=10.0):
        net._wait_for_work(deadline=5.0, stop_event=None)  # Should return immediately

    # Check that it caps to cooldown interval or remaining time
    stop_event = threading.Event()
    with patch.object(net._work_available, "wait") as mock_wait:
        with patch("time.monotonic", return_value=0.0):
            net._wait_for_work(deadline=5.0, stop_event=stop_event)
            mock_wait.assert_called_once()
            # timeout is min(cooldown_interval=0.05, 0.1) = 0.05
            assert mock_wait.call_args[1]["timeout"] == 0.05


def test_validate_transition_arcs():
    from cpnx.transitions import InputArc

    net = PetriNet()
    p1 = Place("p1")
    net.add_place(p1)

    # OK
    t_ok = Transition("t1", inputs=[InputArc("p1")], outputs=[], action=lambda t: t)
    net._validate_transition_arcs("t1", t_ok)

    # Arc points to an unregistered place
    t_bad_place = Transition("t2", inputs=[InputArc("p2")], outputs=[], action=lambda t: t)
    with pytest.raises(KeyError):
        net._validate_transition_arcs("t2", t_bad_place)

    # Arc points to a transition
    t3 = Transition("t3", inputs=[], outputs=[], action=lambda t: t)
    net.add_transition(t3)
    t_bad_target = Transition("t4", inputs=[InputArc("t3")], outputs=[], action=lambda t: t)
    with pytest.raises(TypeError):
        net._validate_transition_arcs("t4", t_bad_target)


def test_rollback_data_token():
    from cpnx.engine import _rollback_data_token

    t = Transition("t", inputs=[], outputs=[], action=lambda x: x, max_retries=2)
    tok = Token()

    # Below retries: should evolve attempt count and schedule in future,
    # relative to the caller-supplied reference time (not the wall clock).
    dest, rb_tok, is_dl = _rollback_data_token(tok, "src", t, 5.0, "err", 10.0)
    assert dest == "src"
    assert rb_tok.attempts == 1
    assert rb_tok.available_at == 15.0
    assert not is_dl

    # Above retries: dead-lettered
    tok2 = Token(attempts=2)
    dest, rb_tok, is_dl = _rollback_data_token(tok2, "src", t, 5.0, "err", 10.0)
    assert dest == "err"
    assert is_dl
    assert rb_tok.available_at == 0.0


def test_arc_available():
    from cpnx.transitions import InputArc

    net = PetriNet()
    arc = InputArc("p1", count=1)
    # Missing place → None
    assert net._arc_available(arc, None, None, False) is None

    p = Place("p1")
    net.add_place(p)
    assert net._arc_available(arc, p, None, False) is None  # no tokens

    tok = Token()
    p.deposit(tok)
    assert net._arc_available(arc, p, None, False) == [tok]


def test_is_arc_active():
    from cpnx.transitions import OutputArc

    net = PetriNet()
    arc_none = OutputArc("p1")
    assert net._is_arc_active(arc_none, []) is True

    # Conditions are callables only; a None condition means the arc is always active.
    arc_callable = OutputArc("p1", condition=lambda t: len(t) > 0)
    assert net._is_arc_active(arc_callable, []) is False
    assert net._is_arc_active(arc_callable, [Token()]) is True


def test_process_rollback_token():
    from cpnx.engine import _process_rollback_token
    from cpnx.places import ResourcePlace

    t = Transition("t", inputs=[], outputs=[], action=lambda x: x)

    p = ResourcePlace("p", 1)
    tok_res = p.retrieve(1)[0]

    dest, rb_tok, is_dl = _process_rollback_token(tok_res, "src", t, 5.0, "err", 10.0)
    assert dest == "src"
    assert rb_tok is tok_res
    assert not is_dl

    tok_data = Token(attempts=0)
    dest2, rb_tok2, is_dl2 = _process_rollback_token(tok_data, "src", t, 5.0, "err", 10.0)
    assert dest2 == "src"
    assert rb_tok2.attempts == 1
    assert rb_tok2.available_at == 15.0
    assert not is_dl2


def test_arc_available_requires_full_count():
    from cpnx.transitions import InputArc

    net = PetriNet()
    p = Place("p1")
    net.add_place(p)
    arc = InputArc("p1", count=2)

    p.deposit(Token())
    # Only one token present, arc demands two → unmet (multiplicity rule).
    assert net._arc_available(arc, p, None, False) is None

    p.deposit(Token())
    result = net._arc_available(arc, p, None, False)
    assert result is not None
    assert len(result) == 2


def test_filter_highest_priority():
    t1 = Transition("t1", inputs=[], outputs=[], action=lambda t: t, priority=1)
    t2 = Transition("t2", inputs=[], outputs=[], action=lambda t: t, priority=2)
    t3 = Transition("t3", inputs=[], outputs=[], action=lambda t: t, priority=1)

    # _filter_highest_priority now operates on (transition, binding) pairs; the binding
    # payload is irrelevant to priority filtering, so empty bindings suffice here.
    assert PetriNet._filter_highest_priority([]) == []
    result = PetriNet._filter_highest_priority([(t1, []), (t2, []), (t3, [])])
    names = {t.name for t, _ in result}
    assert names == {"t1", "t3"}
    assert all(t.name != "t2" for t, _ in result)


def test_select_transition_to_fire_equal_priority():
    net = PetriNet()
    t1 = Transition("t1", inputs=[], outputs=[], action=lambda t: t)
    t2 = Transition("t2", inputs=[], outputs=[], action=lambda t: t)
    net.add_transition(t1)
    net.add_transition(t2)

    # Both have equal priority — random.choice means either can be selected.
    # Run many times and confirm both are returned at some point.
    seen = set()
    for _ in range(50):
        selected = net._select_transition_to_fire()
        assert selected is not None
        transition, _binding = selected
        seen.add(transition.name)
    assert seen == {"t1", "t2"}


def test_should_stop_run():
    import threading

    stop = threading.Event()

    # Neither condition met
    with patch("time.monotonic", return_value=1.0):
        assert not PetriNet._should_stop_run(deadline=5.0, stop_event=stop)

    # Deadline exceeded
    with patch("time.monotonic", return_value=10.0):
        assert PetriNet._should_stop_run(deadline=5.0, stop_event=None)

    # Stop event set
    stop.set()
    with patch("time.monotonic", return_value=1.0):
        assert PetriNet._should_stop_run(deadline=5.0, stop_event=stop)

    # No deadline, no event
    assert not PetriNet._should_stop_run(deadline=None, stop_event=None)


def test_get_deposit_counts():
    t1, t2 = Token(), Token()
    counts = PetriNet._get_deposit_counts([("p1", t1), ("p1", t2), ("p2", t1)])
    assert counts == {"p1": 2, "p2": 1}
    assert PetriNet._get_deposit_counts([]) == {}


def test_resolve_binding_consume_all():
    """consume_all arcs bind ALL available tokens, not just arc.count."""
    from cpnx.engine import _flatten_binding
    from cpnx.transitions import InputArc

    net = PetriNet()
    p = Place("p")
    net.add_place(p)

    t1, t2, t3 = Token(), Token(), Token()
    for t in (t1, t2, t3):
        p.deposit(t)

    arc = InputArc("p", consume_all=True)
    trans = Transition("t", inputs=[arc], outputs=[], action=lambda x: x)
    binding = net._resolve_binding(trans, None)

    assert binding is not None
    assert len(_flatten_binding(binding)) == 3  # all three, not just arc.count (default 1)
