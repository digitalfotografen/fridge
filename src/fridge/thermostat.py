import logging
import math
import os
import threading
import time
from configparser import ConfigParser
from dataclasses import dataclass
from enum import Enum
from collections.abc import Callable

from diskcache import Cache

from fridge import ModeState, ActionState, EnergyState
from fridge import cache_path
from fridge import Sht30, RelayGpio, I2cGpio
from fridge.signals import (
    action_state_changed,
    energy_state_changed,
    mode_state_changed,
)

logger = logging.getLogger(__name__)


DEFAULT_COOL_DOWN_TARGET_TEMPERATURE = 12.0
DEFAULT_COOL_DOWN_HYSTERESIS = 1.5
DEFAULT_COOL_DOWN_FREEZE_GUARD = 9.0
DEFAULT_COOL_TARGET_TEMPERATURE = 5.0
DEFAULT_COOL_HYSTERESIS = 1.5
DEFAULT_COOL_FREEZE_GUARD = 2.0
DEFAULT_SPECIAL_TARGET_TEMPERATURE = 5.0
DEFAULT_SPECIAL_HYSTERESIS = 1.5
DEFAULT_SPECIAL_FREEZE_GUARD = 2.0
DEFAULT_TARGET_HUMIDITY = 55.0
DEFAULT_HUMIDITY_HYSTERESIS = 5.0
DEFAULT_LOW_PRICE_COOL_ADJ = -2.0
DEFAULT_LOW_PRICE_HEAT_ADJ = 1.0
DEFAULT_HIGH_PRICE_COOL_ADJ = 2.0
DEFAULT_HIGH_PRICE_HEAT_ADJ = -2.0
DEFAULT_MIN_VENT_FAN_MINUTES = 5.0
DEFAULT_MAX_COOLER_MINUTES = 30.0
DEFAULT_COOLER_PAUSE_MINUTES = 15.0
DEFAULT_COOLER_SOFT_START_MINUTES = 2.0
DEFAULT_AUTO_SWITCH_HOURS = 10.0


@dataclass
class ThermostatThresholds:
    """
    Dataclass for holding temperature and humidity thresholds.
    """

    target_temperature: float | None
    target_humidity: float | None
    heater_on_threshold: float | None
    heater_off_threshold: float | None
    cooler_on_threshold: float | None
    cooler_off_threshold: float | None
    fan_on_threshold: float | None
    fan_off_threshold: float | None


def _resolve_price_adjustments(
    config: ConfigParser, energy_state: EnergyState
) -> tuple[float, float]:
    """Return the (cool, heat) target adjustments for the current energy state."""
    if energy_state == EnergyState.LOW_PRICE:
        return (
            config.getfloat("Energy", "low_price_cool_adj", fallback=DEFAULT_LOW_PRICE_COOL_ADJ),
            config.getfloat("Energy", "low_price_heat_adj", fallback=DEFAULT_LOW_PRICE_HEAT_ADJ),
        )
    if energy_state == EnergyState.HIGH_PRICE:
        return (
            config.getfloat("Energy", "high_price_cool_adj", fallback=DEFAULT_HIGH_PRICE_COOL_ADJ),
            config.getfloat("Energy", "high_price_heat_adj", fallback=DEFAULT_HIGH_PRICE_HEAT_ADJ),
        )
    return 0.0, 0.0


