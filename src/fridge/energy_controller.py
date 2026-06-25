"""
Apply the planner's cached :class:`~fridge.enums.EnergyState` to the
thermostat on each 15-minute slot boundary.

The :class:`EnergyController` is a thin glue component that ties the
:class:`~fridge.energy_plan.EnergyPlanner` to the
:class:`~fridge.thermostat.Thermostat`:

1. On every tick it looks up the energy state for the current 15-minute slot
   in the :class:`~fridge.energy_db.EnergyDatabase`.
2. If no state is cached for the current slot it asks the planner to
   recompute — this covers the case where prices have just been fetched but
   the plan has not been built yet.
3. If there is still no cached state (no prices at all, network outage on
   first start, ...) it falls back to :data:`EnergyState.NORMAL_PRICE` so the
   thermostat behaves safely without any network access.

The controller exposes a tick-based API for testing and runs a background
thread that wakes on each 15-minute slot boundary.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta

from fridge.energy_plan import SLOT_MINUTES, EnergyPlanner
from fridge.enums import EnergyState

logger = logging.getLogger(__name__)


def _slot_start(when: datetime) -> datetime:
    """Floor *when* to the start of its 15-minute slot, in UTC."""
    when = when.astimezone(UTC)
    minute = (when.minute // SLOT_MINUTES) * SLOT_MINUTES
    return when.replace(minute=minute, second=0, microsecond=0)


class EnergyController:
    """
    Apply the cached energy plan to the thermostat on each slot boundary.

    :param thermostat: Thermostat to receive ``set_energy_state`` calls.
    :param planner: Planner used to read the cache and recompute when stale.
    :param fallback_state: State applied when no plan is available. Defaults
        to :data:`EnergyState.NORMAL_PRICE`.
    """

    def __init__(
        self,
        thermostat,
        planner: EnergyPlanner,
        *,
        fallback_state: EnergyState = EnergyState.NORMAL_PRICE,
    ) -> None:
        self._thermostat = thermostat
        self._planner = planner
        self._fallback_state = fallback_state

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start the background slot-boundary loop (idempotent)."""
        if self._started:
            return
        self._started = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="EnergyController", daemon=True)
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
    def tick(self, now: datetime | None = None) -> EnergyState:
        """
        Apply the energy state for the current 15-minute slot.

        :param now: Reference time, defaults to ``datetime.now(tz=UTC)``.
        :return: The state that was applied to the thermostat.
        """
        if now is None:
            now = datetime.now(tz=UTC)

        database = self._planner.database
        state = database.get_energy_state_at(now)
        if state is None:
            # No plan yet for the current slot; try to (re)compute one.
            self._planner.recompute(now=now)
            state = database.get_energy_state_at(now)

        if state is None:
            logger.info("No energy plan available; falling back to %s", self._fallback_state.name)
            state = self._fallback_state

        self._thermostat.set_energy_state(state)
        return state

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------
    def _seconds_until_next_slot(self, now: datetime | None = None) -> float:
        """Return seconds until the next 15-minute slot boundary."""
        if now is None:
            now = datetime.now(tz=UTC)
        next_slot = _slot_start(now) + timedelta(minutes=SLOT_MINUTES)
        return max(0.0, (next_slot - now.astimezone(UTC)).total_seconds())

    def _run_loop(self) -> None:
        """Background loop: tick immediately then on each slot boundary."""
        self.tick()
        while not self._stop_event.is_set():
            if self._stop_event.wait(self._seconds_until_next_slot()):
                break
            self.tick()
