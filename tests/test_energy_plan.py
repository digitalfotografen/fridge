"""
Tests for the energy-price classification algorithm.

:func:`fridge.energy_plan.classify_prices` turns a list of 15-minute price
slots into a list of :class:`~fridge.enums.EnergyState` slots using percentile
bands, a pre-cooling look-ahead pass and sanity caps. :class:`EnergyPlanner`
reads the prices from the :class:`~fridge.energy_db.EnergyDatabase`, classifies
them and caches the result back in the database.
"""

from __future__ import annotations

from configparser import ConfigParser
from datetime import UTC, datetime, timedelta

import pytest

from fridge.energy_db import EnergyDatabase, PriceSlot
from fridge.enums import EnergyState
from fridge.energy_plan import EnergyPlanner, classify_prices


BASE = datetime(2026, 6, 23, 0, 0, tzinfo=UTC)

LOW = EnergyState.LOW_PRICE
NORMAL = EnergyState.NORMAL_PRICE
HIGH = EnergyState.HIGH_PRICE


def _prices(totals: list[float], start: datetime = BASE) -> list[PriceSlot]:
    return [
        PriceSlot(
            starts_at=start + timedelta(minutes=15 * i),
            total=total,
            currency="SEK",
        )
        for i, total in enumerate(totals)
    ]


def _states(slots) -> list[EnergyState]:
    return [s.energy_state for s in slots]


# ----------------------------------------------------------------------
# classify_prices: percentile bands
# ----------------------------------------------------------------------
def test_empty_prices_returns_empty():
    assert classify_prices([]) == []


def test_flat_prices_are_all_normal():
    # All identical -> low and high thresholds collapse, nothing is LOW/HIGH.
    slots = classify_prices(_prices([1.0] * 8))
    assert _states(slots) == [NORMAL] * 8


def test_three_bands_are_assigned():
    # 9 increasing prices: 33rd/67th percentiles split into 3 equal bands.
    totals = [1, 2, 3, 4, 5, 6, 7, 8, 9]
    slots = classify_prices(
        _prices(totals),
        precool_lookahead_slots=0,
        max_consecutive_low_slots=99,
    )
    states = _states(slots)
    # Lowest prices LOW, highest HIGH, middle NORMAL; all three present.
    assert states[0] == LOW
    assert states[-1] == HIGH
    assert LOW in states and NORMAL in states and HIGH in states


def test_starts_at_preserved_and_ordered():
    slots = classify_prices(_prices([1, 2, 3, 4]))
    assert [s.starts_at for s in slots] == [BASE + timedelta(minutes=15 * i) for i in range(4)]


# ----------------------------------------------------------------------
# classify_prices: pre-cooling look-ahead
# ----------------------------------------------------------------------
def test_precooling_relabels_normal_before_high_to_low():
    # Construct: clearly NORMAL slots followed by a HIGH spike.
    # Lows at the front anchor the LOW band, mid values NORMAL, big spike HIGH.
    totals = [1, 1, 5, 5, 5, 100]
    slots = classify_prices(
        _prices(totals),
        precool_lookahead_slots=3,
        max_consecutive_low_slots=99,
    )
    states = _states(slots)
    assert states[-1] == HIGH
    # The NORMAL slots within 3 slots before the HIGH should become LOW.
    assert states[2] == LOW
    assert states[3] == LOW
    assert states[4] == LOW


def test_precooling_respects_lookahead_bound():
    totals = [1, 1, 5, 5, 5, 5, 5, 100]
    slots = classify_prices(
        _prices(totals),
        precool_lookahead_slots=2,
        max_consecutive_low_slots=99,
    )
    states = _states(slots)
    # Only the 2 NORMAL slots immediately before the HIGH are relabelled.
    assert states[-1] == HIGH
    assert states[6] == LOW
    assert states[5] == LOW
    # Slots further back stay NORMAL.
    assert states[4] == NORMAL
    assert states[3] == NORMAL


def test_precooling_does_not_create_low_inside_high_block():
    # Two HIGH blocks separated by NORMALs; look-back from the second HIGH must
    # stop at the first HIGH block and never relabel a HIGH slot to LOW. Enough
    # NORMAL slots are present so the 67th percentile sits below the spikes.
    #  idx: 0   1    2    3   4   5    6   7   8   9
    totals = [1, 1, 100, 100, 10, 10, 100, 10, 10, 10]
    slots = classify_prices(
        _prices(totals),
        precool_lookahead_slots=5,
        max_consecutive_low_slots=99,
    )
    states = _states(slots)
    # HIGH slots remain HIGH.
    assert states[2] == HIGH
    assert states[3] == HIGH
    assert states[6] == HIGH
    # No LOW assigned inside the HIGH block at index 2-3.
    assert states[2] != LOW
    assert states[3] != LOW
    # The NORMAL slots before the second HIGH are pre-cooled, but the look-back
    # stops at the first HIGH block (index 3) and does not reach further.
    assert states[4] == LOW
    assert states[5] == LOW


# ----------------------------------------------------------------------
# classify_prices: consecutive-LOW cap
# ----------------------------------------------------------------------
def test_consecutive_low_is_capped():
    # Six naturally-low prices followed by many higher ones so the 33rd
    # percentile sits above the low prices and labels exactly six LOW slots.
    totals = [1.0] * 6 + [10.0] * 12
    slots = classify_prices(
        _prices(totals),
        precool_lookahead_slots=0,
        max_consecutive_low_slots=4,
    )
    states = _states(slots)
    low_run = sum(1 for s in states if s == LOW)
    assert low_run == 4
    # Earliest excess LOW slots are reverted to NORMAL (cool closest to demand).
    assert states[0] == NORMAL
    assert states[1] == NORMAL
    assert states[2] == LOW
    assert states[5] == LOW


# ----------------------------------------------------------------------
# EnergyPlanner
# ----------------------------------------------------------------------
def _config(**overrides: str) -> ConfigParser:
    cfg = ConfigParser()
    cfg["Tibber"] = {
        "precool_lookahead_slots": "3",
        "max_consecutive_low_slots": "4",
    }
    cfg["Tibber"].update(overrides)
    return cfg


@pytest.fixture
def db() -> EnergyDatabase:
    database = EnergyDatabase(":memory:")
    yield database
    database.close()


def test_planner_reads_prices_and_caches_states(db: EnergyDatabase):
    db.upsert_prices(_prices([1, 1, 5, 5, 100, 100]))
    planner = EnergyPlanner(_config(), database=db)

    result = planner.recompute(now=BASE)

    assert result, "planner should produce energy states"
    # States are persisted in the cache and readable back.
    cached = db.get_energy_states(BASE, BASE + timedelta(hours=3))
    assert _states(cached) == _states(result)


def test_planner_only_classifies_current_and_future_slots(db: EnergyDatabase):
    db.upsert_prices(_prices([1, 2, 3, 4, 5, 6]))
    planner = EnergyPlanner(_config(), database=db)

    # "Now" is at the third slot -> only slots from index 2 onward classified.
    now = BASE + timedelta(minutes=30)
    result = planner.recompute(now=now)

    assert result[0].starts_at == BASE + timedelta(minutes=30)


def test_planner_handles_no_prices(db: EnergyDatabase):
    planner = EnergyPlanner(_config(), database=db)
    assert planner.recompute(now=BASE) == []
