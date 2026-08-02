"""
Export the logged database to CSV or Parquet files for analysis.

Three tables are stored in the shared application database: ``prices``,
``energy_states`` and ``measurements`` (see :mod:`fridge.energy_db`). This
module turns them into flat files that are convenient to analyse with tools
such as polars, pandas or a spreadsheet.

Two layouts are supported:

- **merged** (default): the three tables are joined on their timestamp into a
  single wide table and written to one file. This is the most convenient layout
  for correlating equipment behaviour with the energy price.
- **separate tables**: one file per table is written, with the table name
  appended to the output stem.

Both layouts are available in CSV and Parquet formats.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from fridge.energy_db import EnergyDatabase

# Wide bounds used when the caller does not restrict the time range.
_MIN_TIME = datetime(1970, 1, 1, tzinfo=UTC)
_MAX_TIME = datetime(2999, 1, 1, tzinfo=UTC)

#: Supported export formats mapped to their file suffix.
FORMATS = ("csv", "parquet")

_PRICES_SCHEMA = {
    "starts_at": pl.String,
    "total": pl.Float64,
    "energy": pl.Float64,
    "tax": pl.Float64,
    "currency": pl.String,
    "level": pl.String,
}

_ENERGY_STATES_SCHEMA = {
    "starts_at": pl.String,
    "energy_state": pl.String,
}

_MEASUREMENTS_SCHEMA = {
    "recorded_at": pl.String,
    "temperature": pl.Float64,
    "humidity": pl.Float64,
    "mode": pl.String,
    "action_state": pl.String,
    "energy_state": pl.String,
    "target_temperature": pl.Float64,
    "target_humidity": pl.Float64,
    "price": pl.Float64,
    "price_currency": pl.String,
}

_HOURLY_STATS_SCHEMA = {
    "hour_start": pl.String,
    "mode": pl.String,
    "min_temperature": pl.Float64,
    "max_temperature": pl.Float64,
    "mean_temperature": pl.Float64,
    "min_humidity": pl.Float64,
    "max_humidity": pl.Float64,
    "mean_humidity": pl.Float64,
    "cooler_duty": pl.Float64,
    "compressor_duty": pl.Float64,
    "sample_count": pl.Int64,
}


def _isoformat(value: datetime | None) -> str | None:
    """Return the ISO-8601 string for *value* or ``None``."""
    return value.isoformat() if value is not None else None


def _prices_frame(database: EnergyDatabase, start: datetime, end: datetime) -> pl.DataFrame:
    rows = []
    for slot in database.get_prices(start, end):
        record = asdict(slot)
        record["starts_at"] = _isoformat(slot.starts_at)
        rows.append(record)
    return pl.DataFrame(rows, schema=_PRICES_SCHEMA)


def _energy_states_frame(database: EnergyDatabase, start: datetime, end: datetime) -> pl.DataFrame:
    rows = [
        {"starts_at": _isoformat(slot.starts_at), "energy_state": slot.energy_state.name}
        for slot in database.get_energy_states(start, end)
    ]
    return pl.DataFrame(rows, schema=_ENERGY_STATES_SCHEMA)


def _measurements_frame(database: EnergyDatabase, start: datetime, end: datetime) -> pl.DataFrame:
    rows = []
    for measurement in database.get_measurements(start, end):
        record = asdict(measurement)
        record["recorded_at"] = _isoformat(measurement.recorded_at)
        rows.append(record)
    return pl.DataFrame(rows, schema=_MEASUREMENTS_SCHEMA)


def _hourly_stats_frame(database: EnergyDatabase, start: datetime, end: datetime) -> pl.DataFrame:
    rows = []
    for stats in database.get_hourly_stats(start, end):
        record = asdict(stats)
        record["hour_start"] = _isoformat(stats.hour_start)
        rows.append(record)
    return pl.DataFrame(rows, schema=_HOURLY_STATS_SCHEMA)


def _tables(database: EnergyDatabase, start: datetime, end: datetime) -> dict[str, pl.DataFrame]:
    """Return all logged tables as named polars frames."""
    return {
        "prices": _prices_frame(database, start, end),
        "energy_states": _energy_states_frame(database, start, end),
        "measurements": _measurements_frame(database, start, end),
        "hourly_stats": _hourly_stats_frame(database, start, end),
    }


def _merged_frame(tables: dict[str, pl.DataFrame]) -> pl.DataFrame:
    """
    Join the three tables on a common ``timestamp`` column.

    ``prices`` and ``energy_states`` are keyed on their slot start while
    ``measurements`` are keyed on their record time. The frames are renamed to
    avoid column clashes, outer-joined on ``timestamp`` and sorted by time.
    """
    prices = tables["prices"].rename({"starts_at": "timestamp"})
    prices = prices.rename(
        {column: f"price_{column}" for column in prices.columns if column != "timestamp"}
    )

    energy_states = tables["energy_states"].rename({"starts_at": "timestamp"})

    measurements = tables["measurements"].rename({"recorded_at": "timestamp"})

    merged = prices.join(energy_states, on="timestamp", how="full", coalesce=True)
    merged = merged.join(measurements, on="timestamp", how="full", coalesce=True)

    # Left-join hourly stats on hour-truncated timestamp so each row is
    # annotated with the aggregates of the hour it falls into.
    hourly = tables["hourly_stats"]
    if not hourly.is_empty():
        hourly = hourly.rename(
            {
                column: ("timestamp_hour" if column == "hour_start" else f"hourly_{column}")
                for column in hourly.columns
            }
        )
        # Derive an ISO-hour key from the merged timestamp column by
        # replacing everything past the hour with ``:00:00+00:00``. All our
        # timestamps are stored as UTC ISO-8601 strings ending in ``+00:00``,
        # so this yields the same key format that :func:`_hourly_stats_frame`
        # produces.
        merged = merged.with_columns(
            (pl.col("timestamp").str.slice(0, 13) + pl.lit(":00:00+00:00")).alias("timestamp_hour")
        )
        merged = merged.join(hourly, on="timestamp_hour", how="left")
        merged = merged.drop("timestamp_hour")

    if not merged.is_empty():
        merged = merged.sort("timestamp")
    return merged


def _write(frame: pl.DataFrame, path: Path, fmt: str) -> None:
    if fmt == "csv":
        frame.write_csv(path)
    elif fmt == "parquet":
        frame.write_parquet(path)
    else:  # pragma: no cover - guarded by the CLI choice
        raise ValueError(f"Unsupported format: {fmt!r}")


def export_database(
    database: EnergyDatabase,
    output: str | Path,
    *,
    fmt: str = "csv",
    separate_tables: bool = False,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[Path]:
    """
    Export the database to one or more files.

    :param database: The database to read from.
    :param output: Output path. For the merged layout this is the file itself;
        for the separate-tables layout the table name is inserted before the
        suffix (e.g. ``fridge.csv`` -> ``fridge_prices.csv``).
    :param fmt: ``"csv"`` (default) or ``"parquet"``.
    :param separate_tables: When ``True`` write one file per table; otherwise
        merge all tables into a single file.
    :param start: Optional inclusive lower time bound.
    :param end: Optional exclusive upper time bound.
    :return: The list of written file paths.
    """
    if fmt not in FORMATS:
        raise ValueError(f"Unsupported format: {fmt!r}")

    start = start or _MIN_TIME
    end = end or _MAX_TIME
    output = Path(output)

    tables = _tables(database, start, end)

    if separate_tables:
        written: list[Path] = []
        for name, frame in tables.items():
            path = output.with_name(f"{output.stem}_{name}{output.suffix or '.' + fmt}")
            _write(frame, path, fmt)
            written.append(path)
        return written

    merged = _merged_frame(tables)
    path = output if output.suffix else output.with_suffix(f".{fmt}")
    _write(merged, path, fmt)
    return [path]
