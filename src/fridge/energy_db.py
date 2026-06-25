"""
SQLite storage for energy prices and computed energy states.

This module provides a small persistence layer used by the energy-price
optimisation feature. It stores two kinds of records in a single SQLite
database (``~/fridge_data/fridge_prices.db`` by default):

- ``prices``: raw 15-minute price slots fetched from Tibber. These are kept
  for future reference and act as a cache so the controller can work even when
  the Tibber API is temporarily unreachable.
- ``energy_states``: the :class:`~fridge.enums.EnergyState` computed by the
  classification algorithm for each 15-minute slot. This table is a cache of
  the planning result so the control loop does not have to recompute it on
  every tick.
- ``measurements``: periodic snapshots of temperature, humidity and controller
  states logged once per minute. These are kept for later analysis of equipment
  performance and energy-cost savings.

This is the single database for the whole application (prices, energy states and
measurements share one SQLite file).

All timestamps are stored as ISO-8601 strings in UTC. Datetimes passed in are
normalised to timezone-aware UTC before being stored, and returned as
timezone-aware UTC datetimes.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from fridge.enums import EnergyState

DEFAULT_DATABASE_FILE = "~/fridge_data/fridge_prices.db"


def database_path(config) -> str:
    """
    Return the configured path to the shared application database.

    The path is read from the ``[Logging] database_file`` key, falling back to
    :data:`DEFAULT_DATABASE_FILE` when it is missing.

    :param config: A :class:`configparser.ConfigParser` instance.
    :return: The database path (``~`` is expanded by :class:`EnergyDatabase`).
    """
    return config.get("Logging", "database_file", fallback=DEFAULT_DATABASE_FILE)


@dataclass(frozen=True)
class PriceSlot:
    """
    A single 15-minute price slot.

    :param starts_at: Start of the slot (timezone-aware, stored in UTC).
    :param total: Total price including tax, used for classification.
    :param energy: Energy component of the price, if available.
    :param tax: Tax component of the price, if available.
    :param currency: Currency code, e.g. ``"SEK"``.
    :param level: Tibber price level string, e.g. ``"NORMAL"``, if available.
    """

    starts_at: datetime
    total: float
    energy: float | None = None
    tax: float | None = None
    currency: str = ""
    level: str | None = None


@dataclass(frozen=True)
class EnergyStateSlot:
    """
    A single 15-minute slot with its computed energy state.

    :param starts_at: Start of the slot (timezone-aware, stored in UTC).
    :param energy_state: Computed :class:`~fridge.enums.EnergyState`.
    """

    starts_at: datetime
    energy_state: EnergyState


@dataclass(frozen=True)
class Measurement:
    """
    A single periodic measurement snapshot.

    :param recorded_at: Time the measurement was taken (timezone-aware, stored
        in UTC).
    :param temperature: Internal temperature in degrees Celsius, if available.
    :param humidity: Relative humidity in percent, if available.
    :param mode: Current :class:`~fridge.enums.ModeState` name.
    :param action_state: Current :class:`~fridge.enums.ActionState` name.
    :param energy_state: Current :class:`~fridge.enums.EnergyState` name.
    :param target_temperature: Active target temperature, if any.
    :param target_humidity: Active target humidity, if any.
    :param price: Total price for the current slot, if available.
    :param price_currency: Currency of ``price``, if available.
    """

    recorded_at: datetime
    temperature: float | None
    humidity: float | None
    mode: str
    action_state: str
    energy_state: str
    target_temperature: float | None = None
    target_humidity: float | None = None
    price: float | None = None
    price_currency: str | None = None


def _to_utc(value: datetime) -> datetime:
    """
    Normalise a datetime to timezone-aware UTC.

    :param value: A naive or timezone-aware datetime. Naive values are assumed
        to already be in UTC.
    :return: A timezone-aware datetime in UTC.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    """Return the ISO-8601 representation of *value* in UTC."""
    return _to_utc(value).isoformat()


def _parse(value: str) -> datetime:
    """Parse an ISO-8601 string into a timezone-aware UTC datetime."""
    return _to_utc(datetime.fromisoformat(value))


