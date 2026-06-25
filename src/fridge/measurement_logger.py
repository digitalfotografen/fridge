"""
Periodically log temperature, humidity and controller states to the database.

The :class:`MeasurementLogger` is a small glue component that snapshots the
current state of the :class:`~fridge.thermostat.Thermostat` once per interval
(default 60 seconds) and stores it in the shared
:class:`~fridge.energy_db.EnergyDatabase` ``measurements`` table.

The stored rows are used for later analysis of equipment performance and of the
energy-cost savings achieved by the price-aware control. The current slot price
is included (when available) so that energy cost can be correlated with the
controller's behaviour.

The logger exposes a tick-based API for testing and runs a background thread
that wakes on a fixed interval.
"""

from __future__ import annotations

import logging
import math
import threading
from datetime import UTC, datetime

from fridge.energy_db import EnergyDatabase, Measurement

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 60.0


def _clean(value: float | None) -> float | None:
    """Return ``None`` for missing/NaN values, otherwise the value itself."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


class MeasurementLogger:
    """
    Log a snapshot of the controller state to the database on each interval.

    :param thermostat: Thermostat providing temperature, humidity and states.
    :param database: Shared database used to store measurements and read the
        current slot price.
    :param interval_seconds: Seconds between snapshots. Defaults to
        :data:`DEFAULT_INTERVAL_SECONDS`.
    """

    def __init__(
        self,
        thermostat,
        database: EnergyDatabase,
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        self._thermostat = thermostat
        self._database = database
        self._interval_seconds = interval_seconds

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start the background logging loop (idempotent)."""
        if self._started:
            return
        self._started = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="MeasurementLogger", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the background loop (idempotent)."""
        if not self._started:
            return
        self._started = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # ------------------------------------------------------------------
    # Core logic
    # ------------------------------------------------------------------
    def tick(self, now: datetime | None = None) -> Measurement:
        """
        Snapshot the current controller state and store it.

        :param now: Reference time, defaults to ``datetime.now(tz=UTC)``.
        :return: The :class:`~fridge.energy_db.Measurement` that was stored.
        """
        if now is None:
            now = datetime.now(tz=UTC)

        thresholds = getattr(self._thermostat, "thresholds", None)
        target_temperature = getattr(thresholds, "target_temperature", None)
        target_humidity = getattr(thresholds, "target_humidity", None)

        price = None
        price_currency = None
        price_slot = self._database.get_price_at(now)
        if price_slot is not None:
            price = price_slot.total
            price_currency = price_slot.currency or None

        measurement = Measurement(
            recorded_at=now,
            temperature=_clean(self._thermostat.temperature),
            humidity=_clean(self._thermostat.humidity),
            mode=self._thermostat.get_mode().name,
            action_state=self._thermostat.get_action_state().name,
            energy_state=self._thermostat.get_energy_state().name,
            target_temperature=target_temperature,
            target_humidity=target_humidity,
            price=price,
            price_currency=price_currency,
        )
        self._database.insert_measurement(measurement)
        return measurement

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------
    def _run_loop(self) -> None:
        """Background loop: tick immediately then on each interval."""
        self._safe_tick()
        while not self._stop_event.is_set():
            if self._stop_event.wait(self._interval_seconds):
                break
            self._safe_tick()

    def _safe_tick(self) -> None:
        """Run :meth:`tick`, logging and swallowing any error."""
        try:
            self.tick()
        except Exception:  # noqa: BLE001 - logging must never crash the loop
            logger.exception("Failed to log measurement")
