import threading
import time

import pytest

from cpnx import InputArc, OutputArc, PetriNet, Place, SinkPlace, Token, Transition


def test_sink_place_keep_last_zero():
    sink = SinkPlace("sink", keep_last=0)
    for i in range(10):
        sink.deposit(Token(payload={"val": i}))

    assert len(sink) == 0
    assert len(sink.tokens) == 0
    assert sink.stats()["absorbed"] == 10
    assert sink.stats()["kept"] == 0


def test_sink_place_keep_last_n():
    sink = SinkPlace("sink", keep_last=3)
    for i in range(10):
        sink.deposit(Token(payload={"val": i}))

    assert len(sink) == 3
    assert len(sink.tokens) == 3
    assert sink.stats()["absorbed"] == 10
    assert sink.stats()["kept"] == 3

    # Last 3 kept tokens
    retained_vals = [t.payload["val"] for t in sink.tokens]
    assert retained_vals == [7, 8, 9]


def test_sink_place_terminal_and_quiescence():
    net = PetriNet()
    net.add_place(Place("input"))
    net.add_place(SinkPlace("sink", keep_last=1))
    net.add_place(Place("output"))

    net.add_transition(
        Transition(
            name="t1",
            inputs=[InputArc("input")],
            outputs=[OutputArc("sink")],
            action=lambda tokens: tokens,
        )
    )

    # Downstream transition wired to consume from a sink
    t2 = Transition(
        name="t2",
        inputs=[InputArc("sink")],
        outputs=[OutputArc("output")],
        action=lambda tokens: tokens,
    )
    net.add_transition(t2)

    # Verify that SinkPlace does not allow retrieval
    sink_place = net.places["sink"]
    assert sink_place.can_retrieve() is False
    with pytest.raises(ValueError, match="SinkPlace is terminal"):
        sink_place.retrieve(1)
    with pytest.raises(ValueError, match="SinkPlace is terminal"):
        sink_place.retrieve_all()
    with pytest.raises(ValueError, match="SinkPlace is terminal"):
        sink_place.retrieve_specific([Token()])
    with pytest.raises(ValueError, match="SinkPlace is terminal"):
        sink_place.peek(1)

    # If we deposit token to input, t1 should fire, placing token in sink.
    # But t2 should never fire because can_retrieve() is False.
    # So the net should naturally quiesce.
    net.deposit("input", Token())
    net.run()

    assert len(net.places["input"].tokens) == 0
    # Sink place token is kept (since keep_last=1) but t2 never fires.
    assert len(net.places["sink"].tokens) == 1
    assert len(net.places["output"].tokens) == 0
    assert net.is_quiescent() is True


def test_sink_place_infinite_capacity_no_backpressure():
    net = PetriNet()
    net.add_place(Place("input"))
    net.add_place(SinkPlace("sink", keep_last=0))

    net.add_transition(
        Transition(
            name="t1",
            inputs=[InputArc("input")],
            outputs=[OutputArc("sink", count=2)],
            action=lambda tokens: [Token(), Token()],
        )
    )

    # SinkPlace should always allow deposit
    sink_place = net.places["sink"]
    assert sink_place.can_deposit(100) is True

    # Check that transition enables and fires repeatedly even with keep_last=0
    for _ in range(5):
        net.deposit("input", Token())

    net.run()
    assert len(net.places["input"].tokens) == 0
    assert sink_place.stats()["absorbed"] == 10


def test_by_color_tallies_and_drain_stats():
    sink = SinkPlace("sink", keep_last=5)
    sink.deposit(Token(color="red"))
    sink.deposit(Token(color="red"))
    sink.deposit(Token(color="blue"))
    sink.deposit(Token(color=None))

    # Tally checks
    stats = sink.stats()
    assert stats["absorbed"] == 4
    assert stats["by_color"] == {"red": 2, "blue": 1, None: 1}
    assert stats["first_deposit_time"] is not None

    # Drain stats
    drained = sink.drain_stats()
    assert drained["absorbed"] == 4
    assert drained["by_color"] == {"red": 2, "blue": 1, None: 1}
    assert drained["kept"] == 4

    # Post-drain checks (running totals reset for deltas, but ring buffer kept)
    stats_after = sink.stats()
    assert stats_after["absorbed"] == 0
    assert stats_after["by_color"] == {}
    assert stats_after["kept"] == 4  # ring buffer is NOT cleared
    assert len(sink) == 4
    assert stats_after["first_deposit_time"] is None

    # Re-deposit sets first_deposit_time again
    sink.deposit(Token(color="red"))
    assert sink.stats()["first_deposit_time"] is not None


