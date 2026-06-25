"""
Tibber price fetcher.

This module fetches electricity prices from the Tibber API and stores them in
the :class:`~fridge.energy_db.EnergyDatabase`. The stored prices are used by the
energy-price optimisation feature (classification + thermostat control) and act
as an offline cache so the controller keeps working when the network or the
Tibber API is temporarily unavailable.

Design notes:

- Prices are fetched with a small, explicit GraphQL query through ``pyTibber``'s
  :meth:`tibber.Tibber.execute`. This gives us the full ``total``/``energy``/
  ``tax``/``level`` fields for *today* and *tomorrow*.
- The network call is wrapped with Tenacity (exponential backoff, capped number
  of attempts) and never raises into the control loop -- failures are logged and
  the cached prices are reused instead.
- Tibber currently returns hourly price entries in most markets. To present a
  uniform grid to the rest of the application we resample every source entry
  onto consecutive 15-minute slots that share the same price. If Tibber returns
  native 15-minute entries this becomes a no-op.
- Fetching runs in a background thread: once on start-up (if the cache does not
  already cover the current slot) and then once per day at a configurable local
  hour (default 16:00 ``Europe/Stockholm``), when the next day's prices become
  available.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from configparser import ConfigParser
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import tibber
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from fridge.energy_db import EnergyDatabase, PriceSlot

logger = logging.getLogger(__name__)

#: Length of a single price slot in minutes.
SLOT_MINUTES = 15
#: Assumed duration of the final source entry when it cannot be derived from the
#: start of a following entry.
DEFAULT_ENTRY_MINUTES = 60
#: Default SQLite database path for prices and energy states.
DEFAULT_DATABASE_FILE = "~/fridge_data/fridge_prices.db"
#: Default local hour (24h) at which the daily fetch runs.
DEFAULT_FETCH_HOUR = 16
#: Default IANA timezone used for scheduling the daily fetch.
DEFAULT_TIMEZONE = "Europe/Stockholm"
#: User agent reported to the Tibber API.
USER_AGENT = "fridge-controller"

#: GraphQL query returning price info for every home on the account.
PRICE_INFO_QUERY = """
{
  viewer {
    homes {
      id
      currentSubscription {
        priceInfo {
          today { startsAt total energy tax level currency }
          tomorrow { startsAt total energy tax level currency }
        }
      }
    }
  }
}
"""


def _opt_float(value: object) -> float | None:
    """Return *value* as ``float`` or ``None`` if it is missing/empty."""
    if value is None:
        return None
    return float(value)  # type: ignore[arg-type]


def parse_entries_to_slots(entries: list[dict]) -> list[PriceSlot]:
    """
    Convert raw Tibber ``priceInfo`` entries into 15-minute price slots.

    Each source entry covers the time until the next entry's start (or
    :data:`DEFAULT_ENTRY_MINUTES` for the final entry). It is expanded into
    consecutive 15-minute slots that all share the same price values so the rest
    of the system can operate on a uniform 15-minute grid regardless of the
    resolution Tibber returns. All timestamps are normalised to UTC.

    :param entries: Raw entries as returned by the Tibber API. Each entry must
        contain at least ``startsAt`` and ``total``.
    :return: Ordered list of :class:`~fridge.energy_db.PriceSlot`.
    """
    parsed: list[tuple[datetime, dict]] = []
    for entry in entries:
        starts_at = datetime.fromisoformat(entry["startsAt"]).astimezone(UTC)
        parsed.append((starts_at, entry))
    parsed.sort(key=lambda item: item[0])

    slots: list[PriceSlot] = []
    for index, (starts_at, entry) in enumerate(parsed):
        if index + 1 < len(parsed):
            duration = parsed[index + 1][0] - starts_at
        elif index > 0:
            # Reuse the previous gap for the final entry so a native 15-minute
            # feed is not wrongly expanded to a full hour.
            duration = starts_at - parsed[index - 1][0]
        else:
            duration = timedelta(minutes=DEFAULT_ENTRY_MINUTES)
        steps = max(1, int(duration.total_seconds() // (SLOT_MINUTES * 60)))
        for step in range(steps):
            slot_start = starts_at + timedelta(minutes=SLOT_MINUTES * step)
            slots.append(
                PriceSlot(
                    starts_at=slot_start,
                    total=float(entry["total"]),
                    energy=_opt_float(entry.get("energy")),
                    tax=_opt_float(entry.get("tax")),
                    currency=entry.get("currency") or "",
                    level=entry.get("level"),
                )
            )
    return slots


class TibberPriceFetcher:
    """
    Fetches Tibber prices and stores them in the energy database.

    :param config: Application configuration (uses the ``[Tibber]`` section).
    :param database: Optional :class:`~fridge.energy_db.EnergyDatabase`. When not
        provided one is created from the configured ``database_file`` and owned
        (closed) by this fetcher.
    """

    def __init__(self, config: ConfigParser, database: EnergyDatabase | None = None) -> None:
        self._api_key = config.get("Tibber", "api_key", fallback="")
        self._home_id = config.get("Tibber", "home_id", fallback="").strip() or None
        self._fetch_hour = config.getint("Tibber", "fetch_hour", fallback=DEFAULT_FETCH_HOUR)
        timezone_name = config.get("Tibber", "timezone", fallback=DEFAULT_TIMEZONE)
        self._timezone = ZoneInfo(timezone_name)

        if database is None:
            db_path = config.get("Tibber", "database_file", fallback=DEFAULT_DATABASE_FILE)
            self._database = EnergyDatabase(db_path)
            self._owns_database = True
        else:
            self._database = database
            self._owns_database = False

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False

    @property
    def database(self) -> EnergyDatabase:
        """The energy database used to store fetched prices."""
        return self._database

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start the background fetch thread (idempotent)."""
        if self._started:
            return
        self._started = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="TibberFetcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the background thread and close an owned database."""
        if not self._started:
            return
        self._started = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self._owns_database:
            self._database.close()

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------
    def _run_loop(self) -> None:
        """Background loop: initial fetch then a daily fetch at ``fetch_hour``."""
        if self._needs_initial_fetch():
            self.fetch_and_store()
        while not self._stop_event.is_set():
            wait_seconds = self._seconds_until_next_fetch()
            if self._stop_event.wait(wait_seconds):
                break
            self.fetch_and_store()

    def _next_fetch_time(self, now: datetime) -> datetime:
        """Return the next ``fetch_hour`` boundary in the configured timezone."""
        local_now = now.astimezone(self._timezone)
        target = local_now.replace(hour=self._fetch_hour, minute=0, second=0, microsecond=0)
        if target <= local_now:
            target += timedelta(days=1)
        return target

    def _seconds_until_next_fetch(self, now: datetime | None = None) -> float:
        """Seconds until the next scheduled daily fetch."""
        now = now or datetime.now(tz=UTC)
        delta = self._next_fetch_time(now) - now.astimezone(self._timezone)
        return max(0.0, delta.total_seconds())

    def _needs_initial_fetch(self, now: datetime | None = None) -> bool:
        """True when the cache does not contain a price for the current slot."""
        now = now or datetime.now(tz=UTC)
        return self._database.get_price_at(now) is None

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------
    def fetch_and_store(self) -> int:
        """
        Fetch prices from Tibber and store them, returning the slot count.

        Network and parsing errors are logged and swallowed (returns ``0``) so
        the control loop keeps running on cached data.

        :return: Number of 15-minute price slots stored.
        """
        try:
            entries = asyncio.run(self._fetch_raw_entries())
        except RetryError as exc:
            logger.warning("Giving up fetching Tibber prices after retries: %s", exc)
            return 0
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Failed to fetch Tibber prices: %s", exc)
            return 0

        slots = parse_entries_to_slots(entries)
        if slots:
            self._database.upsert_prices(slots)
        logger.info("Stored %d Tibber price slots", len(slots))
        return len(slots)

    @retry(
        retry=retry_if_exception_type((aiohttp.ClientError, TimeoutError, asyncio.TimeoutError)),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, max=30),
        reraise=True,
    )
    async def _fetch_raw_entries(self) -> list[dict]:
        """
        Fetch raw price entries for the selected home from the Tibber API.

        :return: ``today`` + ``tomorrow`` price entries for the chosen home.
        """
        async with aiohttp.ClientSession() as session:
            client = tibber.Tibber(self._api_key, websession=session, user_agent=USER_AGENT)
            await client.update_info()
            home_id = self._select_home_id(client.get_home_ids())
            data = await client.execute(PRICE_INFO_QUERY)
            await client.close_connection()
        return self._extract_entries(data, home_id)

    def _select_home_id(self, home_ids: list[str]) -> str | None:
        """
        Choose which home to use based on configuration.

        :param home_ids: Home ids available on the account.
        :return: The configured home id when present, otherwise the first home.
            ``None`` when the account has no homes.
        """
        if not home_ids:
            logger.warning("Tibber account has no homes")
            return None
        if self._home_id is not None:
            if self._home_id in home_ids:
                return self._home_id
            logger.warning(
                "Configured Tibber home_id %s not found; using first home %s",
                self._home_id,
                home_ids[0],
            )
            return home_ids[0]
        if len(home_ids) > 1:
            logger.info(
                "Multiple Tibber homes found; using first (%s). "
                "Set [Tibber] home_id to choose another.",
                home_ids[0],
            )
        return home_ids[0]

    @staticmethod
    def _extract_entries(data: dict, home_id: str | None) -> list[dict]:
        """
        Extract ``today`` + ``tomorrow`` entries for *home_id* from a response.

        :param data: Raw GraphQL response from :data:`PRICE_INFO_QUERY`.
        :param home_id: Home id to extract; when ``None`` the first home is used.
        :return: List of raw price entries (possibly empty).
        """
        homes = (data.get("viewer") or {}).get("homes") or []
        if not homes:
            return []
        selected = None
        if home_id is not None:
            for home in homes:
                if home.get("id") == home_id:
                    selected = home
                    break
        if selected is None:
            selected = homes[0]
        subscription = selected.get("currentSubscription") or {}
        price_info = subscription.get("priceInfo") or {}
        today = price_info.get("today") or []
        tomorrow = price_info.get("tomorrow") or []
        return [*today, *tomorrow]
