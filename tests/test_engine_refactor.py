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
        transition, token_sources, deposit=fake_deposit, retry_delay=0.0, error_place="__error__"
    )

    assert len(deposited) == 2
    assert len(dl_data) == 0
    assert len(data_tokens) == 1
    assert data_tokens[0] == token
    assert deposited[0][0] == "p_in"  # resource token returned to source
    assert deposited[1][0] == "p_in"  # data token retried
    assert deposited_calls == deposited  # fake_deposit was called for every deposit

    # Exhausted token → dead-lettered
    deposited_calls.clear()
    token2 = token.evolve(attempts=1)
    token_sources2 = [("p_in", token2)]

    deposited2, dl_data2, data_tokens2 = _rollback_failed_transition(
        transition, token_sources2, deposit=fake_deposit, retry_delay=0.0, error_place="__error__"
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
    arc_true = OutputArc("p_data", expression="len(tokens) > 0")
    arc_false = OutputArc("p_data", expression="False")

    transition = Transition(
        "t",
        inputs=[],
        outputs=[arc_always, arc_true, arc_false],
        action=lambda t: t,
    )

    token = Token()
    active = net._evaluate_output_guards(transition, [token])

    assert len(active) == 2
    # OutputArc.expression has compare=False, so use identity checks
    active_arcs = [a for a, _ in active]
    assert any(a is arc_always for a in active_arcs)
    assert any(a is arc_true for a in active_arcs)
    assert not any(a is arc_false for a in active_arcs)

    # With no tokens the string guard also evaluates to False
    active_empty = net._evaluate_output_guards(transition, [])
    assert len(active_empty) == 1
    assert active_empty[0][0] is arc_always

    # Callable expression
    arc_callable = OutputArc("p_data", expression=lambda tokens: len(tokens) == 0)
    t2 = Transition("t2", inputs=[], outputs=[arc_callable], action=lambda t: t)
    assert len(net._evaluate_output_guards(t2, [token])) == 0
    assert len(net._evaluate_output_guards(t2, [])) == 1


def test_check_input_preconditions():
    from cpnx.transitions import InputArc

    net = PetriNet()
    p_in = Place("p_in")
    net.add_place(p_in)

    t1 = Transition("t1", inputs=[InputArc("p_in")], outputs=[], action=lambda t: t)
    # No tokens, should fail
    ok, tokens = net._check_input_preconditions(t1, 0.0)
    assert not ok
    assert tokens == []

    # Unregistered place
    t2 = Transition("t2", inputs=[InputArc("unknown")], outputs=[], action=lambda t: t)
    ok, tokens = net._check_input_preconditions(t2, 0.0)
    assert not ok

    # Token available
    token = Token()
    p_in.deposit(token)
    ok, tokens = net._check_input_preconditions(t1, 0.0)
    assert ok
    assert tokens == [token]


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

    # string expression
    arc2 = InputArc("p", expression="tokens")
    res = net._resolve_input_tokens(arc2, available)
    assert res == available[:1]  # count is 1 by default

    # exception in expression
    arc3 = InputArc("p", expression="1/0")
    assert net._resolve_input_tokens(arc3, available) is None


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
    t2 = Transition("t", inputs=[], outputs=[OutputArc("p_out", expression="True")], action=lambda t: t)
    assert net._check_output_capacity(t2)


def test_check_transition_guard():
    net = PetriNet()
    t = Transition("t", inputs=[], outputs=[], action=lambda t: t)

    # no guard
    assert net._check_transition_guard(t, [])

    # string guard
    t2 = Transition("t", inputs=[], outputs=[], action=lambda t: t, guard="len(tokens) > 0")
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


def test_sync_subnet_time():
    net = PetriNet()
    subnet = PetriNet()

    # model_time None — advance_time must not be called (_model_time stays unset)
    assert net._model_time is None
    net._sync_subnet_time(subnet)
    assert subnet._model_time is None

    # model_time set — subnet advances to match
    net._model_time = 10.0
    net._sync_subnet_time(subnet)
    assert subnet._model_time == 10.0


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
