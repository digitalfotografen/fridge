from configparser import ConfigParser
from diskcache import Cache
from dataclasses import dataclass
import math
import threading
import time

from fridge import ModeState, ActionState, EnergyState
from fridge import cache_path
from fridge import Sht30, RelayGpio, I2cGpio


DEFAULT_COOL_DOWN_TARGET_TEMPERATURE = 12.0
DEFAULT_COOL_DOWN_HYSTERESIS = 1.0
DEFAULT_COOL_TARGET_TEMPERATURE = 5.0
DEFAULT_COOL_HYSTERESIS = 1.0
DEFAULT_FREEZE_GUARD = 0.0
DEFAULT_TARGET_HUMIDITY = 50.0
DEFAULT_HUMIDITY_HYSTERESIS = 5.0
DEFAULT_LOW_PRICE_COOL_ADJ = -2.0
DEFAULT_LOW_PRICE_HEAT_ADJ = 1.0
DEFAULT_HIGH_PRICE_COOL_ADJ = 2.0
DEFAULT_HIGH_PRICE_HEAT_ADJ = -2.0


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


def generate_thresholds(
    config: ConfigParser,
    mode: ModeState,
    energy_state: EnergyState,
) -> ThermostatThresholds:
    """
    Generate thresholds based on the mode, energy state and configuration.

    The returned thresholds are interpreted by the thermostat loop as follows:

    - ``None`` means that the threshold is inactive for the current mode.
    - ``COOL_DOWN`` and ``COOL`` are calculated from configured target and hysteresis.
    - Energy-state adjustments are applied separately for heating and cooling targets.
    - ``COOL`` heater thresholds are clamped by ``freeze_guard``.

    :param config: Application configuration
    :param mode: Operation mode state
    :param energy_state: Current energy pricing state
    :return: Thermostat thresholds
    """
    if energy_state == EnergyState.LOW_PRICE:
        cool_adjustment = config.getfloat(
            "Energy", "low_price_cool_adj", fallback=DEFAULT_LOW_PRICE_COOL_ADJ
        )
        heat_adjustment = config.getfloat(
            "Energy", "low_price_heat_adj", fallback=DEFAULT_LOW_PRICE_HEAT_ADJ
        )
    elif energy_state == EnergyState.HIGH_PRICE:
        cool_adjustment = config.getfloat(
            "Energy", "high_price_cool_adj", fallback=DEFAULT_HIGH_PRICE_COOL_ADJ
        )
        heat_adjustment = config.getfloat(
            "Energy", "high_price_heat_adj", fallback=DEFAULT_HIGH_PRICE_HEAT_ADJ
        )
    else:
        cool_adjustment = 0.0
        heat_adjustment = 0.0

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

            heater_target = target_temperature + heat_adjustment
            cooler_target = target_temperature + cool_adjustment

            return ThermostatThresholds(
                target_temperature=target_temperature,
                target_humidity=None,
                heater_on_threshold=heater_target - hysteresis,
                heater_off_threshold=heater_target + hysteresis,
                cooler_on_threshold=cooler_target + hysteresis,
                cooler_off_threshold=cooler_target - hysteresis,
                fan_on_threshold=1.0,
                fan_off_threshold=0.0,
            )

        case ModeState.COOL:
            target_temperature = config.getfloat(
                "CoolMode",
                "target_temperature",
                fallback=DEFAULT_COOL_TARGET_TEMPERATURE,
            )
            hysteresis = config.getfloat(
                "CoolMode",
                "hysteresis",
                fallback=DEFAULT_COOL_HYSTERESIS,
            )
            freeze_guard = config.getfloat(
                "CoolMode",
                "freeze_guard",
                fallback=DEFAULT_FREEZE_GUARD,
            )
            target_humidity = config.getfloat(
                "CoolMode",
                "target_humidity",
                fallback=DEFAULT_TARGET_HUMIDITY,
            )
            humidity_hysteresis = config.getfloat(
                "CoolMode",
                "humidity_hysteresis",
                fallback=DEFAULT_HUMIDITY_HYSTERESIS,
            )

            heater_target = target_temperature + heat_adjustment
            cooler_target = target_temperature + cool_adjustment

            return ThermostatThresholds(
                target_temperature=target_temperature,
                target_humidity=target_humidity,
                heater_on_threshold=max(freeze_guard, heater_target - hysteresis),
                heater_off_threshold=max(freeze_guard, heater_target + hysteresis),
                cooler_on_threshold=cooler_target + hysteresis,
                cooler_off_threshold=cooler_target - hysteresis,
                fan_on_threshold=target_humidity + humidity_hysteresis,
                fan_off_threshold=target_humidity - humidity_hysteresis,
            )


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
        self.cache = Cache(cache_path)
        self.mode = self.cache.get("mode", ModeState.IDLE)
        self.energy_state = self.cache.get("energy_state", EnergyState.NORMAL_PRICE)
        self.thresholds = generate_thresholds(
            config=config, mode=self.mode, energy_state=self.energy_state
        )
        self._temperature = math.nan
        self._humidity = math.nan
        self._action_state = ActionState.IDLE
        self.stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.refresh_rate = self.config.getfloat("General", "refresh_rate", fallback=1.0)

    def start(self) -> None:
        """
        Start the thermostat loop to continuously monitor and control the fridge.
        """
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

        self._set_relays(cooler=False, heater=False, fan=False)
        self._set_action_state(ActionState.IDLE)

    @property
    def action_state(self) -> ActionState:
        """
        Get the current action state of the thermostat.
        :return: Current action state
        """
        return self._action_state

    @property
    def temperature(self) -> float:
        """
        Get the last measured temperature from the sensor.
        :return: Temperature in Celsius
        """
        return self._temperature

    @property
    def humidity(self) -> float:
        """
        Get the last measured humidity from the sensor.
        :return: Humidity in percentage
        """
        return self._humidity

    def set_mode(self, mode: ModeState) -> None:
        """
        Set the thermostat mode.
        Updates the thresholds based on the new mode.
        :param mode: New mode
        :return:
        """
        self.mode = mode
        self.cache.set("mode", mode)
        self.thresholds = generate_thresholds(
            config=self.config, mode=self.mode, energy_state=self.energy_state
        )

    def get_mode(self) -> ModeState:
        """
        Get the current mode of the thermostat.
        :return: Current mode
        """
        return self.mode

    def set_energy_state(self, energy_state: EnergyState) -> None:
        """
        Set the energy state of the thermostat.
        Updates the thresholds based on the new energy state.
        :param energy_state: New energy state
        :return:
        """
        self.energy_state = energy_state
        self.cache.set("energy_state", energy_state)
        self.thresholds = generate_thresholds(
            config=self.config, mode=self.mode, energy_state=self.energy_state
        )

    def get_energy_state(self) -> EnergyState:
        """
        Get the current energy state of the thermostat.
        :return: Current energy state
        """
        return self.energy_state

    def get_action_state(self) -> ActionState:
        """
        Get the current action state of the thermostat.
        :return: Current action state
        """
        return self.action_state

    def _set_action_state(self, action_state: ActionState) -> None:
        """
        Update the action state and persist it when changed.
        :param action_state: New action state
        :return:
        """
        if action_state == self._action_state:
            return
        self._action_state = action_state
        self.cache.set("action_state", action_state)

    def _set_relays(self, cooler: bool, heater: bool, fan: bool) -> None:
        """
        Set relay states, avoiding redundant writes.
        :param cooler: Cooler relay state
        :param heater: Heater relay state
        :param fan: Ventilation fan relay state
        :return:
        """
        if self.relay.read_cooler_relay() != cooler:
            self.relay.set_cooler_relay(cooler)
        if self.relay.read_heater_relay() != heater:
            self.relay.set_heater_relay(heater)
        if self.relay.read_vent_fan_relay() != fan:
            self.relay.set_vent_fan_relay(fan)

    def _read_sensor(self) -> None:
        """
        Read temperature and humidity from the sensor.
        :return:
        """
        self._temperature, self._humidity = self.sensor.read_data()

    def _loop(self) -> None:
        """
        Main loop for the thermostat.
        This method reads the temperature and humidity from the sensor,
        and updates the relay state based on the current mode and energy state.

        The loop runs until the stop event is set.

        This method should be run in a separate thread.
        :return:
        """
        while not self.stop_event.is_set():
            self._read_sensor()

            if self.mode == ModeState.IDLE:
                self._set_action_state(ActionState.IDLE)
                self._set_relays(cooler=False, heater=False, fan=False)
                time.sleep(self.refresh_rate)
                continue

            next_action = self._action_state
            if (
                self.thresholds.cooler_on_threshold is not None
                and self._temperature >= self.thresholds.cooler_on_threshold
            ):
                next_action = ActionState.COOLING
            elif (
                self.thresholds.heater_on_threshold is not None
                and self._temperature <= self.thresholds.heater_on_threshold
            ):
                next_action = ActionState.HEATING
            elif (
                self._action_state == ActionState.COOLING
                and self.thresholds.cooler_off_threshold is not None
                and self._temperature <= self.thresholds.cooler_off_threshold
            ):
                next_action = ActionState.IDLE
            elif (
                self._action_state == ActionState.HEATING
                and self.thresholds.heater_off_threshold is not None
                and self._temperature >= self.thresholds.heater_off_threshold
            ):
                next_action = ActionState.IDLE

            if next_action != self._action_state:
                self._set_action_state(next_action)
                self._set_relays(
                    cooler=next_action == ActionState.COOLING,
                    heater=next_action == ActionState.HEATING,
                    fan=self.mode == ModeState.COOL_DOWN,
                )

            if self.mode == ModeState.COOL:
                fan_state = self.relay.read_vent_fan_relay()
                if (
                    self.thresholds.fan_on_threshold is not None
                    and self._humidity >= self.thresholds.fan_on_threshold
                    and not fan_state
                ):
                    self.relay.set_vent_fan_relay(True)
                elif (
                    self.thresholds.fan_off_threshold is not None
                    and self._humidity <= self.thresholds.fan_off_threshold
                    and fan_state
                ):
                    self.relay.set_vent_fan_relay(False)

            time.sleep(self.refresh_rate)
