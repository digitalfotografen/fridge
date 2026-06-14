from configparser import ConfigParser
import threading
import time
from typing import cast
from unittest.mock import patch

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
        self.close_calls = 0

    def get(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)

    def set(self, key: str, value: object) -> None:
        self.values[key] = value

    def delete(self, key: str) -> bool:
        """Remove a key from the cache, return True if it existed."""
        return self.values.pop(key, None) is not None

    def close(self) -> None:
        self.close_calls += 1


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
    config.set("General", "min_relay_interval", "0")

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


def test_stop_closes_cache(
    thermostat_testbed: tuple[Thermostat, _FakeRelay, _FakeSensor, _FakeCache],
) -> None:
    thermostat, _relay, _sensor, cache = thermostat_testbed

    thermostat.stop()

    assert cache.close_calls == 1


def _make_rate_limited_testbed(
    monkeypatch: pytest.MonkeyPatch,
    interval: float = 0.1,
) -> tuple[Thermostat, _FakeRelay, _FakeSensor, _FakeCache]:
    """Create a thermostat testbed with a short relay interval for rate limiting tests."""
    relay = _FakeRelay()
    sensor = _FakeSensor([(20.0, 40.0)])
    cache = _FakeCache()

    monkeypatch.setattr("fridge.thermostat.Sht30", lambda: sensor)
    monkeypatch.setattr("fridge.thermostat.RelayGpio", lambda _gpio: relay)
    monkeypatch.setattr("fridge.thermostat.Cache", lambda _path: cache)

    config = _load_default_config()
    config.set("General", "refresh_rate", "0.01")
    config.set("General", "min_relay_interval", str(interval))

    thermostat = Thermostat(gpio=cast(object, None), config=config)
    return thermostat, relay, sensor, cache


def test_relay_rate_limit_blocks_rapid_switching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relay change is blocked when called again within the minimum interval."""
    thermostat, relay, sensor, _cache = _make_rate_limited_testbed(monkeypatch, interval=10.0)
    thermostat.set_mode(ModeState.COOL)

    # First reading triggers cooling (temp above cooler_on_threshold)
    # Second reading would trigger idle (temp below cooler_off_threshold)
    # But the second change should be blocked by rate limiting
    sensor._readings = [(10.0, 40.0), (3.0, 40.0), (3.0, 40.0)]

    thread = threading.Thread(target=thermostat._loop)
    thread.start()
    time.sleep(0.05)
    thermostat.stop_event.set()
    thread.join(timeout=1)

    cooler_calls = [call for call in relay.calls if call[0] == "cooler"]
    # Only the first switch (cooler on) should happen; second is rate-limited
    assert cooler_calls == [("cooler", True)]


def test_relay_rate_limit_allows_change_after_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relay change is allowed once the minimum interval has elapsed."""
    thermostat, relay, sensor, _cache = _make_rate_limited_testbed(monkeypatch, interval=0.05)
    thermostat.set_mode(ModeState.COOL)

    # Temp high → cooling on, then temp low → cooling off after interval
    sensor._readings = [(10.0, 40.0), (3.0, 40.0), (3.0, 40.0), (3.0, 40.0)]

    thread = threading.Thread(target=thermostat._loop)
    thread.start()
    # Wait long enough for the interval to pass and second change to happen
    time.sleep(0.15)
    thermostat.stop_event.set()
    thread.join(timeout=1)

    cooler_calls = [call for call in relay.calls if call[0] == "cooler"]
    assert cooler_calls == [("cooler", True), ("cooler", False)]