def generate_thresholds(
    config: ConfigParser,
    mode: ModeState,
    energy_state: EnergyState,
) -> ThermostatThresholds:
    """
    Generate thresholds based on the mode, energy state and configuration.

    Cooling and heating are controlled independently:

    - Cooler thresholds are derived from ``target_temperature`` (adjusted by the
      LOW/HIGH price cool adjustment) and ``hysteresis``: on above
      ``target + hysteresis / 2``, off below ``target - hysteresis / 2``.
    - Heater thresholds are derived purely from ``freeze_guard`` (adjusted by the
      LOW/HIGH price heat adjustment): on below ``freeze_guard``, off above
      ``freeze_guard + hysteresis``. The heater is never coupled to the
      cooling target, so it can never be triggered by the cooler spinning
      down or by a duty-cycle pause.
    - ``IDLE`` returns all-``None`` thresholds.

    :param config: Application configuration
    :param mode: Operation mode state
    :param energy_state: Current energy pricing state
    :return: Thermostat thresholds
    """
    cool_adjustment, heat_adjustment = _resolve_price_adjustments(config, energy_state)

    def _cool_thresholds(target: float, hysteresis: float) -> tuple[float, float, float]:
        cooler_target = target + cool_adjustment
        return (
            cooler_target,
            cooler_target + hysteresis / 2.0,
            cooler_target - hysteresis / 2.0,
        )

    def _heat_thresholds(freeze_guard: float, hysteresis: float) -> tuple[float, float]:
        adjusted = freeze_guard + heat_adjustment
        return adjusted, adjusted + hysteresis

    match mode:
        case ModeState.IDLE:
            return ThermostatThresholds(
                target_temperature=None,
                target_humidity=None,
                heater_on_threshold=None,
                heater_off_threshold=None,
                cooler_on_threshold=None,
                cooler_off_threshold=None,
                fan_on_threshold=None,
                fan_off_threshold=None,
            )

        case ModeState.COOL_DOWN:
            target_temperature = config.getfloat(
                "CoolDownMode",
                "target_temperature",
                fallback=DEFAULT_COOL_DOWN_TARGET_TEMPERATURE,
            )
            hysteresis = config.getfloat(
                "CoolDownMode",
                "hysteresis",
                fallback=DEFAULT_COOL_DOWN_HYSTERESIS,
            )
            freeze_guard = config.getfloat(
                "CoolDownMode",
                "freeze_guard",
                fallback=DEFAULT_COOL_DOWN_FREEZE_GUARD,
            )

            cooler_target, cooler_on, cooler_off = _cool_thresholds(target_temperature, hysteresis)
            heater_on, heater_off = _heat_thresholds(freeze_guard, hysteresis)

            return ThermostatThresholds(
                target_temperature=cooler_target,
                target_humidity=None,
                heater_on_threshold=heater_on,
                heater_off_threshold=heater_off,
                cooler_on_threshold=cooler_on,
                cooler_off_threshold=cooler_off,
                fan_on_threshold=1.0,
                fan_off_threshold=0.0,
            )

        case ModeState.COOL:
            section = "CoolMode"
            default_freeze = DEFAULT_COOL_FREEZE_GUARD
            default_target = DEFAULT_COOL_TARGET_TEMPERATURE
            default_hyst = DEFAULT_COOL_HYSTERESIS

        case ModeState.SPECIAL:
            section = "SpecialMode"
            default_freeze = DEFAULT_SPECIAL_FREEZE_GUARD
            default_target = DEFAULT_SPECIAL_TARGET_TEMPERATURE
            default_hyst = DEFAULT_SPECIAL_HYSTERESIS

    target_temperature = config.getfloat(section, "target_temperature", fallback=default_target)
    hysteresis = config.getfloat(section, "hysteresis", fallback=default_hyst)
    freeze_guard = config.getfloat(section, "freeze_guard", fallback=default_freeze)
    target_humidity = config.getfloat(section, "target_humidity", fallback=DEFAULT_TARGET_HUMIDITY)
    humidity_hysteresis = config.getfloat(
        section, "humidity_hysteresis", fallback=DEFAULT_HUMIDITY_HYSTERESIS
    )

    cooler_target, cooler_on, cooler_off = _cool_thresholds(target_temperature, hysteresis)
    heater_on, heater_off = _heat_thresholds(freeze_guard, hysteresis)

    return ThermostatThresholds(
        target_temperature=cooler_target,
        target_humidity=target_humidity,
        heater_on_threshold=heater_on,
        heater_off_threshold=heater_off,
        cooler_on_threshold=cooler_on,
        cooler_off_threshold=cooler_off,
        fan_on_threshold=target_humidity + humidity_hysteresis,
        fan_off_threshold=target_humidity - humidity_hysteresis,
    )


