from configparser import ConfigParser
import threading
import time
from typing import cast

import pytest

from fridge import ActionState, EnergyState, ModeState
from fridge.thermostat import Thermostat, generate_thresholds


def _load_default_config() -> ConfigParser:
    config = ConfigParser()
    config.read("config_default.ini")
    return config


def test_generate_thresholds_idle_returns_none_thresholds() -> None:
    config = _load_default_config()

    thresholds = generate_thresholds(config, ModeState.IDLE, EnergyState.NORMAL_PRICE)

    assert thresholds.target_temperature is None
    assert thresholds.target_humidity is None
    assert thresholds.heater_on_threshold is None
    assert thresholds.heater_off_threshold is None
    assert thresholds.cooler_on_threshold is None
    assert thresholds.cooler_off_threshold is None
    assert thresholds.fan_on_threshold is None
    assert thresholds.fan_off_threshold is None


def test_generate_thresholds_cool_down_normal_price() -> None:
    config = _load_default_config()

    thresholds = generate_thresholds(config, ModeState.COOL_DOWN, EnergyState.NORMAL_PRICE)

    assert thresholds.target_temperature == 12.0
    assert thresholds.target_humidity is None
    assert thresholds.heater_on_threshold == 10.5
    assert thresholds.heater_off_threshold == 13.5
    assert thresholds.cooler_on_threshold == 13.5
    assert thresholds.cooler_off_threshold == 10.5
    assert thresholds.fan_on_threshold == 1.0
    assert thresholds.fan_off_threshold == 0.0


def test_generate_thresholds_cool_down_low_price_adjusts_targets() -> None:
    config = _load_default_config()

    thresholds = generate_thresholds(config, ModeState.COOL_DOWN, EnergyState.LOW_PRICE)

    assert thresholds.target_temperature == 12.0
    assert thresholds.heater_on_threshold == 11.5
    assert thresholds.heater_off_threshold == 14.5
    assert thresholds.cooler_on_threshold == 11.5
    assert thresholds.cooler_off_threshold == 8.5


def test_generate_thresholds_cool_mode_high_price_uses_freeze_guard() -> None:
    config = _load_default_config()

    thresholds = generate_thresholds(config, ModeState.COOL, EnergyState.HIGH_PRICE)

    assert thresholds.target_temperature == 5.0
    assert thresholds.target_humidity == 55.0
    assert thresholds.heater_on_threshold == 1.5
    assert thresholds.heater_off_threshold == 4.5
    assert thresholds.cooler_on_threshold == 8.5
    assert thresholds.cooler_off_threshold == 5.5
    assert thresholds.fan_on_threshold == 60.0
    assert thresholds.fan_off_threshold == 50.0


def test_generate_thresholds_uses_readme_defaults_as_fallbacks_for_cool_down() -> None:
    config = ConfigParser()

    thresholds = generate_thresholds(config, ModeState.COOL_DOWN, EnergyState.NORMAL_PRICE)

    assert thresholds.target_temperature == 12.0
    assert thresholds.target_humidity is None
    assert thresholds.heater_on_threshold == 10.5
    assert thresholds.heater_off_threshold == 13.5
    assert thresholds.cooler_on_threshold == 13.5
    assert thresholds.cooler_off_threshold == 10.5
    assert thresholds.fan_on_threshold == 1.0
    assert thresholds.fan_off_threshold == 0.0


def test_generate_thresholds_uses_readme_defaults_as_fallbacks_for_cool_mode_high_price() -> None:
    config = ConfigParser()

    thresholds = generate_thresholds(config, ModeState.COOL, EnergyState.HIGH_PRICE)

    assert thresholds.target_temperature == 5.0
    assert thresholds.target_humidity == 55.0
    assert thresholds.heater_on_threshold == 1.5
    assert thresholds.heater_off_threshold == 4.5
    assert thresholds.cooler_on_threshold == 8.5
    assert thresholds.cooler_off_threshold == 5.5
    assert thresholds.fan_on_threshold == 60.0
    assert thresholds.fan_off_threshold == 50.0