def test_relay_rate_limit_idle_mode_always_forces_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching to IDLE mode always forces relays off, bypassing rate limit."""
    thermostat, relay, sensor, _cache = _make_rate_limited_testbed(monkeypatch, interval=10.0)
    thermostat.set_mode(ModeState.COOL)

    # First reading triggers cooling
    sensor._readings = [(10.0, 40.0), (10.0, 40.0)]

    thread = threading.Thread(target=thermostat._loop)
    thread.start()
    time.sleep(0.03)

    # Switch to IDLE — should force relays off despite rate limit
    thermostat.set_mode(ModeState.IDLE)
    time.sleep(0.03)
    thermostat.stop_event.set()
    thread.join(timeout=1)

    assert relay.cooler is False
    assert relay.heater is False
    assert relay.fan is False


def test_relay_rate_limit_independent_per_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each relay is rate-limited independently."""
    thermostat, relay, sensor, _cache = _make_rate_limited_testbed(monkeypatch, interval=10.0)
    thermostat.set_mode(ModeState.COOL)

    # Temp high → cooling on, then temp very low → should want heating
    # Cooler→on is allowed (first change), heater→on should also be allowed
    # (independent relay, first change for heater)
    sensor._readings = [(10.0, 40.0), (0.0, 40.0), (0.0, 40.0)]

    thread = threading.Thread(target=thermostat._loop)
    thread.start()
    time.sleep(0.05)
    thermostat.stop_event.set()
    thread.join(timeout=1)

    cooler_calls = [call for call in relay.calls if call[0] == "cooler"]
    heater_calls = [call for call in relay.calls if call[0] == "heater"]

    # Cooler was turned on first, then rate-limited from turning off
    assert ("cooler", True) in cooler_calls
    # Heater should be turned on (independent, first change for heater)
    assert ("heater", True) in heater_calls


# ---------------------------------------------------------------------------
# COOL_DOWN → COOL auto-switch tests
# ---------------------------------------------------------------------------


def test_set_mode_cool_down_stores_timestamp(
    thermostat_testbed: tuple[Thermostat, _FakeRelay, _FakeSensor, _FakeCache],
) -> None:
    """Switching to COOL_DOWN stores a timestamp in the cache."""
    thermostat, _relay, _sensor, cache = thermostat_testbed

    with patch("fridge.thermostat.time") as mock_time:
        mock_time.monotonic.return_value = 100.0
        mock_time.time.return_value = 1000.0
        mock_time.sleep = time.sleep
        thermostat.set_mode(ModeState.COOL_DOWN)

    assert cache.values.get("cool_down_start") == 1000.0


def test_set_mode_idle_clears_cool_down_timestamp(
    thermostat_testbed: tuple[Thermostat, _FakeRelay, _FakeSensor, _FakeCache],
) -> None:
    """Switching to IDLE clears the cool_down_start timestamp."""
    thermostat, _relay, _sensor, cache = thermostat_testbed
    cache.values["cool_down_start"] = 1000.0

    thermostat.set_mode(ModeState.IDLE)

    assert cache.values.get("cool_down_start") is None


def test_set_mode_cool_clears_cool_down_timestamp(
    thermostat_testbed: tuple[Thermostat, _FakeRelay, _FakeSensor, _FakeCache],
) -> None:
    """Switching to COOL clears the cool_down_start timestamp."""
    thermostat, _relay, _sensor, cache = thermostat_testbed
    cache.values["cool_down_start"] = 1000.0

    thermostat.set_mode(ModeState.COOL)

    assert cache.values.get("cool_down_start") is None