class CoolerState(Enum):
    """States of the cooler duty-cycle state machine."""

    OFF = "OFF"
    SOFT_START = "SOFT_START"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    SPIN_DOWN = "SPIN_DOWN"


class CoolerController:
    """
    Duty-cycle state machine for the cooler unit (Port B Bit 0) and the
    compressor-pause relay (Port B Bit 3).

    The controller enforces:

    - Soft start: on power-up the compressor is held paused for
      ``soft_start_time`` seconds before the compressor is released.
    - Duty cycle: the compressor may only run continuously for
      ``max_run_time`` seconds, then must pause for ``pause_time`` seconds
      with the circulation fan (Bit 0 HIGH) still running.
    - Spin down: when a cool request drops, the compressor is paused for
      ``pause_time`` seconds before the cooler unit is de-energised so the
      heat exchanger can defrost. If a spin-down starts while the machine
      is already in a duty-cycle pause, the remaining pause is reused.

    Time is passed in explicitly via ``tick(now, cool_requested)`` so the
    state machine is fully deterministic and testable without freezegun.
    """

    def __init__(
        self,
        set_cooler: Callable[[bool], None],
        set_compressor_pause: Callable[[bool], None],
        max_run_time: float,
        pause_time: float,
        soft_start_time: float,
    ) -> None:
        """
        :param set_cooler: callback to drive Port B Bit 0
        :param set_compressor_pause: callback to drive Port B Bit 3
        :param max_run_time: max continuous compressor run time in seconds
        :param pause_time: forced pause duration in seconds
        :param soft_start_time: compressor delay after switching cooler on, seconds
        """
        self._set_cooler = set_cooler
        self._set_compressor_pause = set_compressor_pause
        self.max_run_time = max_run_time
        self.pause_time = pause_time
        self.soft_start_time = soft_start_time
        self.state: CoolerState = CoolerState.OFF
        self._entered_at: float = 0.0

    @property
    def cooler_active(self) -> bool:
        """Return True if the cooler unit is currently engaged (in any state except OFF)."""
        return self.state is not CoolerState.OFF

    def _enter(self, state: CoolerState, now: float) -> None:
        if state is self.state:
            return
        logger.info("CoolerController: %s -> %s", self.state.name, state.name)
        self.state = state
        self._entered_at = now

    def force_off(self, now: float) -> None:
        """
        Immediately drop both relays and reset the state machine.

        Used for IDLE mode / shutdown. Bypasses the graceful spin-down.
        """
        self._set_compressor_pause(False)
        self._set_cooler(False)
        self.state = CoolerState.OFF
        self._entered_at = now

    def tick(self, now: float, cool_requested: bool) -> None:
        """
        Advance the state machine one step.

        :param now: current time in seconds (monotonic)
        :param cool_requested: ``True`` if the temperature is above the
            cooler-on threshold or the cooler has not yet reached the
            cooler-off threshold (i.e. still needs cooling)
        """
        elapsed = now - self._entered_at

        match self.state:
            case CoolerState.OFF:
                if cool_requested:
                    # Switch cooler on: Bit 3 HIGH (paused), Bit 0 HIGH, soft-start delay.
                    self._set_compressor_pause(True)
                    self._set_cooler(True)
                    self._enter(CoolerState.SOFT_START, now)

            case CoolerState.SOFT_START:
                if not cool_requested:
                    # Temperature already low enough: skip to spin-down.
                    # Compressor never actually started, so reuse the current
                    # pause window as the spin-down pause.
                    self._enter(CoolerState.SPIN_DOWN, now)
                elif elapsed >= self.soft_start_time:
                    self._set_compressor_pause(False)
                    self._enter(CoolerState.RUNNING, now)

            case CoolerState.RUNNING:
                if not cool_requested:
                    self._set_compressor_pause(True)
                    self._enter(CoolerState.SPIN_DOWN, now)
                elif elapsed >= self.max_run_time:
                    self._set_compressor_pause(True)
                    self._enter(CoolerState.PAUSED, now)

            case CoolerState.PAUSED:
                if not cool_requested:
                    # Re-use the remaining pause time as the spin-down pause.
                    self._enter(CoolerState.SPIN_DOWN, self._entered_at)
                elif elapsed >= self.pause_time:
                    self._set_compressor_pause(False)
                    self._enter(CoolerState.RUNNING, now)

            case CoolerState.SPIN_DOWN:
                # Compressor already paused; keep Bit 0 HIGH while the fan
                # runs out the pause time, then drop the cooler unit.
                if elapsed >= self.pause_time:
                    self._set_cooler(False)
                    # Bit 3 stays HIGH per spec.
                    self.state = CoolerState.OFF
                    self._entered_at = now


