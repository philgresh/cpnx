"""Smoke tests for the ☕ Concurrency Cafe's opt-in stations (``benchmarks/cafe/stations/``).

Each station exists to exercise one engine cost path that the base topology never touches,
and several of them depend on a property that is **invisible in ordinary review**: whether a
guard, key, or filter *certifies* as closed-world under ``cpnx.certification``.

That makes these tests unusually load-bearing. `specials_board` and `eighty_six` are only
meaningful because their callables read module-level mutable state and therefore fail
certification — freezing that state into a constant would leave both stations importing,
building, running, and reporting numbers, just for a different code path than the one they
claim to measure. A benchmark that silently measures the wrong thing is worse than one that
crashes, so the certification matrix is asserted here explicitly rather than trusted to a
comment.

These deliberately do NOT assert conservation — see ``tests/test_concurrency_cafe.py`` for
why the cafe is not a conservation-checked net.
"""

import sys
import time
import warnings
from pathlib import Path

import pytest

from cpnx import Token
from cpnx.certification import is_inline_safe

# The cafe lives under benchmarks/ (not a package, not on the pytest pythonpath), so add
# that directory to sys.path the same way the fixture itself shims in ``src``.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))

from cafe import build_cafe  # noqa: E402
from cafe.stations import batch_triage, cupping, decaf, eighty_six, knock_box, specials_board  # noqa: E402

#: Every station flag on ``build_cafe``, with the place and transition it must contribute.
STATIONS = [
    ("cold_brew", "P_Cold_Brew_Steeping", "T_Pull_Cold_Brew"),
    ("batch_triage", "P_Batch_Triage_Queue", "T_Batch_Triage_Serve"),
    ("decaf", "P_Decaf_Line", "T_Decaf_Pull"),
    ("knock_box", "P_Knock_Box", "T_Empty_Knock_Box"),
    ("specials_board", "P_Specials_Queue", "T_Specials_Serve"),
    ("eighty_six", "P_Eighty_Six_Queue", "T_Eighty_Six_Serve"),
    ("cupping", "P_Sample_Queue", "T_Cupping_Flight"),
    ("pastry_case", "P_Food_Order", "T_Pastry_Case"),
]


class TestStationsAreOptIn:
    def test_base_cafe_has_no_station_places(self):
        """A bare ``build_cafe()`` must be exactly the base topology — every station is
        structure-preserving when off, which is what keeps published benchmark numbers
        comparable across the addition of new stations."""
        net = build_cafe()
        for _flag, place, transition in STATIONS:
            assert place not in net.places, f"{place} leaked into the default topology"
            assert transition not in net.transitions, f"{transition} leaked into the default topology"

    @pytest.mark.parametrize(("flag", "place", "transition"), STATIONS)
    def test_flag_adds_exactly_its_station(self, flag, place, transition):
        net = build_cafe(**{flag: True})
        net.validate()  # raises on any structural problem
        assert place in net.places
        assert transition in net.transitions

    def test_all_stations_at_once_still_validates(self):
        """Stations must compose — nothing may depend on being the only one enabled."""
        net = build_cafe(**{flag: True for flag, _, _ in STATIONS}, cold_brew_key=True)
        net.validate()
        for _flag, place, transition in STATIONS:
            assert place in net.places
            assert transition in net.transitions

    def test_cold_brew_key_requires_cold_brew(self):
        with pytest.raises(ValueError, match="requires cold_brew=True"):
            build_cafe(cold_brew_key=True)


class TestCertificationMatrix:
    """The property each station's *identity* rests on. See this module's docstring."""

    def test_batch_triage_key_certifies(self):
        """The indexed fast path: a certified key is what the persistent (key, seq)
        min-heap on the place can be built from."""
        assert is_inline_safe(batch_triage.batch_triage_key) is True

    def test_specials_board_key_does_not_certify(self):
        """Its whole reason to exist. If this starts passing, someone froze
        ``_SPECIALS_BOARD`` and the uncertified-key regime is no longer being measured."""
        assert is_inline_safe(specials_board.specials_board_key) is False

    def test_specials_board_orders_identically_to_batch_triage(self):
        """The two stations are an A/B pair, so the ordering must match exactly — otherwise
        the comparison measures a different sort, not a different dispatch path."""
        token = Token(payload={"dairy_free": True, "weight_g": 18.0})
        assert specials_board.specials_board_key(token) == batch_triage.batch_triage_key(token)

    def test_eighty_six_pairs_a_certified_key_with_an_uncertified_filter(self):
        """The asymmetry this station demonstrates: one uncertified filter disqualifies the
        arc from indexing no matter how well the key certifies."""
        arc = eighty_six.transitions()[0].inputs[0]
        assert is_inline_safe(arc.key) is True
        assert is_inline_safe(arc.filter) is False

    def test_decaf_filter_certifies_but_has_no_key(self):
        """The cliff: certification is not enough. A filter-only arc never gets an index,
        because ``_ensure_key_index`` bails as soon as ``arc.key is None``."""
        arc = decaf.transitions()[0].inputs[0]
        assert is_inline_safe(arc.filter) is True
        assert arc.key is None

    def test_knock_box_drains_without_tripping_the_selection_warning(self):
        """``consume_all=True`` silently ignores ``key``/``filter`` and warns when both are
        set. This station means the drain, so it must set neither and stay silent."""
        with warnings.catch_warnings():
            warnings.simplefilter("error", UserWarning)
            arc = knock_box.transitions()[0].inputs[0]
        assert arc.consume_all is True
        assert arc.key is None and arc.filter is None

    def test_cupping_pulls_a_multi_token_flight(self):
        arc = cupping.transitions()[0].inputs[0]
        assert arc.count > 1, "a flight of one is not a flight — the count>1 path is untested"
        assert is_inline_safe(arc.key) is True


class TestStationsFire:
    """A station that never fires is worthless as a benchmark fixture, so each one that can
    be driven standalone is driven here."""

    def test_decaf_serves_only_decaf_tickets(self):
        with build_cafe(decaf=True, seed=1) as net:
            for i in range(6):
                net.deposit("P_Decaf_Line", Token(payload={"decaf": i % 2 == 0}))
            net.run(deadline=time.monotonic() + 2.0)

            # Three decaf tickets are eligible; the three regular ones must stay put,
            # which is the filter doing its job rather than the queue merely draining.
            remaining = net.marking["P_Decaf_Line"]
            assert len(remaining) == 3
            assert all(not t.payload["decaf"] for t in remaining)

    def test_cold_brew_respects_steeping_time(self):
        """A batch is not pourable until its own ``available_at`` has elapsed — the engine
        filters to matured tokens before the arc ever sees them."""
        with build_cafe(cold_brew=True, seed=1) as net:
            net.deposit(
                "P_Cold_Brew_Steeping",
                Token(color="cold_brew", payload={"cup_oz": 16}, available_at=time.monotonic() + 30.0),
            )
            net.run(deadline=time.monotonic() + 1.0)
            assert len(net.marking["P_Cold_Brew_Steeping"]) == 1, "an unsteeped batch was poured"

    def test_keyed_cold_brew_builds_and_still_defers(self):
        """The timed×key residual arm must behave identically to its control — the index
        declining is a pure cost difference, never a semantic one."""
        with build_cafe(cold_brew=True, cold_brew_key=True, seed=1) as net:
            net.deposit(
                "P_Cold_Brew_Steeping",
                Token(color="cold_brew", payload={"cup_oz": 16}, available_at=time.monotonic() + 30.0),
            )
            net.run(deadline=time.monotonic() + 1.0)
            assert len(net.marking["P_Cold_Brew_Steeping"]) == 1
