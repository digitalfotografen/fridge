"""
Tests for the energy price / energy state SQLite store.

The :class:`fridge.energy_db.EnergyDatabase` stores raw 15-minute Tibber price
slots and the computed energy states. It is used both as long-term history and
as a cache so the controller works without network access.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fridge.enums import EnergyState
from fridge.energy_db import EnergyDatabase, EnergyStateSlot, Measurement, PriceSlot


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


def test_prune_removes_old_rows(db: EnergyDatabase):
    db.upsert_prices(_slots(4))
    db.upsert_energy_states([EnergyStateSlot(BASE, EnergyState.LOW_PRICE)])
    db.insert_measurement(_measurement(BASE))

    deleted = db.prune(BASE + timedelta(minutes=30))

    # Two price slots + one energy state + one measurement removed
    assert deleted == 4
    remaining = db.get_prices(BASE, BASE + timedelta(hours=1))
    assert [s.starts_at for s in remaining] == [
        BASE + timedelta(minutes=30),
        BASE + timedelta(minutes=45),
    ]


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