class EnergyDatabase:
    """
    SQLite-backed store for price slots and computed energy states.

    The database is the common store for the fridge application's energy
    feature. The connection is created on construction and the required tables
    are ensured to exist.
    """

    def __init__(self, db_path: str) -> None:
        """
        Open (or create) the database at *db_path* and ensure the schema.

        :param db_path: Path to the SQLite file. ``~`` is expanded and any
            missing parent directories are created. Use ``":memory:"`` for an
            in-memory database (useful in tests).
        """
        if db_path != ":memory:":
            db_path = os.path.expanduser(db_path)
            parent = os.path.dirname(db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        # ``check_same_thread=False`` lets the controller, the Tibber fetcher
        # and the test threads share one connection. SQLite serialises writes
        # internally; our access pattern is low-frequency.
        self._connection = sqlite3.connect(db_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create the ``prices`` and ``energy_states`` tables if needed."""
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS prices (
                starts_at  TEXT PRIMARY KEY,
                total      REAL NOT NULL,
                energy     REAL,
                tax        REAL,
                currency   TEXT,
                level      TEXT,
                fetched_at TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS energy_states (
                starts_at    TEXT PRIMARY KEY,
                energy_state INTEGER NOT NULL,
                computed_at  TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS measurements (
                recorded_at        TEXT PRIMARY KEY,
                temperature        REAL,
                humidity           REAL,
                mode               TEXT NOT NULL,
                action_state       TEXT NOT NULL,
                energy_state       TEXT NOT NULL,
                target_temperature REAL,
                target_humidity    REAL,
                price              REAL,
                price_currency     TEXT
            )
            """
        )
        self._connection.commit()

    def upsert_prices(self, slots: list[PriceSlot]) -> None:
        """
        Insert or update price slots.

        Existing slots with the same ``starts_at`` are replaced, making this
        safe to call repeatedly with overlapping windows.

        :param slots: Price slots to store.
        """
        fetched_at = _iso(datetime.now(tz=UTC))
        self._connection.executemany(
            """
            INSERT INTO prices (starts_at, total, energy, tax, currency, level, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(starts_at) DO UPDATE SET
                total=excluded.total,
                energy=excluded.energy,
                tax=excluded.tax,
                currency=excluded.currency,
                level=excluded.level,
                fetched_at=excluded.fetched_at
            """,
            [
                (
                    _iso(slot.starts_at),
                    slot.total,
                    slot.energy,
                    slot.tax,
                    slot.currency,
                    slot.level,
                    fetched_at,
                )
                for slot in slots
            ],
        )
        self._connection.commit()

    def get_prices(self, start: datetime, end: datetime) -> list[PriceSlot]:
        """
        Return price slots with ``start <= starts_at < end``, ordered by time.

        :param start: Inclusive lower bound (UTC-normalised).
        :param end: Exclusive upper bound (UTC-normalised).
        :return: Ordered list of :class:`PriceSlot`.
        """
        rows = self._connection.execute(
            """
            SELECT starts_at, total, energy, tax, currency, level
            FROM prices
            WHERE starts_at >= ? AND starts_at < ?
            ORDER BY starts_at
            """,
            (_iso(start), _iso(end)),
        ).fetchall()
        return [
            PriceSlot(
                starts_at=_parse(row["starts_at"]),
                total=row["total"],
                energy=row["energy"],
                tax=row["tax"],
                currency=row["currency"] or "",
                level=row["level"],
            )
            for row in rows
        ]

    def upsert_energy_states(self, slots: list[EnergyStateSlot]) -> None:
        """
        Insert or update computed energy-state slots.

        :param slots: Energy-state slots to store.
        """
        computed_at = _iso(datetime.now(tz=UTC))
        self._connection.executemany(
            """
            INSERT INTO energy_states (starts_at, energy_state, computed_at)
            VALUES (?, ?, ?)
            ON CONFLICT(starts_at) DO UPDATE SET
                energy_state=excluded.energy_state,
                computed_at=excluded.computed_at
            """,
            [(_iso(slot.starts_at), int(slot.energy_state), computed_at) for slot in slots],
        )
        self._connection.commit()

    def get_energy_states(self, start: datetime, end: datetime) -> list[EnergyStateSlot]:
        """
        Return energy-state slots with ``start <= starts_at < end``.

        :param start: Inclusive lower bound (UTC-normalised).
        :param end: Exclusive upper bound (UTC-normalised).
        :return: Ordered list of :class:`EnergyStateSlot`.
        """
        rows = self._connection.execute(
            """
            SELECT starts_at, energy_state
            FROM energy_states
            WHERE starts_at >= ? AND starts_at < ?
            ORDER BY starts_at
            """,
            (_iso(start), _iso(end)),
        ).fetchall()
        return [
            EnergyStateSlot(
                starts_at=_parse(row["starts_at"]),
                energy_state=EnergyState(row["energy_state"]),
            )
            for row in rows
        ]

    def get_energy_state_at(self, when: datetime) -> EnergyState | None:
        """
        Return the energy state for the 15-minute slot containing *when*.

        The slot whose ``starts_at`` is the latest value not after *when* is
        returned. This is the slot currently in effect.

        :param when: Point in time to look up.
        :return: The :class:`~fridge.enums.EnergyState` or ``None`` if no
            matching slot is stored.
        """
        row = self._connection.execute(
            """
            SELECT energy_state
            FROM energy_states
            WHERE starts_at <= ?
            ORDER BY starts_at DESC
            LIMIT 1
            """,
            (_iso(when),),
        ).fetchone()
        if row is None:
            return None
        return EnergyState(row["energy_state"])

    def get_price_at(self, when: datetime) -> PriceSlot | None:
        """
        Return the price slot containing *when* (latest slot not after *when*).

        :param when: Point in time to look up.
        :return: The :class:`PriceSlot` or ``None`` if no matching slot exists.
        """
        row = self._connection.execute(
            """
            SELECT starts_at, total, energy, tax, currency, level
            FROM prices
            WHERE starts_at <= ?
            ORDER BY starts_at DESC
            LIMIT 1
            """,
            (_iso(when),),
        ).fetchone()
        if row is None:
            return None
        return PriceSlot(
            starts_at=_parse(row["starts_at"]),
            total=row["total"],
            energy=row["energy"],
            tax=row["tax"],
            currency=row["currency"] or "",
            level=row["level"],
        )

    def insert_measurement(self, measurement: Measurement) -> None:
        """
        Insert or replace a single measurement snapshot.

        Existing rows with the same ``recorded_at`` are replaced, making this
        safe to call repeatedly.

        :param measurement: The :class:`Measurement` to store.
        """
        self._connection.execute(
            """
            INSERT INTO measurements (
                recorded_at, temperature, humidity, mode, action_state,
                energy_state, target_temperature, target_humidity, price, price_currency
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(recorded_at) DO UPDATE SET
                temperature=excluded.temperature,
                humidity=excluded.humidity,
                mode=excluded.mode,
                action_state=excluded.action_state,
                energy_state=excluded.energy_state,
                target_temperature=excluded.target_temperature,
                target_humidity=excluded.target_humidity,
                price=excluded.price,
                price_currency=excluded.price_currency
            """,
            (
                _iso(measurement.recorded_at),
                measurement.temperature,
                measurement.humidity,
                measurement.mode,
                measurement.action_state,
                measurement.energy_state,
                measurement.target_temperature,
                measurement.target_humidity,
                measurement.price,
                measurement.price_currency,
            ),
        )
        self._connection.commit()

    def get_measurements(self, start: datetime, end: datetime) -> list[Measurement]:
        """
        Return measurements with ``start <= recorded_at < end``, ordered by time.

        :param start: Inclusive lower bound (UTC-normalised).
        :param end: Exclusive upper bound (UTC-normalised).
        :return: Ordered list of :class:`Measurement`.
        """
        rows = self._connection.execute(
            """
            SELECT recorded_at, temperature, humidity, mode, action_state,
                   energy_state, target_temperature, target_humidity, price, price_currency
            FROM measurements
            WHERE recorded_at >= ? AND recorded_at < ?
            ORDER BY recorded_at
            """,
            (_iso(start), _iso(end)),
        ).fetchall()
        return [
            Measurement(
                recorded_at=_parse(row["recorded_at"]),
                temperature=row["temperature"],
                humidity=row["humidity"],
                mode=row["mode"],
                action_state=row["action_state"],
                energy_state=row["energy_state"],
                target_temperature=row["target_temperature"],
                target_humidity=row["target_humidity"],
                price=row["price"],
                price_currency=row["price_currency"],
            )
            for row in rows
        ]

    def prune(self, older_than: datetime) -> int:
        """
        Delete price, energy-state and measurement rows before *older_than*.

        :param older_than: Rows with timestamps ``< older_than`` are removed.
        :return: The total number of deleted rows.
        """
        cutoff = _iso(older_than)
        cursor = self._connection.execute("DELETE FROM prices WHERE starts_at < ?", (cutoff,))
        deleted = cursor.rowcount
        cursor = self._connection.execute(
            "DELETE FROM energy_states WHERE starts_at < ?", (cutoff,)
        )
        deleted += cursor.rowcount
        cursor = self._connection.execute(
            "DELETE FROM measurements WHERE recorded_at < ?", (cutoff,)
        )
        deleted += cursor.rowcount
        self._connection.commit()
        return deleted

    def close(self) -> None:
        """Close the underlying database connection."""
        self._connection.close()

    def __enter__(self) -> EnergyDatabase:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
