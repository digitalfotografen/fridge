"""
Tests for :mod:`fridge.exporter`.

The exporter turns the three logged tables (prices, energy states and
measurements) into CSV or Parquet files, either merged into a single file or
one file per table.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from fridge.energy_db import (
    EnergyDatabase,
    EnergyStateSlot,
    HourlyStats,
    Measurement,
    PriceSlot,
)
from fridge.enums import EnergyState
from fridge.exporter import export_database

BASE = datetime(2026, 6, 23, 14, 0, tzinfo=UTC)


@pytest.fixture
def db() -> EnergyDatabase:
    database = EnergyDatabase(":memory:")
    database.upsert_prices([PriceSlot(starts_at=BASE, total=1.0, currency="SEK", level="NORMAL")])
    database.upsert_energy_states([EnergyStateSlot(BASE, EnergyState.LOW_PRICE)])
    database.insert_measurement(
        Measurement(
            recorded_at=BASE,
            temperature=4.2,
            humidity=58.0,
            mode="COOL",
            action_state="COOLING",
            energy_state="LOW_PRICE",
            target_temperature=5.0,
            target_humidity=55.0,
            price=1.0,
            price_currency="SEK",
        )
    )
    yield database
    database.close()


def test_merged_csv_is_default(db: EnergyDatabase, tmp_path):
    output = tmp_path / "fridge.csv"

    written = export_database(db, output)

    assert written == [output]
    assert output.exists()
    frame = pl.read_csv(output)
    # Merged frame is keyed on a single timestamp column.
    assert "timestamp" in frame.columns
    assert "temperature" in frame.columns
    assert "price_total" in frame.columns
    assert "energy_state" in frame.columns
    assert frame.height == 1


def test_separate_tables_writes_one_file_per_table(db: EnergyDatabase, tmp_path):
    output = tmp_path / "fridge.csv"

    written = export_database(db, output, separate_tables=True)

    names = {path.name for path in written}
    assert names == {
        "fridge_prices.csv",
        "fridge_energy_states.csv",
        "fridge_measurements.csv",
        "fridge_hourly_stats.csv",
    }
    for path in written:
        assert path.exists()

    measurements = pl.read_csv(tmp_path / "fridge_measurements.csv")
    assert measurements["temperature"][0] == 4.2


def test_parquet_export(db: EnergyDatabase, tmp_path):
    output = tmp_path / "fridge.parquet"

    written = export_database(db, output, fmt="parquet")

    assert written == [output]
    frame = pl.read_parquet(output)
    assert frame.height == 1
    assert "temperature" in frame.columns


def test_time_range_filters_rows(db: EnergyDatabase, tmp_path):
    db.insert_measurement(
        Measurement(
            recorded_at=BASE + timedelta(hours=2),
            temperature=9.9,
            humidity=50.0,
            mode="COOL",
            action_state="IDLE",
            energy_state="NORMAL_PRICE",
        )
    )
    output = tmp_path / "fridge.csv"

    export_database(db, output, start=BASE, end=BASE + timedelta(hours=1))

    frame = pl.read_csv(output)
    assert frame.height == 1


def test_empty_database_exports_header_only(tmp_path):
    database = EnergyDatabase(":memory:")
    try:
        output = tmp_path / "empty.csv"
        export_database(database, output)
    finally:
        database.close()

    frame = pl.read_csv(output)
    assert frame.height == 0


def test_unknown_format_raises(db: EnergyDatabase, tmp_path):
    with pytest.raises(ValueError):
        export_database(db, tmp_path / "x.txt", fmt="txt")


def _insert_hourly(db: EnergyDatabase, hour_start: datetime) -> None:
    db.insert_hourly_stats(
        HourlyStats(
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
    )


def test_separate_tables_writes_hourly_stats_file(db: EnergyDatabase, tmp_path):
    _insert_hourly(db, BASE)
    output = tmp_path / "fridge.csv"

    export_database(db, output, separate_tables=True)

    hourly = pl.read_csv(tmp_path / "fridge_hourly_stats.csv")
    assert hourly.height == 1
    assert hourly["cooler_duty"][0] == 62.5
    assert hourly["compressor_duty"][0] == 41.7
    assert hourly["sample_count"][0] == 720


def test_merged_export_left_joins_hourly_stats(db: EnergyDatabase, tmp_path):
    _insert_hourly(db, BASE)
    output = tmp_path / "fridge.csv"

    export_database(db, output)

    frame = pl.read_csv(output)
    assert frame.height == 1
    # Merged columns are prefixed to avoid clashes with the ``mode`` and
    # temperature aggregates.
    assert "hourly_mode" in frame.columns
    assert "hourly_cooler_duty" in frame.columns
    assert frame["hourly_cooler_duty"][0] == 62.5
    assert frame["hourly_compressor_duty"][0] == 41.7
