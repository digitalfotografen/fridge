"""
Tests for the Tibber price fetcher.

The :class:`fridge.tibber_client.TibberPriceFetcher` fetches prices from the
Tibber API and stores them in the energy database. These tests cover the pure
logic (resampling to 15-minute slots, home selection, response extraction and
scheduling) and the ``fetch_and_store`` flow with the network call mocked, so no
live API calls are made.
"""

from __future__ import annotations

from configparser import ConfigParser
from datetime import UTC, datetime, timedelta

import pytest

from fridge.energy_db import EnergyDatabase
from fridge.tibber_client import (
    SLOT_MINUTES,
    TibberPriceFetcher,
    parse_entries_to_slots,
)


def _config(**tibber_overrides: str) -> ConfigParser:
    cfg = ConfigParser()
    cfg["Tibber"] = {
        "api_key": "test-key",
        "timezone": "Europe/Stockholm",
        "fetch_hour": "16",
    }
    cfg["Tibber"].update(tibber_overrides)
    return cfg


def _entry(starts_at: str, total: float, **extra) -> dict:
    entry = {
        "startsAt": starts_at,
        "total": total,
        "energy": total * 0.7,
        "tax": total * 0.3,
        "level": "NORMAL",
        "currency": "SEK",
    }
    entry.update(extra)
    return entry


@pytest.fixture
def db() -> EnergyDatabase:
    database = EnergyDatabase(":memory:")
    yield database
    database.close()


@pytest.fixture
def fetcher(db: EnergyDatabase) -> TibberPriceFetcher:
    return TibberPriceFetcher(_config(), database=db)


# ----------------------------------------------------------------------
# parse_entries_to_slots
# ----------------------------------------------------------------------
def test_hourly_entry_expands_to_four_slots():
    entries = [
        _entry("2026-06-23T00:00:00.000+02:00", 1.0),
        _entry("2026-06-23T01:00:00.000+02:00", 2.0),
    ]

    slots = parse_entries_to_slots(entries)

    # Two hourly entries -> 8 fifteen-minute slots.
    assert len(slots) == 8
    # First entry is 00:00 +02:00 == 22:00 UTC previous day.
    assert slots[0].starts_at == datetime(2026, 6, 22, 22, 0, tzinfo=UTC)
    assert [s.starts_at for s in slots[:4]] == [
        datetime(2026, 6, 22, 22, 0, tzinfo=UTC) + timedelta(minutes=SLOT_MINUTES * k)
        for k in range(4)
    ]
    # All four sub-slots share the source price.
    assert all(s.total == 1.0 for s in slots[:4])
    assert all(s.total == 2.0 for s in slots[4:])
    assert slots[0].currency == "SEK"
    assert slots[0].level == "NORMAL"


def test_native_quarter_entries_are_not_split():
    entries = [
        _entry("2026-06-23T00:00:00.000+00:00", 1.0),
        _entry("2026-06-23T00:15:00.000+00:00", 1.1),
        _entry("2026-06-23T00:30:00.000+00:00", 1.2),
    ]

    slots = parse_entries_to_slots(entries)

    assert len(slots) == 3
    assert [s.total for s in slots] == [1.0, 1.1, 1.2]


def test_entries_are_sorted_and_normalised_to_utc():
    entries = [
        _entry("2026-06-23T01:00:00.000+02:00", 2.0),
        _entry("2026-06-23T00:00:00.000+02:00", 1.0),
    ]

    slots = parse_entries_to_slots(entries)

    times = [s.starts_at for s in slots]
    assert times == sorted(times)
    assert all(s.starts_at.tzinfo == UTC for s in slots)


def test_optional_fields_may_be_missing():
    slots = parse_entries_to_slots([{"startsAt": "2026-06-23T00:00:00.000+00:00", "total": 1.0}])

    assert len(slots) == 4
    assert slots[0].energy is None
    assert slots[0].tax is None
    assert slots[0].currency == ""
    assert slots[0].level is None


# ----------------------------------------------------------------------
# Home selection
# ----------------------------------------------------------------------
def test_select_home_id_uses_first_when_unconfigured(fetcher: TibberPriceFetcher):
    assert fetcher._select_home_id(["a", "b"]) == "a"


def test_select_home_id_uses_configured(db: EnergyDatabase):
    fetcher = TibberPriceFetcher(_config(home_id="b"), database=db)
    assert fetcher._select_home_id(["a", "b"]) == "b"