def test_color_set_enforcement():
    sink = SinkPlace("sink", keep_last=2, color_set={"green", "yellow"})
    sink.deposit(Token(color="green"))

    with pytest.raises(TypeError, match="cannot deposit token with color"):
        sink.deposit(Token(color="resource"))
    with pytest.raises(TypeError, match="cannot deposit token with color"):
        sink.deposit(Token(color=None))

    assert sink.stats()["absorbed"] == 1
    assert len(sink) == 1


def test_integration_and_callbacks():
    net = PetriNet()
    net.add_place(Place("input"))
    net.add_place(SinkPlace("sink", keep_last=2))

    net.add_transition(
        Transition(
            name="t",
            inputs=[InputArc("input")],
            outputs=[OutputArc("sink")],
            action=lambda tokens: tokens,
        )
    )

    deposited_events = []

    def on_dep(place_name, token):
        deposited_events.append((place_name, token))

    net.on_token_deposited = on_dep

    net.deposit("input", Token())
    net.deposit("input", Token())
    net.run()

    # on_token_deposited should fire for SinkPlace deposits too
    assert len(deposited_events) == 4
    sink_events = [e for e in deposited_events if e[0] == "sink"]
    assert len(sink_events) == 2

    assert net.places["sink"].stats()["absorbed"] == 2


def test_concurrency_no_lost_counts():
    sink = SinkPlace("sink", keep_last=0)

    num_threads = 8
    deposits_per_thread = 500

    barrier = threading.Barrier(num_threads + 1)

    def worker():
        barrier.wait()
        for i in range(deposits_per_thread):
            # Interleave different colors
            color = "red" if i % 2 == 0 else "blue"
            sink.deposit(Token(color=color))

    threads = [threading.Thread(target=worker) for _ in range(num_threads)]
    for t in threads:
        t.start()

    drained_stats = []
    stop_draining = False

    def drain_worker():
        barrier.wait()
        while not stop_draining:
            drained_stats.append(sink.drain_stats())
            time.sleep(0.001)

    dt = threading.Thread(target=drain_worker)
    dt.start()

    # Join workers
    for t in threads:
        t.join()

    # Stop draining and join drainer
    stop_draining = True
    dt.join()

    # Final drain to capture residual deposits
    drained_stats.append(sink.drain_stats())

    total_expected = num_threads * deposits_per_thread
    total_drained_absorbed = sum(s["absorbed"] for s in drained_stats)

    total_red = sum(s["by_color"].get("red", 0) for s in drained_stats)
    total_blue = sum(s["by_color"].get("blue", 0) for s in drained_stats)

    # The sum of every drain_stats delta plus final stats['absorbed']
    final_stats = sink.stats()
    total_summed = total_drained_absorbed + final_stats["absorbed"]

    assert total_summed == total_expected
    assert total_red + total_blue == total_expected
    assert total_red == total_expected // 2
    assert total_blue == total_expected // 2


def test_visualization_with_sink_place():
    net = PetriNet()
    sink = SinkPlace("sink", keep_last=1)
    net.add_place(sink)

    net.deposit("sink", Token(color="red"))
    net.deposit("sink", Token(color="blue"))

    # len() returns 1 (kept size)
    assert len(sink) == 1

    # snapshot includes absorbed count
    snap = net.snapshot()
    assert snap["places"]["sink"]["absorbed"] == 2
    assert len(snap["places"]["sink"]["tokens"]) == 1

    # to_dot includes absorbed count (2), not len (1)
    dot = net.to_dot()
    assert "sink\\n(2)" in dot
