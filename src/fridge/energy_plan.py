"""
Energy-price classification algorithm.

This module turns the 15-minute Tibber price slots stored in the
:class:`~fridge.energy_db.EnergyDatabase` into a plan of
:class:`~fridge.enums.EnergyState` slots that the thermostat control uses to
shift cooling into cheaper periods.

The algorithm has four parts:

1. **Percentile bands.** Over the planning window each slot is classified by
   where its ``total`` price falls relative to the 33rd / 67th percentiles:
   below the low percentile is ``LOW_PRICE``, above the high percentile is
   ``HIGH_PRICE`` and everything in between is ``NORMAL_PRICE``. Percentiles are
   robust against price spikes and always use all three levels proportionally.
2. **Pre-cooling look-ahead.** For every ``HIGH_PRICE`` slot we look back up to
   ``precool_lookahead_slots`` slots and relabel any ``NORMAL_PRICE`` slot in
   that window as ``LOW_PRICE`` so the fridge is cooled down before the
   expensive period. The look-back stops at a preceding ``HIGH_PRICE`` slot so a
   high block is never crossed.
3. **Sanity checks.** ``HIGH_PRICE`` slots are never relabelled to
   ``LOW_PRICE`` (no look-back overshoot).
4. **Consecutive-LOW cap.** Runs of ``LOW_PRICE`` longer than
   ``max_consecutive_low_slots`` are trimmed by reverting the *earliest* slots
   in the run to ``NORMAL_PRICE``; the slots closest to the following demand are
   the most useful for pre-cooling and are kept.

:func:`classify_prices` is a pure function so it can be tested with synthetic
price curves. :class:`EnergyPlanner` wires it to the database.
"""

from __future__ import annotations

import logging
from configparser import ConfigParser
from datetime import UTC, datetime, timedelta

from fridge.energy_db import EnergyDatabase, EnergyStateSlot, PriceSlot, database_path
from fridge.enums import EnergyState

logger = logging.getLogger(__name__)

#: Length of a single price slot in minutes.
SLOT_MINUTES = 15
#: Default percentile (inclusive lower band) below which a slot is LOW_PRICE.
DEFAULT_LOW_PERCENTILE = 33.0
#: Default percentile above which a slot is HIGH_PRICE.
DEFAULT_HIGH_PERCENTILE = 67.0
#: Default number of slots to look back when pre-cooling (12 = 3 hours).
DEFAULT_PRECOOL_LOOKAHEAD_SLOTS = 12
#: Default cap on consecutive LOW_PRICE slots (4 = 1 hour).
DEFAULT_MAX_CONSECUTIVE_LOW_SLOTS = 4
#: How far ahead to plan / classify from "now".
PLANNING_HORIZON = timedelta(hours=48)
#: Default SQLite database path for prices and energy states.
DEFAULT_DATABASE_FILE = "~/fridge_data/fridge_prices.db"


