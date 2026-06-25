"""
Tests for :class:`fridge.measurement_logger.MeasurementLogger`.

The logger snapshots the thermostat state and the current slot price and writes
a row to the shared :class:`~fridge.energy_db.EnergyDatabase` ``measurements``
table on each tick.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from fridge.energy_db import EnergyDatabase, PriceSlot
from fridge.enums import ActionState, EnergyState, ModeState
from fridge.measurement_logger import MeasurementLogger
from fridge.thermostat import ThermostatThresholds

BASE = datetime(2026, 6, 23, 14, 0, tzinfo=UTC)


class FakeThermostat:
    """Minimal stand-in exposing the attributes the logger reads."""

    def __init__(self, temperature=5.0, humidity=60.0):
        self.temperature = temperature
        self.humidity = humidity
        self.thresholds = ThermostatThresholds(
            target_temperature=5.0,
            target_humidity=55.0,
            heater_on_threshold=None,
            heater_off_threshold=None,
            cooler_on_threshold=None,
            cooler_off_threshold=None,
            fan_on_threshold=None,
            fan_off_threshold=None,
        )

    def get_mode(self):
        return ModeState.COOL

    def get_action_state(self):
        return ActionState.COOLING

    def get_energy_state(self):
        return EnergyState.NORMAL_PRICE


@pytest.fixture
def db() -> EnergyDatabase:
    database = EnergyDatabase(":memory:")
    yield database
    database.close()


def test_tick_writes_measurement(db: EnergyDatabase):
    db.upsert_prices([PriceSlot(starts_at=BASE, total=1.23, currency="SEK")])
    logger = MeasurementLogger(FakeThermostat(temperature=4.2, humidity=58.0), db)

    measurement = logger.tick(now=BASE + timedelta(minutes=5))

    assert measurement.temperature == 4.2
    assert measurement.mode == "COOL"
    assert measurement.action_state == "COOLING"
    assert measurement.energy_state == "NORMAL_PRICE"
    assert measurement.target_temperature == 5.0
    assert measurement.target_humidity == 55.0
    assert measurement.price == 1.23
    assert measurement.price_currency == "SEK"

    stored = db.get_measurements(BASE, BASE + timedelta(hours=1))
    assert len(stored) == 1
    assert stored[0].temperature == 4.2


def test_tick_without_price_stores_none(db: EnergyDatabase):
    logger = MeasurementLogger(FakeThermostat(), db)

    measurement = logger.tick(now=BASE)

    assert measurement.price is None
    assert measurement.price_currency is None


def test_tick_replaces_nan_with_none(db: EnergyDatabase):
    logger = MeasurementLogger(FakeThermostat(temperature=math.nan, humidity=math.nan), db)

    measurement = logger.tick(now=BASE)

    assert measurement.temperature is None
    assert measurement.humidity is None


def test_start_and_stop_are_idempotent(db: EnergyDatabase):
    logger = MeasurementLogger(FakeThermostat(), db, interval_seconds=3600)

    logger.start()
    logger.start()  # second start is a no-op
    logger.stop()
    logger.stop()  # second stop is a no-op

    # The initial tick writes one row.
    stored = db.get_measurements(datetime(1970, 1, 1, tzinfo=UTC), datetime(2999, 1, 1, tzinfo=UTC))
    assert len(stored) == 1