class _FakeSensor:
    def __init__(self, readings: list[tuple[float, float]]) -> None:
        self._readings = readings
        self._index = 0

    def read_data(self) -> tuple[float, float]:
        value = self._readings[min(self._index, len(self._readings) - 1)]
        self._index += 1
        return value


class _FakeRelay:
    def __init__(self) -> None:
        self.cooler = False
        self.heater = False
        self.fan = False
        self.calls: list[tuple[str, bool]] = []

    def set_cooler_relay(self, state: bool) -> None:
        self.cooler = state
        self.calls.append(("cooler", state))

    def read_cooler_relay(self) -> bool:
        return self.cooler

    def set_heater_relay(self, state: bool) -> None:
        self.heater = state
        self.calls.append(("heater", state))

    def read_heater_relay(self) -> bool:
        return self.heater

    def set_vent_fan_relay(self, state: bool) -> None:
        self.fan = state
        self.calls.append(("fan", state))

    def read_vent_fan_relay(self) -> bool:
        return self.fan


class _FakeCache:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def get(self, key: str, default: object) -> object:
        return self.values.get(key, default)

    def set(self, key: str, value: object) -> None:
        self.values[key] = value


@pytest.fixture
def thermostat_testbed(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Thermostat, _FakeRelay, _FakeSensor, _FakeCache]:
    relay = _FakeRelay()
    sensor = _FakeSensor([(20.0, 40.0)])
    cache = _FakeCache()

    monkeypatch.setattr("fridge.thermostat.Sht30", lambda: sensor)
    monkeypatch.setattr("fridge.thermostat.RelayGpio", lambda _gpio: relay)
    monkeypatch.setattr("fridge.thermostat.Cache", lambda _path: cache)

    config = _load_default_config()
    config.set("General", "refresh_rate", "0.01")

    thermostat = Thermostat(gpio=cast(object, None), config=config)
    return thermostat, relay, sensor, cache


def test_thermostat_loop_idle_turns_all_relays_off_and_sets_action_idle(
    thermostat_testbed: tuple[Thermostat, _FakeRelay, _FakeSensor, _FakeCache],
) -> None:
    thermostat, relay, sensor, _cache = thermostat_testbed
    sensor._readings = [(10.0, 80.0)]
    thermostat.set_mode(ModeState.IDLE)
    relay.cooler = True
    relay.heater = True
    relay.fan = True

    thread = threading.Thread(target=thermostat._loop)
    thread.start()
    time.sleep(0.03)
    thermostat.stop_event.set()
    thread.join(timeout=1)

    assert thread.is_alive() is False
    assert relay.cooler is False
    assert relay.heater is False
    assert relay.fan is False
    assert thermostat.action_state == ActionState.IDLE


def test_thermostat_loop_changes_relays_only_when_action_state_changes(
    thermostat_testbed: tuple[Thermostat, _FakeRelay, _FakeSensor, _FakeCache],
) -> None:
    thermostat, relay, sensor, _cache = thermostat_testbed
    thermostat.set_mode(ModeState.COOL)
    sensor._readings = [(10.0, 40.0), (10.0, 40.0), (3.0, 40.0), (3.0, 40.0)]

    thread = threading.Thread(target=thermostat._loop)
    thread.start()
    time.sleep(0.06)
    thermostat.stop_event.set()
    thread.join(timeout=1)

    cooler_calls = [call for call in relay.calls if call[0] == "cooler"]
    assert cooler_calls == [("cooler", True), ("cooler", False)]


def test_start_and_stop_are_idempotent(
    thermostat_testbed: tuple[Thermostat, _FakeRelay, _FakeSensor, _FakeCache],
) -> None:
    thermostat, relay, _sensor, _cache = thermostat_testbed

    thermostat.start()
    first_thread = thermostat._thread
    thermostat.start()

    assert thermostat._thread is first_thread

    thermostat.stop()
    thermostat.stop()

    assert thermostat.stop_event.is_set()
    assert relay.cooler is False
    assert relay.heater is False
    assert relay.fan is False
