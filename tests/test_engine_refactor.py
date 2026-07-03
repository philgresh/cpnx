from collections import deque

import pytest

from cpnx.engine import PetriNet
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

    net._invoke_transition_callbacks(transition, False, 0.1, Exception("test"), [Token({})], [], [])
    assert error_called

    error_called = False
    net._invoke_transition_callbacks(transition, False, 0.1, KeyboardInterrupt("test"), [Token({})], [], [])
    assert not error_called


def test_invoke_transition_callbacks_zero_dead_letters():
    net = PetriNet()
    transition = Transition(name="t", inputs=[], outputs=[], action=lambda t: t)

    dl_called = False

    def mock_on_dl(t_name, token):
        nonlocal dl_called
        dl_called = True

    net.on_token_dead_lettered = mock_on_dl

    net._invoke_transition_callbacks(transition, False, 0.1, Exception("test"), [], [Token({})], [])
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
    out_deque = deque([Token({})])

    planned_deposits, plan_error = net._plan_and_validate_deposits(transition, active_outputs, res_deque, out_deque)
    assert plan_error is not None
    assert "Place 'p_out' is not registered." in str(plan_error)
    assert planned_deposits == []


def test_enact_planned_deposits():
    net = PetriNet()
    p_out = Place("p_out")
    net.add_place(p_out)

    token = Token({})
    planned = [("p_out", token)]
    out_arc = OutputArc("p_out")
    active_outputs = [(out_arc, False)]
    res_deque = deque()
    out_deque = deque([token])

    with net._lock:
        deposited = net._enact_planned_deposits(planned, active_outputs, res_deque, out_deque)

    assert len(deposited) == 1
    assert deposited[0] == ("p_out", token)
    assert len(p_out) == 1
    # Check that it popped from deque
    assert len(out_deque) == 0


def test_return_leftover_resources():
    net = PetriNet()
    p_in = Place("p_in")
    net.add_place(p_in)

    resource_token = Token({"r": 1}, color="resource")
    token_sources = [("p_in", resource_token)]
    res_deque = deque([resource_token])

    with net._lock:
        returned = net._return_leftover_resources(res_deque, token_sources)

    assert len(returned) == 1
    assert returned[0] == ("p_in", resource_token)
    assert len(p_in) == 1


def test_execute_transition_action():
    net = PetriNet()

    def action(tokens):
        return tokens

    transition = Transition(name="t", inputs=[], outputs=[], action=action)
    token = Token({})
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


def test_try_commit_transition_unexpected_error():
    net = PetriNet()
    p_out = Place("p_out")
    net.add_place(p_out)
    transition = Transition(name="t", inputs=[], outputs=[OutputArc("p_out")], action=lambda t: t)

    def failing_plan(*args, **kwargs):
        raise SystemExit("fatal")

    net._plan_and_validate_deposits = failing_plan

    with pytest.raises(SystemExit, match="fatal"):
        net._try_commit_transition(transition, [], [Token({})], [])


def test_rollback_failed_transition():
    net = PetriNet()
    p_in = Place("p_in")
    net.add_place(p_in)

    token = Token({"data": 1})
    res_token = Token({"r": 1}, color="resource")

    transition = Transition(name="t", inputs=[], outputs=[], action=lambda t: t, max_retries=1)

    token_sources = [("p_in", token), ("p_in", res_token)]

    with net._lock:
        deposited, dl_data, data_tokens = net._rollback_failed_transition(transition, token_sources)

    assert len(deposited) == 2
    assert len(dl_data) == 0
    assert len(data_tokens) == 1
    assert data_tokens[0] == token
    assert deposited[0][0] == "p_in"  # res token
    assert deposited[1][0] == "p_in"  # data token retry

    token2 = token.evolve(attempts=1)
    token_sources2 = [("p_in", token2)]

    with net._lock:
        deposited2, dl_data2, data_tokens2 = net._rollback_failed_transition(transition, token_sources2)

    assert len(deposited2) == 1
    assert deposited2[0][0] == net.error_place
    assert len(dl_data2) == 1
    assert len(data_tokens2) == 1


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
    import time

    from cpnx.transitions import InputArc

    net = PetriNet()
    p = Place("p")
    arc = InputArc("p", settle_secs=1.0)

    # 0 settle time always met
    assert net._is_settle_time_met(p, InputArc("p", settle_secs=0.0))

    # Real time
    p.last_deposit_time = 0.0
    old_mono = time.monotonic
    time.monotonic = lambda: 0.5
    try:
        assert not net._is_settle_time_met(p, arc)
        time.monotonic = lambda: 1.5
        assert net._is_settle_time_met(p, arc)
    finally:
        time.monotonic = old_mono

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
    from cpnx.places import Place

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