class Thermostat:
    def __init__(self, gpio: I2cGpio, config: ConfigParser):
        """
        Initialize the thermostat with the given GPIO interface and sensor.
        Reads the current mode and energy state from the cache.
        :param gpio: I2C GPIO interface for communication with the relay
        :param config: Configuration parser for retrieving thresholds
        """
        self.config = config
        self.sensor = Sht30()
        self.relay = RelayGpio(gpio)
        self._cache_dir = os.path.expanduser(cache_path)
        self.cache = Cache(self._cache_dir)
        self._cache_closed = False
        self.mode = self.cache.get("mode", ModeState.IDLE)
        self.energy_state = self.cache.get("energy_state", EnergyState.NORMAL_PRICE)
        self.thresholds = generate_thresholds(
            config=config, mode=self.mode, energy_state=self.energy_state
        )
        self._temperature = math.nan
        self._humidity = math.nan
        self._action_state = self.cache.get("action_state", ActionState.IDLE)
        logger.info(
            "Restored state from cache: mode=%s, energy_state=%s, action_state=%s",
            self.mode.name,
            self.energy_state.name,
            self._action_state.name,
        )
        self.stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.refresh_rate = self.config.getfloat("General", "refresh_rate", fallback=1.0)
        self._min_vent_fan_seconds = (
            self.config.getfloat("General", "min_vent_fan", fallback=DEFAULT_MIN_VENT_FAN_MINUTES)
            * 60.0
        )
        self._last_vent_fan_switch: float | None = None
        self.cooler = CoolerController(
            set_cooler=self.relay.set_cooler_relay,
            set_compressor_pause=self.relay.set_compressor_pause_relay,
            max_run_time=self.config.getfloat(
                "General", "max_cooler_time", fallback=DEFAULT_MAX_COOLER_MINUTES
            )
            * 60.0,
            pause_time=self.config.getfloat(
                "General", "cooler_pause_time", fallback=DEFAULT_COOLER_PAUSE_MINUTES
            )
            * 60.0,
            soft_start_time=self.config.getfloat(
                "General", "cooler_soft_start_time", fallback=DEFAULT_COOLER_SOFT_START_MINUTES
            )
            * 60.0,
        )
        # Track whether the last resolved control decision asked for cooling.
        self._cool_requested = False

    def _ensure_cache_open(self) -> None:
        """Re-open the cache if it was previously closed."""
        if self._cache_closed:
            self.cache = Cache(self._cache_dir)
            self._cache_closed = False

    def start(self) -> None:
        """
        Start the thermostat loop to continuously monitor and control the fridge.
        """
        self._ensure_cache_open()
        if self._thread is not None and self._thread.is_alive():
            return
        self.stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """
        Stop the thermostat loop and release resources.
        """
        self.stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1)

        self._set_action_state(ActionState.IDLE)
        self.cooler.force_off(time.monotonic())
        self.relay.set_heater_relay(False)
        self.relay.set_vent_fan_relay(False)
        if not self._cache_closed:
            self.cache.close()
            self._cache_closed = True

    @property
    def action_state(self) -> ActionState:
        """Get the current action state of the thermostat."""
        return self._action_state

    @property
    def temperature(self) -> float:
        """Get the last measured temperature from the sensor."""
        return self._temperature

    @property
    def humidity(self) -> float:
        """Get the last measured humidity from the sensor."""
        return self._humidity

    def mains_on(self) -> bool:
        """
        Return whether the cooler mains (Port B Bit 0) is currently HIGH.

        True in every :class:`CoolerState` except ``OFF``. Used by the hourly
        duty-cycle aggregator to compute ``cooler_duty``.
        """
        return self.cooler.state is not CoolerState.OFF

    def compressor_running(self) -> bool:
        """
        Return whether the compressor is actively running.

        True only when the :class:`CoolerController` is in the ``RUNNING``
        state (Bit 0 HIGH and Bit 3 LOW). Used by the hourly duty-cycle
        aggregator to compute ``compressor_duty``.
        """
        return self.cooler.state is CoolerState.RUNNING

    def set_mode(self, mode: ModeState) -> None:
        """
        Set the thermostat mode and refresh thresholds.

        :param mode: New mode
        """
        self._ensure_cache_open()
        mode_changed = mode != self.mode
        self.mode = mode
        self.cache.set("mode", mode)

        if mode == ModeState.COOL_DOWN:
            if self.cache.get("cool_down_start") is None:
                self.cache.set("cool_down_start", time.time())
        else:
            self.cache.delete("cool_down_start")

        self.thresholds = generate_thresholds(
            config=self.config, mode=self.mode, energy_state=self.energy_state
        )
        logger.info(
            "Mode changed to %s (temperature=%.1f°C, humidity=%.1f%%)",
            mode.name,
            self._temperature,
            self._humidity,
        )
        if mode_changed:
            mode_state_changed.send(self, state=mode)

    def get_mode(self) -> ModeState:
        return self.mode

    def set_energy_state(self, energy_state: EnergyState) -> None:
        """Set the energy state and refresh thresholds."""
        self._ensure_cache_open()
        energy_changed = energy_state != self.energy_state
        self.energy_state = energy_state
        self.cache.set("energy_state", energy_state)
        self.thresholds = generate_thresholds(
            config=self.config, mode=self.mode, energy_state=self.energy_state
        )
        logger.info(
            "Energy state changed to %s (temperature=%.1f°C, humidity=%.1f%%)",
            energy_state.name,
            self._temperature,
            self._humidity,
        )
        if energy_changed:
            energy_state_changed.send(self, state=energy_state)

    def get_energy_state(self) -> EnergyState:
        return self.energy_state

    def get_action_state(self) -> ActionState:
        return self.action_state

    def _set_action_state(self, action_state: ActionState) -> None:
        """
        Update and persist the action state; emit a signal on change.

        Note: the actual relay writes are performed elsewhere (cooler is
        driven by :class:`CoolerController`, heater directly). This method
        only tracks the logical action.
        """
        self._ensure_cache_open()
        action_changed = action_state != self._action_state
        if action_changed:
            logger.info(
                "Action state changed to %s (temperature=%.1f°C, humidity=%.1f%%)",
                action_state.name,
                self._temperature,
                self._humidity,
            )
        self._action_state = action_state
        self.cache.set("action_state", action_state)
        if action_changed:
            action_state_changed.send(self, state=action_state)

    def _update_vent_fan(self, now: float) -> None:
        """
        Update the vent fan relay based on mode and humidity, with rate limiting.

        Rate limiting is bypassed in IDLE mode (safety override).
        """
        match self.mode:
            case ModeState.IDLE:
                if self.relay.read_vent_fan_relay():
                    self.relay.set_vent_fan_relay(False)
                    self._last_vent_fan_switch = now
                return

            case ModeState.COOL_DOWN:
                desired = True

            case ModeState.COOL | ModeState.SPECIAL:
                if self.thresholds.fan_on_threshold is None:
                    return
                current = self.relay.read_vent_fan_relay()
                if self._humidity > self.thresholds.fan_on_threshold:
                    desired = True
                elif self._humidity <= self.thresholds.fan_off_threshold:
                    desired = False
                else:
                    desired = current

            case _:
                return

        if self.relay.read_vent_fan_relay() == desired:
            return
        if (
            self._last_vent_fan_switch is not None
            and (now - self._last_vent_fan_switch) < self._min_vent_fan_seconds
        ):
            return
        self.relay.set_vent_fan_relay(desired)
        self._last_vent_fan_switch = now

    def _resolve_cooling_request(self) -> bool:
        """
        Return ``True`` if the cooler should be engaged, applying hysteresis.

        Uses the current ``_cool_requested`` flag to keep the cooler engaged
        between ``cooler_on_threshold`` and ``cooler_off_threshold``.
        """
        on_threshold = self.thresholds.cooler_on_threshold
        off_threshold = self.thresholds.cooler_off_threshold
        if on_threshold is None or off_threshold is None:
            return False
        if self._cool_requested:
            return self._temperature > off_threshold
        return self._temperature >= on_threshold

    def _update_heater(self) -> bool:
        """
        Drive the heater relay purely from freeze_guard, independent of cooling.

        :return: ``True`` if the heater is now on
        """
        on_threshold = self.thresholds.heater_on_threshold
        off_threshold = self.thresholds.heater_off_threshold
        if on_threshold is None or off_threshold is None:
            if self.relay.read_heater_relay():
                self.relay.set_heater_relay(False)
            return False

        currently_on = self.relay.read_heater_relay()
        if currently_on:
            if self._temperature >= off_threshold:
                self.relay.set_heater_relay(False)
                return False
            return True
        if self._temperature < on_threshold:
            self.relay.set_heater_relay(True)
            return True
        return False

    def _check_cool_down_auto_switch(self) -> None:
        """
        Check if COOL_DOWN mode has been active long enough to auto-switch to COOL.
        """
        if self.mode != ModeState.COOL_DOWN:
            return

        cool_down_start = self.cache.get("cool_down_start")
        if cool_down_start is None:
            return

        auto_switch_hours = self.config.getfloat(
            "CoolDownMode", "auto_switch_hours", fallback=DEFAULT_AUTO_SWITCH_HOURS
        )
        elapsed_hours = (time.time() - cool_down_start) / 3600.0
        if elapsed_hours >= auto_switch_hours:
            self.set_mode(ModeState.COOL)

    def _read_sensor(self) -> bool:
        """
        Read temperature and humidity from the sensor.

        :return: ``True`` if the read succeeded, ``False`` on I/O error
        """
        try:
            self._temperature, self._humidity = self.sensor.read_data()
            return True
        except OSError:
            logger.warning("Failed to read sensor data, skipping this cycle")
            return False

    def _control_step(self, now: float) -> None:
        """Run one full control iteration (called from the loop and from tests)."""
        self._update_vent_fan(now)

        if self.mode == ModeState.IDLE:
            self._cool_requested = False
            self.cooler.force_off(now)
            if self.relay.read_heater_relay():
                self.relay.set_heater_relay(False)
            self._set_action_state(ActionState.IDLE)
            return

        # Heater (independent).
        heater_on = self._update_heater()

        # Cooler (via duty-cycle state machine).
        self._cool_requested = self._resolve_cooling_request()
        self.cooler.tick(now, self._cool_requested)

        # Derive the reported action state.
        if self.cooler.cooler_active:
            self._set_action_state(ActionState.COOLING)
        elif heater_on:
            self._set_action_state(ActionState.HEATING)
        else:
            self._set_action_state(ActionState.IDLE)

    def _loop(self) -> None:
        """
        Main loop for the thermostat.

        Runs until ``stop_event`` is set.
        """
        while not self.stop_event.is_set():
            if not self._read_sensor():
                time.sleep(self.refresh_rate)
                continue
            self._control_step(time.monotonic())
            self._check_cool_down_auto_switch()
            time.sleep(self.refresh_rate)
