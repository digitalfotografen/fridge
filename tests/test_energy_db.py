"""
Tests for the energy price / energy state SQLite store.

The :class:`fridge.energy_db.EnergyDatabase` stores raw 15-minute Tibber price
slots and the computed energy states. It is used both as long-term history and
as a cache so the controller works without network access.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from fridge.enums import EnergyState
from fridge.energy_db import (
    EnergyDatabase,
    EnergyStateSlot,
    HourlyStats,
    Measurement,
    PriceSlot,
)


BASE = datetime(2026, 6, 23, 14, 0, tzinfo=UTC)


def _slots(count: int, start: datetime = BASE, total: float = 1.0) -> list[PriceSlot]:
    return [
        PriceSlot(
            starts_at=start + timedelta(minutes=15 * i),
            total=total + i,
            energy=0.5,
            tax=0.25,
            currency="SEK",
            level="NORMAL",
        )
        for i in range(count)
    ]


@pytest.fixture
def db() -> EnergyDatabase:
    database = EnergyDatabase(":memory:")
    yield database
    database.close()


def test_upsert_and_get_prices_round_trip(db: EnergyDatabase):
    slots = _slots(4)
    db.upsert_prices(slots)

    result = db.get_prices(BASE, BASE + timedelta(hours=1))

    assert len(result) == 4
    assert result[0].starts_at == BASE
    assert result[0].total == 1.0
    assert result[0].currency == "SEK"
    assert result[0].level == "NORMAL"


def test_get_prices_range_is_half_open(db: EnergyDatabase):
    db.upsert_prices(_slots(4))

    # Window covering exactly the first two slots [BASE, BASE+30min)
    result = db.get_prices(BASE, BASE + timedelta(minutes=30))

    assert [s.starts_at for s in result] == [BASE, BASE + timedelta(minutes=15)]


def test_upsert_is_idempotent_and_updates(db: EnergyDatabase):
    db.upsert_prices(_slots(2))
    # Re-insert overlapping slot with a new total -> should update, not duplicate
    db.upsert_prices([PriceSlot(starts_at=BASE, total=99.0, currency="SEK")])

    result = db.get_prices(BASE, BASE + timedelta(hours=1))

    assert len(result) == 2
    assert result[0].total == 99.0


def test_naive_datetime_assumed_utc(db: EnergyDatabase):
    naive = datetime(2026, 6, 23, 14, 0)
    db.upsert_prices([PriceSlot(starts_at=naive, total=2.0, currency="SEK")])

    result = db.get_prices(BASE, BASE + timedelta(minutes=15))

    assert len(result) == 1
    assert result[0].starts_at == BASE


def test_get_price_at_returns_current_slot(db: EnergyDatabase):
    db.upsert_prices(_slots(4))

    slot = db.get_price_at(BASE + timedelta(minutes=20))

    assert slot is not None
    assert slot.starts_at == BASE + timedelta(minutes=15)


def test_get_price_at_returns_none_when_before_data(db: EnergyDatabase):
    db.upsert_prices(_slots(4))

    assert db.get_price_at(BASE - timedelta(minutes=1)) is None


def test_energy_states_round_trip(db: EnergyDatabase):
    states = [
        EnergyStateSlot(BASE, EnergyState.LOW_PRICE),
        EnergyStateSlot(BASE + timedelta(minutes=15), EnergyState.NORMAL_PRICE),
        EnergyStateSlot(BASE + timedelta(minutes=30), EnergyState.HIGH_PRICE),
    ]
    db.upsert_energy_states(states)

    result = db.get_energy_states(BASE, BASE + timedelta(hours=1))

    assert [s.energy_state for s in result] == [
        EnergyState.LOW_PRICE,
        EnergyState.NORMAL_PRICE,
        EnergyState.HIGH_PRICE,
    ]


def test_get_energy_state_at(db: EnergyDatabase):
    db.upsert_energy_states(
        [
            EnergyStateSlot(BASE, EnergyState.LOW_PRICE),
            EnergyStateSlot(BASE + timedelta(minutes=15), EnergyState.HIGH_PRICE),
        ]
    )

    assert db.get_energy_state_at(BASE + timedelta(minutes=10)) == EnergyState.LOW_PRICE
    assert db.get_energy_state_at(BASE + timedelta(minutes=20)) == EnergyState.HIGH_PRICE
    assert db.get_energy_state_at(BASE - timedelta(minutes=1)) is None


def test_upsert_energy_states_updates_existing(db: EnergyDatabase):
    db.upsert_energy_states([EnergyStateSlot(BASE, EnergyState.LOW_PRICE)])
    db.upsert_energy_states([EnergyStateSlot(BASE, EnergyState.HIGH_PRICE)])

    result = db.get_energy_states(BASE, BASE + timedelta(minutes=15))

    assert len(result) == 1
    assert result[0].energy_state == EnergyState.HIGH_PRICE


def _measurement(when: datetime, **overrides) -> Measurement:
    defaults = dict(
        recorded_at=when,
        temperature=5.0,
        humidity=60.0,
        mode="COOL",
        action_state="COOLING",
        energy_state="NORMAL_PRICE",
        target_temperature=5.0,
        target_humidity=55.0,
        price=1.23,
        price_currency="SEK",
    )
    defaults.update(overrides)
    return Measurement(**defaults)


def test_insert_and_get_measurements_round_trip(db: EnergyDatabase):
    db.insert_measurement(_measurement(BASE, temperature=4.2, humidity=58.0))

    result = db.get_measurements(BASE, BASE + timedelta(minutes=1))

    assert len(result) == 1
    row = result[0]
    assert row.recorded_at == BASE
    assert row.temperature == 4.2
    assert row.humidity == 58.0
    assert row.mode == "COOL"
    assert row.action_state == "COOLING"
    assert row.energy_state == "NORMAL_PRICE"
    assert row.price == 1.23
    assert row.price_currency == "SEK"


def test_measurements_range_ordered_and_half_open(db: EnergyDatabase):
    for i in range(4):
        db.insert_measurement(_measurement(BASE + timedelta(minutes=i)))

    result = db.get_measurements(BASE, BASE + timedelta(minutes=2))

    assert [m.recorded_at for m in result] == [BASE, BASE + timedelta(minutes=1)]


def test_insert_measurement_replaces_existing(db: EnergyDatabase):
    db.insert_measurement(_measurement(BASE, temperature=4.0))
    db.insert_measurement(_measurement(BASE, temperature=9.0))

    result = db.get_measurements(BASE, BASE + timedelta(minutes=1))

    assert len(result) == 1
    assert result[0].temperature == 9.0


def test_insert_measurement_allows_null_values(db: EnergyDatabase):
    db.insert_measurement(
        _measurement(
            BASE,
            temperature=None,
            humidity=None,
            target_temperature=None,
            target_humidity=None,
            price=None,
            price_currency=None,
        )
    )

    result = db.get_measurements(BASE, BASE + timedelta(minutes=1))

    assert len(result) == 1
    assert result[0].temperature is None
    assert result[0].price is None
    assert result[0].price_currency is None


def _hourly(hour_start: datetime, **overrides) -> HourlyStats:
    defaults = dict(
        hour_start=hour_start,
        mode="COOL",
        min_temperature=4.1,
        max_temperature=5.9,
        mean_temperature=5.02,
        min_humidity=58.2,
        max_humidity=63.7,
        mean_humidity=60.4,
        cooler_duty=62.5,
        compressor_duty=41.7,
        sample_count=720,
    )
    defaults.update(overrides)
    return HourlyStats(**defaults)


def test_insert_and_get_hourly_stats_round_trip(db: EnergyDatabase):
    db.insert_hourly_stats(_hourly(BASE))

    result = db.get_hourly_stats(BASE, BASE + timedelta(hours=1))

    assert len(result) == 1
    row = result[0]
    assert row.hour_start == BASE
    assert row.mode == "COOL"
    assert row.min_temperature == 4.1
    assert row.max_temperature == 5.9
    assert row.mean_temperature == 5.02
    assert row.cooler_duty == 62.5
    assert row.compressor_duty == 41.7
    assert row.sample_count == 720


def test_hourly_stats_range_ordered_and_half_open(db: EnergyDatabase):
    for i in range(4):
        db.insert_hourly_stats(_hourly(BASE + timedelta(hours=i), sample_count=i))

    result = db.get_hourly_stats(BASE, BASE + timedelta(hours=2))

    assert [r.hour_start for r in result] == [BASE, BASE + timedelta(hours=1)]
    assert [r.sample_count for r in result] == [0, 1]


def test_insert_hourly_stats_replaces_existing(db: EnergyDatabase):
    db.insert_hourly_stats(_hourly(BASE, cooler_duty=10.0))
    db.insert_hourly_stats(_hourly(BASE, cooler_duty=90.0))

    result = db.get_hourly_stats(BASE, BASE + timedelta(hours=1))

    assert len(result) == 1
    assert result[0].cooler_duty == 90.0


def test_insert_hourly_stats_allows_null_aggregates(db: EnergyDatabase):
    db.insert_hourly_stats(
        _hourly(
            BASE,
            min_temperature=None,
            max_temperature=None,
            mean_temperature=None,
            min_humidity=None,
            max_humidity=None,
            mean_humidity=None,
        )
    )

    result = db.get_hourly_stats(BASE, BASE + timedelta(hours=1))

    assert len(result) == 1
    assert result[0].min_temperature is None
    assert result[0].mean_humidity is None


def test_get_latest_hourly_stats(db: EnergyDatabase):
    assert db.get_latest_hourly_stats() is None

    db.insert_hourly_stats(_hourly(BASE, sample_count=1))
    db.insert_hourly_stats(_hourly(BASE + timedelta(hours=2), sample_count=3))
    db.insert_hourly_stats(_hourly(BASE + timedelta(hours=1), sample_count=2))

    latest = db.get_latest_hourly_stats()

    assert latest is not None
    assert latest.hour_start == BASE + timedelta(hours=2)
    assert latest.sample_count == 3


def test_prune_removes_old_rows(db: EnergyDatabase):
    db.upsert_prices(_slots(4))
    db.upsert_energy_states([EnergyStateSlot(BASE, EnergyState.LOW_PRICE)])
    db.insert_measurement(_measurement(BASE))
    db.insert_hourly_stats(_hourly(BASE))

    deleted = db.prune(BASE + timedelta(minutes=30))

    # Two price slots + one energy state + one measurement + one hourly stat
    assert deleted == 5
    remaining = db.get_prices(BASE, BASE + timedelta(hours=1))
    assert [s.starts_at for s in remaining] == [
        BASE + timedelta(minutes=30),
        BASE + timedelta(minutes=45),
    ]
    assert db.get_hourly_stats(BASE, BASE + timedelta(hours=2)) == []


def test_creates_file_and_persists(tmp_path):
    db_file = tmp_path / "nested" / "prices.db"
    database = EnergyDatabase(str(db_file))
    database.upsert_prices(_slots(1))
    database.close()

    assert db_file.exists()

    reopened = EnergyDatabase(str(db_file))
    try:
        result = reopened.get_prices(BASE, BASE + timedelta(minutes=15))
        assert len(result) == 1
    finally:
        reopened.close()


class _LockCheckingConnection:
    """Connection proxy that records calls made without the database lock held."""

    def __init__(self, database: EnergyDatabase) -> None:
        self._database = database
        self._connection = database._connection
        self.unlocked_calls: list[str] = []

    def __getattr__(self, name: str):
        attribute = getattr(self._connection, name)
        if not callable(attribute):
            return attribute

        def checked(*args, **kwargs):
            if not self._database._lock.locked():
                self.unlocked_calls.append(name)
            return attribute(*args, **kwargs)

        return checked


def test_every_public_method_holds_lock_while_using_connection(db: EnergyDatabase):
    proxy = _LockCheckingConnection(db)
    db._connection = proxy
    end = BASE + timedelta(hours=2)

    db.upsert_prices(_slots(2))
    db.get_prices(BASE, end)
    db.get_price_at(BASE)
    db.upsert_energy_states([EnergyStateSlot(BASE, EnergyState.NORMAL_PRICE)])
    db.get_energy_states(BASE, end)
    db.get_energy_state_at(BASE)
    db.insert_measurement(_measurement(BASE))
    db.get_measurements(BASE, end)
    db.insert_hourly_stats(_hourly(BASE))
    db.get_hourly_stats(BASE, end)
    db.get_latest_hourly_stats()
    db.prune(BASE)
    db._connection = proxy._connection

    assert proxy.unlocked_calls == []


@pytest.mark.timeout(60)
def test_concurrent_writes_and_reads_do_not_raise(tmp_path):
    """Tibber writes and logger reads on one shared connection must not collide."""
    database = EnergyDatabase(str(tmp_path / "fridge.db"))
    errors: list[BaseException] = []
    rounds = 200

    def write_prices() -> None:
        try:
            for _ in range(rounds):
                database.upsert_prices(_slots(192))
        except BaseException as exc:  # noqa: BLE001 - collect anything for the assert
            errors.append(exc)

    def read_prices() -> None:
        try:
            for i in range(rounds * 20):
                database.get_price_at(BASE + timedelta(minutes=i % 2880))
                database.insert_measurement(_measurement(BASE + timedelta(seconds=i)))
        except BaseException as exc:  # noqa: BLE001 - collect anything for the assert
            errors.append(exc)

    threads = [threading.Thread(target=write_prices), threading.Thread(target=read_prices)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        database.close()

    assert errors == []