def test_auto_switch_cool_down_to_cool_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thermostat auto-switches from COOL_DOWN to COOL after configured hours."""
    relay = _FakeRelay()
    sensor = _FakeSensor([(12.0, 40.0)])
    cache = _FakeCache()

    monkeypatch.setattr("fridge.thermostat.Sht30", lambda: sensor)
    monkeypatch.setattr("fridge.thermostat.RelayGpio", lambda _gpio: relay)
    monkeypatch.setattr("fridge.thermostat.Cache", lambda _path: cache)

    config = _load_default_config()
    config.set("General", "refresh_rate", "0.01")
    config.set("General", "min_relay_interval", "0")
    config.set("CoolDownMode", "auto_switch_hours", "10")

    thermostat = Thermostat(gpio=cast(object, None), config=config)

    # Simulate setting COOL_DOWN 11 hours ago
    now = time.time()
    cache.values["cool_down_start"] = now - (11 * 3600)
    thermostat.set_mode(ModeState.COOL_DOWN)
    # Restore the old timestamp (set_mode would overwrite since cache had no value initially)
    cache.values["cool_down_start"] = now - (11 * 3600)

    thread = threading.Thread(target=thermostat._loop)
    thread.start()
    time.sleep(0.05)
    thermostat.stop_event.set()
    thread.join(timeout=1)

    assert thermostat.mode == ModeState.COOL


def test_auto_switch_does_not_trigger_before_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thermostat stays in COOL_DOWN before the configured timeout."""
    relay = _FakeRelay()
    sensor = _FakeSensor([(12.0, 40.0)])
    cache = _FakeCache()

    monkeypatch.setattr("fridge.thermostat.Sht30", lambda: sensor)
    monkeypatch.setattr("fridge.thermostat.RelayGpio", lambda _gpio: relay)
    monkeypatch.setattr("fridge.thermostat.Cache", lambda _path: cache)

    config = _load_default_config()
    config.set("General", "refresh_rate", "0.01")
    config.set("General", "min_relay_interval", "0")
    # Very long auto-switch time
    config.set("CoolDownMode", "auto_switch_hours", "100")

    thermostat = Thermostat(gpio=cast(object, None), config=config)
    thermostat.set_mode(ModeState.COOL_DOWN)

    thread = threading.Thread(target=thermostat._loop)
    thread.start()
    time.sleep(0.05)
    thermostat.stop_event.set()
    thread.join(timeout=1)

    assert thermostat.mode == ModeState.COOL_DOWN


# ---------------------------------------------------------------------------
# State persistence tests
# ---------------------------------------------------------------------------


def test_thermostat_restores_action_state_from_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thermostat should restore action_state from cache on init."""
    relay = _FakeRelay()
    sensor = _FakeSensor([(20.0, 40.0)])
    cache = _FakeCache()
    cache.values["action_state"] = ActionState.COOLING
    cache.values["mode"] = ModeState.COOL

    monkeypatch.setattr("fridge.thermostat.Sht30", lambda: sensor)
    monkeypatch.setattr("fridge.thermostat.RelayGpio", lambda _gpio: relay)
    monkeypatch.setattr("fridge.thermostat.Cache", lambda _path: cache)

    config = _load_default_config()
    thermostat = Thermostat(gpio=cast(object, None), config=config)

    assert thermostat.action_state == ActionState.COOLING
    assert thermostat.mode == ModeState.COOL


def test_thermostat_defaults_action_state_to_idle_when_not_cached(
    thermostat_testbed: tuple[Thermostat, _FakeRelay, _FakeSensor, _FakeCache],
) -> None:
    """Thermostat should default to IDLE when action_state is not in cache."""
    thermostat, _relay, _sensor, _cache = thermostat_testbed
    assert thermostat.action_state == ActionState.IDLE


# ---------------------------------------------------------------------------
# Sensor error handling tests
# ---------------------------------------------------------------------------


def test_thermostat_loop_survives_sensor_read_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thermostat loop should not crash when the sensor raises OSError."""
    relay = _FakeRelay()
    cache = _FakeCache()

    call_count = 0

    class _FlakySensor:
        def read_data(self) -> tuple[float, float]:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise OSError("Input/output error")
            return 20.0, 40.0

    monkeypatch.setattr("fridge.thermostat.Sht30", lambda: _FlakySensor())
    monkeypatch.setattr("fridge.thermostat.RelayGpio", lambda _gpio: relay)
    monkeypatch.setattr("fridge.thermostat.Cache", lambda _path: cache)

    config = _load_default_config()
    config.set("General", "refresh_rate", "0.01")
    config.set("General", "min_relay_interval", "0")

    thermostat = Thermostat(gpio=cast(object, None), config=config)
    thermostat.set_mode(ModeState.COOL)

    thread = threading.Thread(target=thermostat._loop)
    thread.start()
    time.sleep(0.1)
    thermostat.stop_event.set()
    thread.join(timeout=1)

    assert thread.is_alive() is False
    assert call_count >= 3