def test_select_home_id_falls_back_when_configured_missing(db: EnergyDatabase):
    fetcher = TibberPriceFetcher(_config(home_id="zzz"), database=db)
    assert fetcher._select_home_id(["a", "b"]) == "a"


def test_select_home_id_none_without_homes(fetcher: TibberPriceFetcher):
    assert fetcher._select_home_id([]) is None


# ----------------------------------------------------------------------
# Response extraction
# ----------------------------------------------------------------------
def test_extract_entries_picks_matching_home():
    data = {
        "viewer": {
            "homes": [
                {
                    "id": "a",
                    "currentSubscription": {
                        "priceInfo": {"today": [_entry("t", 1.0)], "tomorrow": []}
                    },
                },
                {
                    "id": "b",
                    "currentSubscription": {
                        "priceInfo": {
                            "today": [_entry("t", 9.0)],
                            "tomorrow": [_entry("t2", 9.5)],
                        }
                    },
                },
            ]
        }
    }

    entries = TibberPriceFetcher._extract_entries(data, "b")

    assert [e["total"] for e in entries] == [9.0, 9.5]


def test_extract_entries_handles_missing_subscription():
    data = {"viewer": {"homes": [{"id": "a", "currentSubscription": None}]}}

    assert TibberPriceFetcher._extract_entries(data, "a") == []


def test_extract_entries_handles_no_homes():
    assert TibberPriceFetcher._extract_entries({"viewer": {"homes": []}}, None) == []


# ----------------------------------------------------------------------
# Scheduling
# ----------------------------------------------------------------------
def test_next_fetch_time_is_today_when_before_hour(fetcher: TibberPriceFetcher):
    # 10:00 UTC == 12:00 Europe/Stockholm (summer), before 16:00 local.
    now = datetime(2026, 6, 23, 10, 0, tzinfo=UTC)

    target = fetcher._next_fetch_time(now)

    assert target.hour == 16
    assert target.date() == datetime(2026, 6, 23).date()


def test_next_fetch_time_rolls_to_tomorrow_when_past_hour(fetcher: TibberPriceFetcher):
    # 20:00 UTC == 22:00 Europe/Stockholm, after 16:00 local.
    now = datetime(2026, 6, 23, 20, 0, tzinfo=UTC)

    target = fetcher._next_fetch_time(now)

    assert target.hour == 16
    assert target.date() == datetime(2026, 6, 24).date()


def test_seconds_until_next_fetch_is_positive(fetcher: TibberPriceFetcher):
    now = datetime(2026, 6, 23, 10, 0, tzinfo=UTC)
    assert fetcher._seconds_until_next_fetch(now) > 0


def test_needs_initial_fetch_true_when_empty(fetcher: TibberPriceFetcher):
    assert fetcher._needs_initial_fetch(datetime(2026, 6, 23, 12, 0, tzinfo=UTC)) is True


def test_needs_initial_fetch_false_when_cached(fetcher: TibberPriceFetcher):
    now = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)
    fetcher._database.upsert_prices(
        parse_entries_to_slots([_entry("2026-06-23T12:00:00.000+00:00", 1.0)])
    )

    assert fetcher._needs_initial_fetch(now) is False


# ----------------------------------------------------------------------
# fetch_and_store
# ----------------------------------------------------------------------
def test_fetch_and_store_persists_slots(fetcher: TibberPriceFetcher, mocker):
    entries = [
        _entry("2026-06-23T00:00:00.000+00:00", 1.0),
        _entry("2026-06-23T01:00:00.000+00:00", 2.0),
    ]
    mocker.patch.object(fetcher, "_fetch_raw_entries", new=mocker.AsyncMock(return_value=entries))

    count = fetcher.fetch_and_store()

    assert count == 8
    stored = fetcher._database.get_prices(
        datetime(2026, 6, 23, 0, 0, tzinfo=UTC),
        datetime(2026, 6, 23, 2, 0, tzinfo=UTC),
    )
    assert len(stored) == 8


def test_fetch_and_store_swallows_errors(fetcher: TibberPriceFetcher, mocker):
    mocker.patch.object(
        fetcher,
        "_fetch_raw_entries",
        new=mocker.AsyncMock(side_effect=RuntimeError("boom")),
    )

    assert fetcher.fetch_and_store() == 0