def _percentile(sorted_values: list[float], percentile: float) -> float:
    """
    Return the linear-interpolated *percentile* of *sorted_values*.

    :param sorted_values: Values sorted ascending (must be non-empty).
    :param percentile: Percentile in the range ``[0, 100]``.
    :return: The interpolated percentile value.
    """
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (percentile / 100.0) * (len(sorted_values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = rank - lower
    return sorted_values[lower] * (1.0 - fraction) + sorted_values[upper] * fraction


def classify_prices(
    prices: list[PriceSlot],
    *,
    precool_lookahead_slots: int = DEFAULT_PRECOOL_LOOKAHEAD_SLOTS,
    max_consecutive_low_slots: int = DEFAULT_MAX_CONSECUTIVE_LOW_SLOTS,
    low_percentile: float = DEFAULT_LOW_PERCENTILE,
    high_percentile: float = DEFAULT_HIGH_PERCENTILE,
) -> list[EnergyStateSlot]:
    """
    Classify price slots into energy-state slots.

    :param prices: Ordered 15-minute price slots for the planning window.
    :param precool_lookahead_slots: Max slots to look back before a HIGH block
        when relabelling NORMAL slots to LOW for pre-cooling.
    :param max_consecutive_low_slots: Maximum number of consecutive LOW slots.
    :param low_percentile: Percentile below which a slot is LOW.
    :param high_percentile: Percentile above which a slot is HIGH.
    :return: Energy-state slots aligned to *prices*.
    """
    if not prices:
        return []

    totals = sorted(slot.total for slot in prices)
    low_threshold = _percentile(totals, low_percentile)
    high_threshold = _percentile(totals, high_percentile)

    labels: list[EnergyState] = []
    for slot in prices:
        if slot.total < low_threshold:
            labels.append(EnergyState.LOW_PRICE)
        elif slot.total > high_threshold:
            labels.append(EnergyState.HIGH_PRICE)
        else:
            labels.append(EnergyState.NORMAL_PRICE)

    _apply_precooling(labels, precool_lookahead_slots)
    _cap_consecutive_low(labels, max_consecutive_low_slots)

    return [
        EnergyStateSlot(starts_at=slot.starts_at, energy_state=label)
        for slot, label in zip(prices, labels, strict=True)
    ]


def _apply_precooling(labels: list[EnergyState], lookahead: int) -> None:
    """Relabel NORMAL slots before each HIGH block to LOW (in place)."""
    if lookahead <= 0:
        return
    for index, label in enumerate(labels):
        if label != EnergyState.HIGH_PRICE:
            continue
        for step in range(1, lookahead + 1):
            previous = index - step
            if previous < 0:
                break
            if labels[previous] == EnergyState.HIGH_PRICE:
                # Stop at the preceding high block; never cross it.
                break
            if labels[previous] == EnergyState.NORMAL_PRICE:
                labels[previous] = EnergyState.LOW_PRICE


def _cap_consecutive_low(labels: list[EnergyState], max_low: int) -> None:
    """Trim runs of LOW longer than *max_low* by reverting the earliest (in place)."""
    if max_low <= 0:
        return
    run_start = 0
    run_length = 0
    for index in range(len(labels) + 1):
        is_low = index < len(labels) and labels[index] == EnergyState.LOW_PRICE
        if is_low:
            if run_length == 0:
                run_start = index
            run_length += 1
            continue
        if run_length > max_low:
            excess = run_length - max_low
            for revert in range(run_start, run_start + excess):
                labels[revert] = EnergyState.NORMAL_PRICE
        run_length = 0


def _slot_start(when: datetime) -> datetime:
    """Floor *when* to the start of its 15-minute slot, in UTC."""
    when = when.astimezone(UTC)
    minute = (when.minute // SLOT_MINUTES) * SLOT_MINUTES
    return when.replace(minute=minute, second=0, microsecond=0)


class EnergyPlanner:
    """
    Classifies stored prices into cached energy states.

    :param config: Application configuration (uses the ``[Tibber]`` section).
    :param database: Optional :class:`~fridge.energy_db.EnergyDatabase`. When not
        provided one is created from the configured ``database_file`` and owned
        (closed) by this planner.
    """

    def __init__(self, config: ConfigParser, database: EnergyDatabase | None = None) -> None:
        self._precool_lookahead_slots = config.getint(
            "Tibber", "precool_lookahead_slots", fallback=DEFAULT_PRECOOL_LOOKAHEAD_SLOTS
        )
        self._max_consecutive_low_slots = config.getint(
            "Tibber", "max_consecutive_low_slots", fallback=DEFAULT_MAX_CONSECUTIVE_LOW_SLOTS
        )

        if database is None:
            db_path = database_path(config)
            self._database = EnergyDatabase(db_path)
            self._owns_database = True
        else:
            self._database = database
            self._owns_database = False

    @property
    def database(self) -> EnergyDatabase:
        """The energy database used to read prices and cache states."""
        return self._database

    def recompute(self, now: datetime | None = None) -> list[EnergyStateSlot]:
        """
        Re-classify the prices from the current slot forward and cache them.

        :param now: Reference time (defaults to the current UTC time). The
            current 15-minute slot and everything within the planning horizon is
            classified.
        :return: The computed energy-state slots (also stored in the database).
        """
        if now is None:
            now = datetime.now(tz=UTC)
        start = _slot_start(now)
        prices = self._database.get_prices(start, start + PLANNING_HORIZON)
        if not prices:
            logger.info("No prices available to classify from %s", start.isoformat())
            return []

        states = classify_prices(
            prices,
            precool_lookahead_slots=self._precool_lookahead_slots,
            max_consecutive_low_slots=self._max_consecutive_low_slots,
        )
        self._database.upsert_energy_states(states)
        logger.info("Computed %d energy-state slots from %s", len(states), start.isoformat())
        return states

    def close(self) -> None:
        """Close the database if this planner created it."""
        if self._owns_database:
            self._database.close()
