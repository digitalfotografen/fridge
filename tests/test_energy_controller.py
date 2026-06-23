"""
Tests for the energy controller that drives the thermostat from the planner.

The :class:`~fridge.energy_controller.EnergyController` reads the current
15-minute :class:`~fridge.enums.EnergyState` from the cache (the planner's
output) and applies it to the thermostat. When the cache has no entry for the
current slot it asks the planner to recompute; if there is still no plan
(e.g. no prices fetched yet), it falls back to ``NORMAL_PRICE`` so the
controller is safe to run without network access.
"""

from __future__ import annotations

from configparser import ConfigParser
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from fridge.energy_controller import EnergyController
from fridge.energy_db import EnergyDatabase, EnergyStateSlot, PriceSlot
from fridge.energy_plan import EnergyPlanner
from fridge.enums import EnergyState


NOW = datetime(2026, 6, 23, 14, 7, tzinfo=UTC)
SLOT = datetime(2026, 6, 23, 14, 0, tzinfo=UTC)


def _config() -> ConfigParser:
    cfg = ConfigParser()
    cfg["Tibber"] = {
        "precool_lookahead_slots": "0",
        "max_consecutive_low_slots": "99",
    }
    return cfg


@pytest.fixture
def db() -> EnergyDatabase:
    database = EnergyDatabase(":memory:")
    yield database
    database.close()


@pytest.fixture
def thermostat() -> MagicMock:
    mock = MagicMock(name="Thermostat")
    mock.get_energy_state.return_value = EnergyState.NORMAL_PRICE
    return mock


def _planner(cfg: ConfigParser, db: EnergyDatabase) -> EnergyPlanner:
    return EnergyPlanner(cfg, database=db)


# ----------------------------------------------------------------------
# tick(): applying the cached current slot
# ----------------------------------------------------------------------
def test_tick_applies_cached_current_state(db, thermostat):
    db.upsert_energy_states([EnergyStateSlot(SLOT, EnergyState.HIGH_PRICE)])
    controller = EnergyController(thermostat=thermostat, planner=_planner(_config(), db))

    controller.tick(now=NOW)

    thermostat.set_energy_state.assert_called_once_with(EnergyState.HIGH_PRICE)


def test_tick_recomputes_when_cache_is_empty_but_prices_exist(db, thermostat):
    # No cached state yet, but raw prices are available for the current slot.
    db.upsert_prices(
        [PriceSlot(starts_at=SLOT + timedelta(minutes=15 * i), total=1.0 + i) for i in range(6)]
    )
    controller = EnergyController(thermostat=thermostat, planner=_planner(_config(), db))

    controller.tick(now=NOW)

    # Planner ran and cached states; thermostat received the computed state.
    cached = db.get_energy_state_at(NOW)
    assert cached is not None
    thermostat.set_energy_state.assert_called_once_with(cached)


def test_tick_falls_back_to_normal_when_no_data(db, thermostat):
    controller = EnergyController(thermostat=thermostat, planner=_planner(_config(), db))

    controller.tick(now=NOW)

    thermostat.set_energy_state.assert_called_once_with(EnergyState.NORMAL_PRICE)


def test_tick_does_not_recompute_when_current_slot_cached(db, thermostat):
    db.upsert_energy_states([EnergyStateSlot(SLOT, EnergyState.LOW_PRICE)])
    planner = _planner(_config(), db)
    planner.recompute = MagicMock(wraps=planner.recompute)
    controller = EnergyController(thermostat=thermostat, planner=planner)

    controller.tick(now=NOW)

    planner.recompute.assert_not_called()
    thermostat.set_energy_state.assert_called_once_with(EnergyState.LOW_PRICE)


# ----------------------------------------------------------------------
# Slot boundary scheduling
# ----------------------------------------------------------------------
def test_seconds_until_next_slot_aligns_to_15_minutes(db, thermostat):
    controller = EnergyController(thermostat=thermostat, planner=_planner(_config(), db))

    # 14:07 -> next slot at 14:15 -> 8 minutes = 480 seconds.
    assert controller._seconds_until_next_slot(now=NOW) == pytest.approx(8 * 60)
    # Exactly on a boundary -> wait a full slot.
    assert controller._seconds_until_next_slot(now=SLOT) == pytest.approx(15 * 60)


# ----------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------
def test_start_and_stop_are_idempotent(db, thermostat):
    controller = EnergyController(thermostat=thermostat, planner=_planner(_config(), db))

    controller.start()
    controller.start()  # second call is a no-op
    assert controller._thread is not None
    controller.stop()
    controller.stop()
    assert controller._thread is None
