"""
Wall-clock UTC hourly duty-cycle aggregator.

The :class:`HourlyAggregator` samples the thermostat once per interval and
accumulates statistics for the *current* wall-clock UTC hour. On hour rollover
it finalises the just-closed hour into a
:class:`~fridge.energy_db.HourlyStats` row that is written to the shared
:class:`~fridge.energy_db.EnergyDatabase` and, when configured, published as a
retained MQTT message via :meth:`fridge.mqtt.MqttBridge.publish_hourly_stats`.

The running accumulator is periodically snapshotted to a diskcache so that a
mid-hour restart can resume without losing samples. If, on start-up, the
persisted snapshot belongs to an older hour, that hour is first finalised into
the database and then the accumulator is reset to the current hour.

Metrics
-------

- ``cooler_duty`` — percentage of the hour that ``thermostat.mains_on()`` was
  True (i.e. Port B Bit 0 HIGH).
- ``compressor_duty`` — percentage of the hour that
  ``thermostat.compressor_running()`` was True (``CoolerController`` state
  ``RUNNING``).
- ``min``/``max``/``mean`` for temperature and humidity, where the mean is
  time-weighted and NaN samples are ignored for the mean but still counted for
  the duty and mode accumulators.
- ``mode`` — the :class:`~fridge.enums.ModeState` with the greatest cumulative
  time in the hour, with the currently-active mode used as tie-break.
- ``sample_count`` — number of ``tick()`` invocations in the hour.
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from diskcache import Cache

from fridge.energy_db import EnergyDatabase, HourlyStats
from fridge.enums import ModeState

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 5.0
DEFAULT_PERSIST_INTERVAL_SECONDS = 30.0
_CACHE_KEY = "hourly_in_progress"


def _hour_floor(when: datetime) -> datetime:
    """Return *when* truncated to the start of its wall-clock UTC hour."""
    when = when.astimezone(UTC)
    return when.replace(minute=0, second=0, microsecond=0)


def _valid(value: float | None) -> bool:
    """True if *value* is neither ``None`` nor NaN."""
    if value is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    return True


@dataclass
class _Accumulator:
    """Serialisable accumulator for one wall-clock UTC hour."""

    hour_start: datetime
    last_sample_time: datetime | None = None
    sample_count: int = 0
    cooler_time: float = 0.0
    compressor_time: float = 0.0
    time_in_mode: dict[ModeState, float] = field(default_factory=dict)
    active_mode: ModeState = ModeState.IDLE

    min_temperature: float | None = None
    max_temperature: float | None = None
    sum_temperature_dt: float = 0.0
    sum_temperature_dt_valid: float = 0.0

    min_humidity: float | None = None
    max_humidity: float | None = None
    sum_humidity_dt: float = 0.0
    sum_humidity_dt_valid: float = 0.0


class HourlyAggregator:
    """
    Per-hour duty-cycle and climate aggregator.

    :param thermostat: Provider of the sampled state (must expose
        ``get_mode()``, ``mains_on()``, ``compressor_running()`` and the
        ``temperature`` / ``humidity`` attributes).
    :param database: Shared database used to persist finalised hours.
    :param cache_dir: Directory backing the ``diskcache`` used for
        in-progress-hour persistence.
    :param mqtt_bridge: Optional MQTT bridge. If provided, its
        ``publish_hourly_stats(row)`` is invoked on hour rollover. Exceptions
        from the bridge are swallowed and logged so a broker outage cannot
        break the aggregator.
    :param interval_seconds: Interval between background ticks.
    :param persist_interval_seconds: Minimum wall-clock interval between two
        successive snapshots of the in-progress hour to the diskcache.
    """

    def __init__(
        self,
        thermostat,
        database: EnergyDatabase,
        *,
        cache_dir: str,
        mqtt_bridge=None,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        persist_interval_seconds: float = DEFAULT_PERSIST_INTERVAL_SECONDS,
    ) -> None:
        self._thermostat = thermostat
        self._database = database
        self._mqtt = mqtt_bridge
        self._cache_dir = cache_dir
        self._interval_seconds = interval_seconds
        self._persist_interval_seconds = persist_interval_seconds

        self._cache: Cache | None = None
        self._acc: _Accumulator = _Accumulator(hour_start=_hour_floor(datetime.now(tz=UTC)))
        self._last_persist_at: datetime | None = None

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False

    # ------------------------------------------------------------------
    # Public read-only views (used by tests and diagnostics)
    # ------------------------------------------------------------------
    @property
    def current_hour_start(self) -> datetime:
        return self._acc.hour_start

    @property
    def sample_count(self) -> int:
        return self._acc.sample_count

    @property
    def cooler_time(self) -> float:
        return self._acc.cooler_time

    @property
    def compressor_time(self) -> float:
        return self._acc.compressor_time

    @property
    def min_temperature(self) -> float | None:
        return self._acc.min_temperature

    @property
    def max_temperature(self) -> float | None:
        return self._acc.max_temperature

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start the background loop and restore any persisted accumulator."""
        if self._started:
            return
        self._started = True
        self._open_cache()
        self._restore_from_cache(datetime.now(tz=UTC))
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="HourlyAggregator", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the background loop and snapshot the running accumulator."""
        if not self._started:
            return
        self._started = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        # Best-effort final snapshot so a subsequent restart can resume.
        try:
            self._persist_snapshot(force=True)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to snapshot hourly accumulator on stop")
        self._close_cache()

    # ------------------------------------------------------------------
    # Core logic
    # ------------------------------------------------------------------
    def tick(self, now: datetime | None = None) -> None:
        """
        Sample the thermostat and advance the accumulator.

        :param now: Reference time (must be timezone-aware); defaults to
            ``datetime.now(tz=UTC)``.
        """
        if now is None:
            now = datetime.now(tz=UTC)
        else:
            now = now.astimezone(UTC)

        acc = self._acc
        # Bootstrap on the first sample of a fresh accumulator: seed the
        # sampling time (no dt to accumulate yet).
        if acc.last_sample_time is None:
            acc.hour_start = _hour_floor(now)
            acc.last_sample_time = now
            self._sample_min_max(now_valid_dt=0.0)
            acc.sample_count += 1
            acc.active_mode = self._thermostat.get_mode()
            self._maybe_persist(now)
            return

        # Split dt at any hour boundaries we cross since the last sample.
        remaining_start = acc.last_sample_time
        while True:
            hour_end = self._acc.hour_start + timedelta(hours=1)
            if now < hour_end:
                dt = (now - remaining_start).total_seconds()
                self._accumulate(dt)
                break
            # Cross the boundary: accumulate up to hour_end, finalise, reset.
            dt_before = (hour_end - remaining_start).total_seconds()
            self._accumulate(dt_before)
            self._finalize_current_hour()
            self._reset_to_hour(hour_end)
            remaining_start = hour_end
            # Loop again in case ``now`` is more than one hour ahead.

        acc = self._acc  # ``_reset_to_hour`` swapped the accumulator.
        acc.last_sample_time = now
        acc.sample_count += 1
        acc.active_mode = self._thermostat.get_mode()
        self._maybe_persist(now)

    # ------------------------------------------------------------------
    # Accumulation helpers
    # ------------------------------------------------------------------
    def _sample_min_max(self, *, now_valid_dt: float) -> None:
        """Update min/max on the current thermostat readings (used at bootstrap)."""
        temperature = getattr(self._thermostat, "temperature", None)
        humidity = getattr(self._thermostat, "humidity", None)
        acc = self._acc
        if _valid(temperature):
            acc.min_temperature = (
                temperature
                if acc.min_temperature is None
                else min(acc.min_temperature, temperature)
            )
            acc.max_temperature = (
                temperature
                if acc.max_temperature is None
                else max(acc.max_temperature, temperature)
            )
        if _valid(humidity):
            acc.min_humidity = (
                humidity if acc.min_humidity is None else min(acc.min_humidity, humidity)
            )
            acc.max_humidity = (
                humidity if acc.max_humidity is None else max(acc.max_humidity, humidity)
            )

    def _accumulate(self, dt: float) -> None:
        """Add *dt* seconds of the currently-sampled state to the accumulator."""
        if dt <= 0.0:
            return
        acc = self._acc
        mode = self._thermostat.get_mode()
        acc.time_in_mode[mode] = acc.time_in_mode.get(mode, 0.0) + dt
        if self._thermostat.mains_on():
            acc.cooler_time += dt
        if self._thermostat.compressor_running():
            acc.compressor_time += dt

        temperature = getattr(self._thermostat, "temperature", None)
        humidity = getattr(self._thermostat, "humidity", None)
        if _valid(temperature):
            acc.min_temperature = (
                temperature
                if acc.min_temperature is None
                else min(acc.min_temperature, temperature)
            )
            acc.max_temperature = (
                temperature
                if acc.max_temperature is None
                else max(acc.max_temperature, temperature)
            )
            acc.sum_temperature_dt += temperature * dt
            acc.sum_temperature_dt_valid += dt
        if _valid(humidity):
            acc.min_humidity = (
                humidity if acc.min_humidity is None else min(acc.min_humidity, humidity)
            )
            acc.max_humidity = (
                humidity if acc.max_humidity is None else max(acc.max_humidity, humidity)
            )
            acc.sum_humidity_dt += humidity * dt
            acc.sum_humidity_dt_valid += dt

    # ------------------------------------------------------------------
    # Aggregate computation
    # ------------------------------------------------------------------
    def _weighted_mean_temperature(self) -> float | None:
        return self._weighted_mean(self._acc.sum_temperature_dt, self._acc.sum_temperature_dt_valid)

    def _weighted_mean_humidity(self) -> float | None:
        return self._weighted_mean(self._acc.sum_humidity_dt, self._acc.sum_humidity_dt_valid)

    @staticmethod
    def _weighted_mean(total: float, weight: float) -> float | None:
        if weight <= 0.0:
            return None
        return total / weight

    def _dominant_mode(self) -> ModeState:
        acc = self._acc
        if not acc.time_in_mode:
            return acc.active_mode
        max_time = max(acc.time_in_mode.values())
        candidates = [m for m, t in acc.time_in_mode.items() if t == max_time]
        if len(candidates) == 1:
            return candidates[0]
        # Tie-break: mode active at end-of-hour.
        if acc.active_mode in candidates:
            return acc.active_mode
        return candidates[0]

    def _current_row(self) -> HourlyStats:
        acc = self._acc
        hour_seconds = 3600.0
        return HourlyStats(
            hour_start=acc.hour_start,
            mode=self._dominant_mode().name,
            min_temperature=acc.min_temperature,
            max_temperature=acc.max_temperature,
            mean_temperature=self._weighted_mean_temperature(),
            min_humidity=acc.min_humidity,
            max_humidity=acc.max_humidity,
            mean_humidity=self._weighted_mean_humidity(),
            cooler_duty=100.0 * acc.cooler_time / hour_seconds,
            compressor_duty=100.0 * acc.compressor_time / hour_seconds,
            sample_count=acc.sample_count,
        )

    # ------------------------------------------------------------------
    # Rollover / reset
    # ------------------------------------------------------------------
    def _finalize_current_hour(self) -> None:
        row = self._current_row()
        try:
            self._database.insert_hourly_stats(row)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to write hourly_stats row for %s", row.hour_start)
            return
        if self._mqtt is not None:
            try:
                self._mqtt.publish_hourly_stats(row)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to publish hourly_stats to MQTT")

    def _reset_to_hour(self, hour_start: datetime) -> None:
        self._acc = _Accumulator(hour_start=_hour_floor(hour_start))
        # ``last_sample_time`` is None => next accumulate() call will do the
        # right thing after we set it below in ``tick()``. However, we're
        # called from within ``tick()`` when crossing an hour boundary and
        # still have a coherent stream of samples: continue accumulating
        # against the *new* hour by seeding ``last_sample_time`` to the hour
        # boundary.
        self._acc.last_sample_time = hour_start
        self._acc.active_mode = self._thermostat.get_mode()
        # Clear the persisted snapshot: the fresh accumulator will be
        # snapshotted again once ``_maybe_persist`` fires.
        self._delete_snapshot()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _open_cache(self) -> None:
        if self._cache is None:
            self._cache = Cache(self._cache_dir)

    def _close_cache(self) -> None:
        if self._cache is not None:
            try:
                self._cache.close()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to close hourly-stats diskcache")
            self._cache = None

    def _restore_from_cache(self, now: datetime) -> None:
        if self._cache is None:
            return
        snapshot = self._cache.get(_CACHE_KEY)
        current_hour = _hour_floor(now)
        if snapshot is None:
            # No previous run: start a fresh accumulator for the current hour.
            self._acc = _Accumulator(hour_start=current_hour)
            return
        if not isinstance(snapshot, _Accumulator):
            logger.warning("Discarding unexpected hourly-stats snapshot type %r", type(snapshot))
            self._acc = _Accumulator(hour_start=current_hour)
            self._delete_snapshot()
            return
        if snapshot.hour_start == current_hour:
            self._acc = snapshot
            return
        # Stale snapshot: finalise it, then start fresh.
        self._acc = snapshot
        self._finalize_current_hour()
        self._acc = _Accumulator(hour_start=current_hour)
        self._delete_snapshot()

    def _maybe_persist(self, now: datetime) -> None:
        if self._cache is None:
            return
        if (
            self._last_persist_at is None
            or (now - self._last_persist_at).total_seconds() >= self._persist_interval_seconds
        ):
            self._persist_snapshot(force=False)
            self._last_persist_at = now

    def _persist_snapshot(self, *, force: bool) -> None:
        if self._cache is None:
            return
        try:
            self._cache.set(_CACHE_KEY, self._acc)
        except Exception:  # noqa: BLE001
            if force:
                raise
            logger.exception("Failed to snapshot hourly accumulator")

    def _delete_snapshot(self) -> None:
        if self._cache is None:
            return
        try:
            self._cache.delete(_CACHE_KEY)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to delete hourly-stats snapshot")

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------
    def _run_loop(self) -> None:
        self._safe_tick()
        while not self._stop_event.is_set():
            if self._stop_event.wait(self._interval_seconds):
                break
            self._safe_tick()

    def _safe_tick(self) -> None:
        try:
            self.tick()
        except Exception:  # noqa: BLE001
            logger.exception("HourlyAggregator tick failed")
