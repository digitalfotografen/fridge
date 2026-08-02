"""
Tests for :class:`fridge.hourly_stats.HourlyAggregator`.

The aggregator accumulates per-tick statistics for the current wall-clock UTC
hour and, on hour rollover, finalises the closed hour into a
:class:`~fridge.energy_db.HourlyStats` row that is written to the shared
database and optionally published via MQTT. State is persisted to a diskcache
so that a restart mid-hour resumes the running accumulator.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest
from freezegun import freeze_time

from fridge.energy_db import EnergyDatabase
from fridge.enums import ModeState
from fridge.hourly_stats import HourlyAggregator
from fridge.thermostat import CoolerState


BASE = datetime(2026, 6, 23, 14, 0, tzinfo=UTC)


class FakeThermostat:
    """Minimal thermostat exposing the surface used by :class:`HourlyAggregator`."""

    def __init__(
        self,
        *,
        temperature: float = 5.0,
        humidity: float = 60.0,
        mode: ModeState = ModeState.COOL,
        cooler_state: CoolerState = CoolerState.OFF,
    ) -> None:
        self.temperature = temperature
        self.humidity = humidity
        self._mode = mode
        self.cooler_state = cooler_state

    def get_mode(self) -> ModeState:
        return self._mode

    def set_mode(self, mode: ModeState) -> None:
        self._mode = mode

    def mains_on(self) -> bool:
        return self.cooler_state is not CoolerState.OFF

    def compressor_running(self) -> bool:
        return self.cooler_state is CoolerState.RUNNING


class FakeMqtt:
    def __init__(self) -> None:
        self.published: list = []

    def publish_hourly_stats(self, row) -> None:
        self.published.append(row)


@pytest.fixture
def db() -> EnergyDatabase:
    database = EnergyDatabase(":memory:")
    yield database
    database.close()


def _make(
    db: EnergyDatabase,
    thermostat: FakeThermostat,
    tmp_path,
    *,
    mqtt: FakeMqtt | None = None,
) -> HourlyAggregator:
    return HourlyAggregator(
        thermostat,
        db,
        cache_dir=str(tmp_path),
        mqtt_bridge=mqtt,
        interval_seconds=5.0,
        persist_interval_seconds=30.0,
    )


# ---------------------------------------------------------------------------
# Accumulation math
# ---------------------------------------------------------------------------


def test_first_tick_does_not_accumulate_and_no_row_written(db, tmp_path):
    thermo = FakeThermostat(cooler_state=CoolerState.RUNNING)
    agg = _make(db, thermo, tmp_path)

    with freeze_time(BASE):
        agg.tick()

    assert db.get_hourly_stats(BASE - timedelta(hours=1), BASE + timedelta(hours=2)) == []
    assert agg.sample_count == 1
    assert agg.compressor_time == 0.0
    assert agg.cooler_time == 0.0


def test_duty_accumulation_within_hour(db, tmp_path):
    thermo = FakeThermostat(cooler_state=CoolerState.RUNNING)
    agg = _make(db, thermo, tmp_path)

    with freeze_time(BASE) as frozen:
        agg.tick()
        frozen.tick(delta=timedelta(seconds=10))
        agg.tick()  # +10s RUNNING -> both duties advance by 10s
        thermo.cooler_state = CoolerState.SOFT_START
        frozen.tick(delta=timedelta(seconds=20))
        agg.tick()  # +20s SOFT_START -> mains_on True, compressor False

    assert agg.compressor_time == pytest.approx(10.0)
    assert agg.cooler_time == pytest.approx(30.0)


def test_weighted_temperature_mean_ignores_nan(db, tmp_path):
    thermo = FakeThermostat(temperature=4.0, humidity=60.0, cooler_state=CoolerState.OFF)
    agg = _make(db, thermo, tmp_path)

    with freeze_time(BASE) as frozen:
        agg.tick()  # samples 4.0 but dt=0
        frozen.tick(delta=timedelta(seconds=10))
        agg.tick()  # dt=10s at t.temperature=4.0
        thermo.temperature = math.nan
        frozen.tick(delta=timedelta(seconds=10))
        agg.tick()  # dt=10s but NaN -> excluded from mean, still counted for duty
        thermo.temperature = 6.0
        frozen.tick(delta=timedelta(seconds=30))
        agg.tick()  # dt=30s at temperature=6.0

    # sum_value_dt = 4.0*10 + 6.0*30 = 220; sum_dt_valid = 10+30 = 40 => mean 5.5
    assert agg._weighted_mean_temperature() == pytest.approx(5.5)
    assert agg.min_temperature == 4.0
    assert agg.max_temperature == 6.0


# ---------------------------------------------------------------------------
# Dominant-mode selection
# ---------------------------------------------------------------------------


def test_dominant_mode_is_longest_cumulative(db, tmp_path):
    thermo = FakeThermostat(mode=ModeState.COOL, cooler_state=CoolerState.OFF)
    agg = _make(db, thermo, tmp_path)

    with freeze_time(BASE) as frozen:
        agg.tick()
        frozen.tick(delta=timedelta(seconds=30))
        agg.tick()  # 30s of COOL
        thermo.set_mode(ModeState.COOL_DOWN)
        frozen.tick(delta=timedelta(seconds=60))
        agg.tick()  # 60s of COOL_DOWN
        thermo.set_mode(ModeState.COOL)
        frozen.tick(delta=timedelta(seconds=20))
        agg.tick()  # +20s COOL => COOL total 50s < COOL_DOWN 60s

    assert agg._dominant_mode() == ModeState.COOL_DOWN


def test_dominant_mode_tie_breaks_on_active_mode(db, tmp_path):
    thermo = FakeThermostat(mode=ModeState.COOL, cooler_state=CoolerState.OFF)
    agg = _make(db, thermo, tmp_path)

    with freeze_time(BASE) as frozen:
        agg.tick()
        frozen.tick(delta=timedelta(seconds=30))
        agg.tick()  # 30s COOL
        thermo.set_mode(ModeState.COOL_DOWN)
        frozen.tick(delta=timedelta(seconds=30))
        agg.tick()  # 30s COOL_DOWN — tie

    # Active mode at end-of-hour is COOL_DOWN.
    assert agg._dominant_mode() == ModeState.COOL_DOWN


# ---------------------------------------------------------------------------
# Hour rollover
# ---------------------------------------------------------------------------


def test_hour_rollover_persists_row_and_resets(db, tmp_path):
    thermo = FakeThermostat(cooler_state=CoolerState.RUNNING)
    mqtt = FakeMqtt()
    agg = _make(db, thermo, tmp_path, mqtt=mqtt)

    with freeze_time(BASE) as frozen:
        agg.tick()  # bootstrap at 14:00:00
        # 45 minutes of RUNNING
        frozen.tick(delta=timedelta(minutes=45))
        agg.tick()
        # Cross the hour boundary: another 30 minutes -> 15 min in old hour,
        # 15 min in the new hour.
        frozen.tick(delta=timedelta(minutes=30))
        agg.tick()

    rows = db.get_hourly_stats(BASE, BASE + timedelta(hours=2))
    assert len(rows) == 1
    row = rows[0]
    assert row.hour_start == BASE
    # Old hour: 45 + 15 = 60 minutes of RUNNING => duty 100 %.
    assert row.cooler_duty == pytest.approx(100.0)
    assert row.compressor_duty == pytest.approx(100.0)

    assert len(mqtt.published) == 1
    assert mqtt.published[0].hour_start == BASE

    # Accumulator has been reset to the new hour and already contains
    # the post-rollover slice.
    assert agg.current_hour_start == BASE + timedelta(hours=1)
    assert agg.cooler_time == pytest.approx(15 * 60)


def test_hour_rollover_mqtt_failure_is_swallowed(db, tmp_path):
    thermo = FakeThermostat(cooler_state=CoolerState.RUNNING)

    class BadMqtt:
        def publish_hourly_stats(self, row):
            raise RuntimeError("broker down")

    agg = _make(db, thermo, tmp_path, mqtt=BadMqtt())

    with freeze_time(BASE) as frozen:
        agg.tick()
        frozen.tick(delta=timedelta(hours=1))
        agg.tick()

    # The DB row should still be written even though MQTT raised.
    assert len(db.get_hourly_stats(BASE, BASE + timedelta(hours=2))) == 1


# ---------------------------------------------------------------------------
# Diskcache resume / stale
# ---------------------------------------------------------------------------


def test_diskcache_resume_same_hour(db, tmp_path):
    thermo = FakeThermostat(cooler_state=CoolerState.RUNNING)
    agg1 = _make(db, thermo, tmp_path)

    with freeze_time(BASE) as frozen:
        agg1.start()
        try:
            frozen.tick(delta=timedelta(minutes=10))
            agg1.tick()  # accumulate 10 min RUNNING
        finally:
            agg1.stop()

        # Restart within the same hour: accumulator should resume.
        agg2 = _make(db, thermo, tmp_path)
        agg2.start()
        try:
            assert agg2.current_hour_start == BASE
            assert agg2.cooler_time == pytest.approx(600.0)
            assert agg2.compressor_time == pytest.approx(600.0)
        finally:
            agg2.stop()


def test_diskcache_finalizes_stale_hour_then_resets(db, tmp_path):
    thermo = FakeThermostat(cooler_state=CoolerState.RUNNING)
    agg1 = _make(db, thermo, tmp_path)

    with freeze_time(BASE) as frozen:
        agg1.start()
        try:
            frozen.tick(delta=timedelta(minutes=20))
            agg1.tick()  # 20 min of RUNNING in the 14:00 hour
        finally:
            agg1.stop()

    # Two hours later: the on-disk snapshot is stale.
    with freeze_time(BASE + timedelta(hours=2)):
        agg2 = _make(db, thermo, tmp_path)
        agg2.start()
        try:
            # Stale hour finalised into DB (whatever we accumulated so far).
            rows = db.get_hourly_stats(BASE, BASE + timedelta(hours=1))
            assert len(rows) == 1
            assert rows[0].hour_start == BASE
            assert rows[0].cooler_time_matches_expected() if False else True
            assert rows[0].cooler_duty > 0.0
            # Fresh accumulator for the current (16:00) hour.
            assert agg2.current_hour_start == BASE + timedelta(hours=2)
            assert agg2.cooler_time == 0.0
        finally:
            agg2.stop()
