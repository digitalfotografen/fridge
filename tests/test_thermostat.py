from configparser import ConfigParser
import logging
import math
import threading
import time
from typing import cast
from unittest.mock import patch

import pytest

from fridge import ActionState, EnergyState, ModeState
from fridge.thermostat import CoolerController, Thermostat, generate_thresholds
from fridge.thermostat import CoolerState


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
    # Heater uses freeze_guard (9.0) and hysteresis (1.5)
    assert thresholds.heater_on_threshold == 9.0
    assert thresholds.heater_off_threshold == 10.5
    # Cooler uses target +/- hysteresis/2
    assert thresholds.cooler_on_threshold == 12.75
    assert thresholds.cooler_off_threshold == 11.25
    assert thresholds.fan_on_threshold == 1.0
    assert thresholds.fan_off_threshold == 0.0


def test_generate_thresholds_cool_down_low_price_adjusts_targets() -> None:
    config = _load_default_config()

    thresholds = generate_thresholds(config, ModeState.COOL_DOWN, EnergyState.LOW_PRICE)

    # target lowered by 2, freeze_guard raised by 1
    assert thresholds.target_temperature == 10.0
    assert thresholds.heater_on_threshold == 10.0
    assert thresholds.heater_off_threshold == 11.5
    assert thresholds.cooler_on_threshold == 10.75
    assert thresholds.cooler_off_threshold == 9.25


def test_generate_thresholds_cool_mode_high_price_uses_freeze_guard() -> None:
    config = _load_default_config()

    thresholds = generate_thresholds(config, ModeState.COOL, EnergyState.HIGH_PRICE)

    # target 5 + 2, freeze_guard 2 - 2
    assert thresholds.target_temperature == 7.0
    assert thresholds.target_humidity == 55.0
    assert thresholds.heater_on_threshold == 0.0
    assert thresholds.heater_off_threshold == 1.5
    assert thresholds.cooler_on_threshold == 7.75
    assert thresholds.cooler_off_threshold == 6.25
    assert thresholds.fan_on_threshold == 60.0
    assert thresholds.fan_off_threshold == 50.0


def test_generate_thresholds_uses_readme_defaults_as_fallbacks_for_cool_down() -> None:
    config = ConfigParser()

    thresholds = generate_thresholds(config, ModeState.COOL_DOWN, EnergyState.NORMAL_PRICE)

    assert thresholds.target_temperature == 12.0
    assert thresholds.target_humidity is None
    assert thresholds.heater_on_threshold == 9.0
    assert thresholds.heater_off_threshold == 10.5
    assert thresholds.cooler_on_threshold == 12.75
    assert thresholds.cooler_off_threshold == 11.25
    assert thresholds.fan_on_threshold == 1.0
    assert thresholds.fan_off_threshold == 0.0


def test_generate_thresholds_uses_readme_defaults_as_fallbacks_for_cool_mode_high_price() -> None:
    config = ConfigParser()

    thresholds = generate_thresholds(config, ModeState.COOL, EnergyState.HIGH_PRICE)

    assert thresholds.target_temperature == 7.0
    assert thresholds.target_humidity == 55.0
    assert thresholds.heater_on_threshold == 0.0
    assert thresholds.heater_off_threshold == 1.5
    assert thresholds.cooler_on_threshold == 7.75
    assert thresholds.cooler_off_threshold == 6.25
    assert thresholds.fan_on_threshold == 60.0
    assert thresholds.fan_off_threshold == 50.0


def test_generate_thresholds_special_mode_normal_price() -> None:
    config = _load_default_config()

    # config_default.ini SpecialMode: target 14.0, hysteresis 1.5, freeze_guard 2.0
    thresholds = generate_thresholds(config, ModeState.SPECIAL, EnergyState.NORMAL_PRICE)

    assert thresholds.target_temperature == 14.0
    assert thresholds.target_humidity == 55.0
    assert thresholds.heater_on_threshold == 2.0
    assert thresholds.heater_off_threshold == 3.5
    assert thresholds.cooler_on_threshold == 14.75
    assert thresholds.cooler_off_threshold == 13.25
    assert thresholds.fan_on_threshold == 60.0
    assert thresholds.fan_off_threshold == 50.0


def test_generate_thresholds_special_mode_reads_from_special_section() -> None:
    """SPECIAL mode must read its own [SpecialMode] section, independent of [CoolMode]."""
    config = ConfigParser()
    config.read_string(
        """
        [CoolMode]
        target_temperature = 5.0
        hysteresis = 1.5
        freeze_guard = 0.0
        target_humidity = 55.0
        humidity_hysteresis = 5.0

        [SpecialMode]
        target_temperature = 2.0
        hysteresis = 0.5
        freeze_guard = 0.0
        target_humidity = 70.0
        humidity_hysteresis = 3.0
        """
    )

    thresholds = generate_thresholds(config, ModeState.SPECIAL, EnergyState.NORMAL_PRICE)

    assert thresholds.target_temperature == 2.0
    assert thresholds.target_humidity == 70.0
    assert thresholds.heater_on_threshold == 0.0
    assert thresholds.heater_off_threshold == 0.5
    assert thresholds.cooler_on_threshold == 2.25
    assert thresholds.cooler_off_threshold == 1.75
    assert thresholds.fan_on_threshold == 73.0
    assert thresholds.fan_off_threshold == 67.0


def test_generate_thresholds_special_mode_high_price_uses_freeze_guard() -> None:
    config = _load_default_config()

    thresholds = generate_thresholds(config, ModeState.SPECIAL, EnergyState.HIGH_PRICE)

    # target 14 + 2, freeze_guard 2 - 2
    assert thresholds.target_temperature == 16.0
    assert thresholds.target_humidity == 55.0
    assert thresholds.heater_on_threshold == 0.0
    assert thresholds.heater_off_threshold == 1.5
    assert thresholds.cooler_on_threshold == 16.75
    assert thresholds.cooler_off_threshold == 15.25


def test_generate_thresholds_uses_defaults_as_fallbacks_for_special_mode() -> None:
    config = ConfigParser()

    # Defaults from DEFAULT_SPECIAL_* in thermostat.py: target 5, hyst 1.5, freeze_guard 2.0
    thresholds = generate_thresholds(config, ModeState.SPECIAL, EnergyState.NORMAL_PRICE)

    assert thresholds.target_temperature == 5.0
    assert thresholds.target_humidity == 55.0
    assert thresholds.heater_on_threshold == 2.0
    assert thresholds.heater_off_threshold == 3.5
    assert thresholds.cooler_on_threshold == 5.75
    assert thresholds.cooler_off_threshold == 4.25
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
        self.compressor_pause = False
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

    def set_compressor_pause_relay(self, state: bool) -> None:
        self.compressor_pause = state
        self.calls.append(("compressor_pause", state))

    def read_compressor_pause_relay(self) -> bool:
        return self.compressor_pause


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
    # Disable vent-fan and cooler-duty-cycle rate limits so a single control
    # step reflects the intended relay state.
    config.set("General", "min_vent_fan", "0")
    config.set("General", "max_cooler_time", "0")
    config.set("General", "cooler_pause_time", "0")
    config.set("General", "cooler_soft_start_time", "0")

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


def test_thermostat_loop_cool_mode_engages_and_releases_cooler(
    thermostat_testbed: tuple[Thermostat, _FakeRelay, _FakeSensor, _FakeCache],
) -> None:
    """In COOL mode a hot reading engages the cooler; a cold reading releases it."""
    thermostat, relay, sensor, _cache = thermostat_testbed
    thermostat.set_mode(ModeState.COOL)

    # Hot temperature → cooler should be engaged.
    thermostat._temperature = 10.0
    thermostat._humidity = 40.0
    thermostat._control_step(now=0.0)
    assert relay.cooler is True
    assert thermostat.action_state == ActionState.COOLING

    # Cold temperature → cooler should be released (fully off since all
    # duty-cycle timings are 0 in the testbed).
    thermostat._temperature = 3.0
    thermostat._control_step(now=1.0)
    thermostat._control_step(now=2.0)
    assert relay.cooler is False
    assert thermostat.action_state == ActionState.IDLE


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


# ---------------------------------------------------------------------------
# Cooler duty-cycle state-machine tests
# ---------------------------------------------------------------------------


def _make_duty_cycle_testbed(
    monkeypatch: pytest.MonkeyPatch,
    max_cooler_minutes: float = 30.0,
    cooler_pause_minutes: float = 15.0,
    cooler_soft_start_minutes: float = 2.0,
    min_vent_fan_minutes: float = 0.0,
) -> tuple[Thermostat, _FakeRelay, _FakeSensor, _FakeCache]:
    """Create a thermostat testbed with configurable duty-cycle timings."""
    relay = _FakeRelay()
    sensor = _FakeSensor([(20.0, 40.0)])
    cache = _FakeCache()

    monkeypatch.setattr("fridge.thermostat.Sht30", lambda: sensor)
    monkeypatch.setattr("fridge.thermostat.RelayGpio", lambda _gpio: relay)
    monkeypatch.setattr("fridge.thermostat.Cache", lambda _path: cache)

    config = _load_default_config()
    config.set("General", "refresh_rate", "60")
    config.set("General", "min_vent_fan", str(min_vent_fan_minutes))
    config.set("General", "max_cooler_time", str(max_cooler_minutes))
    config.set("General", "cooler_pause_time", str(cooler_pause_minutes))
    config.set("General", "cooler_soft_start_time", str(cooler_soft_start_minutes))

    thermostat = Thermostat(gpio=cast(object, None), config=config)
    return thermostat, relay, sensor, cache


def test_cooler_on_sequence_bit3_high_then_bit0_then_soft_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switch cooler on: Bit3 HIGH, Bit0 HIGH, wait soft-start, Bit3 LOW."""
    thermostat, relay, _sensor, _cache = _make_duty_cycle_testbed(
        monkeypatch, cooler_soft_start_minutes=2.0
    )
    thermostat.set_mode(ModeState.COOL)
    thermostat._temperature = 10.0
    thermostat._humidity = 40.0

    # Tick 1: cool requested, both bits should go high, state SOFT_START.
    thermostat._control_step(now=0.0)
    assert relay.compressor_pause is True
    assert relay.cooler is True
    # The order matters: Bit 3 must be raised before (or with) Bit 0.
    bit3_idx = relay.calls.index(("compressor_pause", True))
    bit0_idx = relay.calls.index(("cooler", True))
    assert bit3_idx < bit0_idx

    # Before soft-start elapses, Bit 3 must remain HIGH.
    thermostat._control_step(now=60.0)  # 1 min
    assert relay.compressor_pause is True

    # After soft-start elapses, Bit 3 goes LOW (compressor released).
    thermostat._control_step(now=120.0)  # 2 min
    assert relay.compressor_pause is False
    assert relay.cooler is True


def test_cooler_duty_cycle_pauses_after_max_run_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After max_cooler_time running, compressor pauses; fan stays on."""
    thermostat, relay, _sensor, _cache = _make_duty_cycle_testbed(
        monkeypatch,
        max_cooler_minutes=5.0,
        cooler_pause_minutes=3.0,
        cooler_soft_start_minutes=1.0,
    )
    thermostat.set_mode(ModeState.COOL)
    thermostat._temperature = 10.0
    thermostat._humidity = 40.0

    # t=0 SOFT_START -> t=60 RUNNING
    thermostat._control_step(now=0.0)
    thermostat._control_step(now=60.0)
    assert relay.compressor_pause is False

    # After max_run_time (5 min) of RUNNING (i.e. now=60+300=360), enter PAUSED.
    thermostat._control_step(now=360.0)
    assert relay.compressor_pause is True
    assert relay.cooler is True  # fan keeps running

    # After pause_time (3 min), compressor should resume.
    thermostat._control_step(now=360.0 + 180.0)
    assert relay.compressor_pause is False
    assert relay.cooler is True


def test_cooler_off_sequence_pauses_before_dropping_bit0(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off-threshold reached while RUNNING: pause first, then drop Bit 0."""
    thermostat, relay, _sensor, _cache = _make_duty_cycle_testbed(
        monkeypatch,
        max_cooler_minutes=60.0,
        cooler_pause_minutes=3.0,
        cooler_soft_start_minutes=1.0,
    )
    thermostat.set_mode(ModeState.COOL)
    thermostat._humidity = 40.0

    # Engage: 0 -> SOFT_START, 60 -> RUNNING
    thermostat._temperature = 10.0
    thermostat._control_step(now=0.0)
    thermostat._control_step(now=60.0)
    assert relay.compressor_pause is False

    # Temperature drops below off-threshold while RUNNING → SPIN_DOWN.
    thermostat._temperature = 3.0
    thermostat._control_step(now=120.0)
    assert relay.compressor_pause is True
    assert relay.cooler is True  # fan keeps running during defrost pause

    # Before the pause elapses, Bit 0 must stay HIGH.
    thermostat._control_step(now=120.0 + 60.0)
    assert relay.cooler is True

    # After pause_time (3 min), Bit 0 drops LOW; Bit 3 stays HIGH per spec.
    thermostat._control_step(now=120.0 + 180.0)
    assert relay.cooler is False
    assert relay.compressor_pause is True


def test_cooler_off_during_pause_reuses_remaining_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off during duty-cycle pause: reuse remaining pause, no additional wait."""
    thermostat, relay, _sensor, _cache = _make_duty_cycle_testbed(
        monkeypatch,
        max_cooler_minutes=5.0,
        cooler_pause_minutes=3.0,
        cooler_soft_start_minutes=1.0,
    )
    thermostat.set_mode(ModeState.COOL)
    thermostat._humidity = 40.0

    # Get into PAUSED: 0 SOFT_START, 60 RUNNING, 360 PAUSED.
    thermostat._temperature = 10.0
    thermostat._control_step(now=0.0)
    thermostat._control_step(now=60.0)
    thermostat._control_step(now=360.0)
    assert relay.compressor_pause is True
    assert relay.cooler is True

    # 1 minute into the 3-minute pause the temperature drops below the
    # off-threshold. The spin-down should reuse the pause window starting at
    # 360, so cooler drops off at now=360+180=540 without any extra pause.
    thermostat._temperature = 3.0
    thermostat._control_step(now=360.0 + 60.0)  # 1 min into pause
    assert relay.cooler is True  # still spinning down

    thermostat._control_step(now=360.0 + 180.0)  # end of original pause window
    assert relay.cooler is False


def test_idle_mode_forces_cooler_off_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching to IDLE mid-cycle forces both cooler relays off immediately."""
    thermostat, relay, _sensor, _cache = _make_duty_cycle_testbed(
        monkeypatch,
        max_cooler_minutes=60.0,
        cooler_pause_minutes=15.0,
        cooler_soft_start_minutes=2.0,
    )
    thermostat.set_mode(ModeState.COOL)
    thermostat._temperature = 10.0
    thermostat._humidity = 40.0
    thermostat._control_step(now=0.0)
    thermostat._control_step(now=120.0)
    assert relay.cooler is True

    thermostat.set_mode(ModeState.IDLE)
    thermostat._temperature = 10.0
    thermostat._control_step(now=180.0)

    assert relay.cooler is False
    assert relay.compressor_pause is False


# ---------------------------------------------------------------------------
# Heater / cooler independence and freeze-guard tests
# ---------------------------------------------------------------------------


def test_heater_turns_on_below_freeze_guard_off_above_plus_hysteresis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Heater ON when T < freeze_guard, OFF when T > freeze_guard + hysteresis."""
    thermostat, relay, _sensor, _cache = _make_duty_cycle_testbed(monkeypatch)
    thermostat.set_mode(ModeState.COOL)  # freeze_guard=2.0, hysteresis=1.5
    thermostat._humidity = 40.0

    # T well above freeze_guard: heater OFF.
    thermostat._temperature = 5.0
    thermostat._control_step(now=0.0)
    assert relay.heater is False

    # T below freeze_guard: heater ON.
    thermostat._temperature = 1.5
    thermostat._control_step(now=1.0)
    assert relay.heater is True

    # T between freeze_guard and freeze_guard+hysteresis: heater stays ON.
    thermostat._temperature = 3.0
    thermostat._control_step(now=2.0)
    assert relay.heater is True

    # T above freeze_guard+hysteresis (3.5): heater OFF.
    thermostat._temperature = 4.0
    thermostat._control_step(now=3.0)
    assert relay.heater is False


def test_heater_not_triggered_during_cooler_spin_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: heater must not turn on while cooler is spinning down."""
    thermostat, relay, _sensor, _cache = _make_duty_cycle_testbed(
        monkeypatch,
        max_cooler_minutes=60.0,
        cooler_pause_minutes=3.0,
        cooler_soft_start_minutes=1.0,
    )
    thermostat.set_mode(ModeState.COOL)  # freeze_guard=2.0
    thermostat._humidity = 40.0

    # Engage cooler.
    thermostat._temperature = 10.0
    thermostat._control_step(now=0.0)
    thermostat._control_step(now=60.0)

    # Temperature drops to just above freeze_guard (still > 2.0) and cooler
    # begins spin-down. Heater must remain OFF throughout the spin-down.
    thermostat._temperature = 2.5
    for t in (120.0, 180.0, 240.0, 300.0):
        thermostat._control_step(now=t)
        assert relay.heater is False, f"heater unexpectedly on at t={t}"


# ---------------------------------------------------------------------------
# Vent-fan rate-limit tests
# ---------------------------------------------------------------------------


def test_vent_fan_rate_limit_blocks_rapid_switching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vent fan cannot switch on/off faster than min_vent_fan (both edges)."""
    thermostat, relay, _sensor, _cache = _make_duty_cycle_testbed(
        monkeypatch, min_vent_fan_minutes=5.0
    )
    thermostat.set_mode(ModeState.COOL)  # target_humidity=55, hyst=5

    # Turn on: humidity high.
    thermostat._temperature = 5.0
    thermostat._humidity = 80.0
    thermostat._control_step(now=0.0)
    assert relay.fan is True

    # Immediately try to turn off (humidity low) — should be blocked.
    thermostat._humidity = 20.0
    thermostat._control_step(now=60.0)  # 1 min later
    assert relay.fan is True

    # After 5 min (300 s), the change is allowed.
    thermostat._control_step(now=300.0)
    assert relay.fan is False


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
# Logging tests
# ---------------------------------------------------------------------------


def test_set_mode_logs_info_with_state_names_and_sensor_values(
    thermostat_testbed: tuple[Thermostat, _FakeRelay, _FakeSensor, _FakeCache],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """set_mode should log an info message with the new mode name, temperature and humidity."""
    thermostat, _relay, sensor, _cache = thermostat_testbed
    thermostat._temperature = 7.36
    thermostat._humidity = 62.84

    with caplog.at_level(logging.INFO, logger="fridge.thermostat"):
        thermostat.set_mode(ModeState.COOL)

    assert any(
        "COOL" in r.message and "7.4" in r.message and "62.8" in r.message for r in caplog.records
    )


def test_set_energy_state_logs_info_with_state_names_and_sensor_values(
    thermostat_testbed: tuple[Thermostat, _FakeRelay, _FakeSensor, _FakeCache],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """set_energy_state should log an info message with the new energy state name, temp and humidity."""
    thermostat, _relay, sensor, _cache = thermostat_testbed
    thermostat._temperature = 5.06
    thermostat._humidity = 50.16

    with caplog.at_level(logging.INFO, logger="fridge.thermostat"):
        thermostat.set_energy_state(EnergyState.LOW_PRICE)

    assert any(
        "LOW_PRICE" in r.message and "5.1" in r.message and "50.2" in r.message
        for r in caplog.records
    )


def test_set_action_state_logs_info_with_state_names_and_sensor_values(
    thermostat_testbed: tuple[Thermostat, _FakeRelay, _FakeSensor, _FakeCache],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_set_action_state should log an info message with the new action state name, temp and humidity."""
    thermostat, _relay, sensor, _cache = thermostat_testbed
    thermostat._temperature = 13.55
    thermostat._humidity = 44.44

    with caplog.at_level(logging.INFO, logger="fridge.thermostat"):
        thermostat._set_action_state(ActionState.COOLING)

    assert any(
        "COOLING" in r.message and "13.6" in r.message and "44.4" in r.message
        for r in caplog.records
    )


def test_set_energy_state_logs_only_on_change(
    thermostat_testbed: tuple[Thermostat, _FakeRelay, _FakeSensor, _FakeCache],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The 'Energy state changed to' log must be emitted only when the state actually changes."""
    thermostat, _relay, _sensor, _cache = thermostat_testbed
    thermostat._temperature = 5.0
    thermostat._humidity = 80.0
    thermostat.set_energy_state(EnergyState.NORMAL_PRICE)

    with caplog.at_level(logging.INFO, logger="fridge.thermostat"):
        thermostat.set_energy_state(EnergyState.NORMAL_PRICE)
        thermostat.set_energy_state(EnergyState.NORMAL_PRICE)

    energy_logs = [r for r in caplog.records if "Energy state changed to" in r.message]
    assert energy_logs == []

    with caplog.at_level(logging.INFO, logger="fridge.thermostat"):
        thermostat.set_energy_state(EnergyState.LOW_PRICE)

    energy_logs = [r for r in caplog.records if "Energy state changed to" in r.message]
    assert len(energy_logs) == 1


def test_state_change_logs_use_short_keys(
    thermostat_testbed: tuple[Thermostat, _FakeRelay, _FakeSensor, _FakeCache],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mode/energy/action logs must use short keys `temp=` and `hum=` (not `temperature=`/`humidity=`)."""
    thermostat, _relay, _sensor, _cache = thermostat_testbed
    thermostat._temperature = 5.2
    thermostat._humidity = 88.4

    with caplog.at_level(logging.INFO, logger="fridge.thermostat"):
        thermostat.set_mode(ModeState.COOL)
        thermostat.set_energy_state(EnergyState.LOW_PRICE)
        thermostat._set_action_state(ActionState.COOLING)

    for needle in ("Mode changed to", "Energy state changed to", "Action state changed to"):
        matches = [r for r in caplog.records if needle in r.message]
        assert matches, f"missing log for {needle!r}"
        for r in matches:
            assert "temp=" in r.message and "hum=" in r.message
            assert "temperature=" not in r.message
            assert "humidity=" not in r.message


def test_cooler_controller_log_includes_temp_hum() -> None:
    """CoolerController state transitions must include temp/hum when a provider is set."""
    calls: list[bool] = []
    controller = CoolerController(
        set_cooler=lambda v: calls.append(v),
        set_compressor_pause=lambda v: None,
        max_run_time=60.0,
        pause_time=10.0,
        soft_start_time=5.0,
        temp_hum_provider=lambda: (7.0, 81.0),
    )

    import logging as _logging

    caplog_records: list[_logging.LogRecord] = []

    class _Handler(_logging.Handler):
        def emit(self, record: _logging.LogRecord) -> None:
            caplog_records.append(record)

    handler = _Handler(level=_logging.INFO)
    src_logger = _logging.getLogger("fridge.thermostat")
    src_logger.addHandler(handler)
    src_logger.setLevel(_logging.INFO)
    try:
        controller.tick(now=0.0, cool_requested=True)
    finally:
        src_logger.removeHandler(handler)

    transition_msgs = [
        r.getMessage() for r in caplog_records if "CoolerController:" in r.getMessage()
    ]
    assert transition_msgs, "expected a CoolerController transition log"
    assert any("temp=7.0" in m and "hum=81.0" in m for m in transition_msgs)


def test_cooler_controller_log_shows_nan_when_readings_unavailable() -> None:
    """CoolerController must render NaN in the log when the provider returns NaN readings."""
    controller = CoolerController(
        set_cooler=lambda v: None,
        set_compressor_pause=lambda v: None,
        max_run_time=60.0,
        pause_time=10.0,
        soft_start_time=5.0,
        temp_hum_provider=lambda: (math.nan, math.nan),
    )

    import logging as _logging

    caplog_records: list[_logging.LogRecord] = []

    class _Handler(_logging.Handler):
        def emit(self, record: _logging.LogRecord) -> None:
            caplog_records.append(record)

    handler = _Handler(level=_logging.INFO)
    src_logger = _logging.getLogger("fridge.thermostat")
    src_logger.addHandler(handler)
    src_logger.setLevel(_logging.INFO)
    try:
        controller.tick(now=0.0, cool_requested=True)
    finally:
        src_logger.removeHandler(handler)

    transition_msgs = [
        r.getMessage() for r in caplog_records if "CoolerController:" in r.getMessage()
    ]
    assert transition_msgs
    assert any("nan" in m.lower() for m in transition_msgs)


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

    thermostat = Thermostat(gpio=cast(object, None), config=config)
    thermostat.set_mode(ModeState.COOL)

    thread = threading.Thread(target=thermostat._loop)
    thread.start()
    time.sleep(0.1)
    thermostat.stop_event.set()
    thread.join(timeout=1)

    assert thread.is_alive() is False
    assert call_count >= 3


# ---------------------------------------------------------------------------
# Cooler / compressor run-time accessors (used by HourlyAggregator)
# ---------------------------------------------------------------------------


def test_mains_on_false_when_cooler_off(
    thermostat_testbed: tuple[Thermostat, _FakeRelay, _FakeSensor, _FakeCache],
) -> None:
    """mains_on() is False while the CoolerController is in OFF state."""
    thermostat, _relay, _sensor, _cache = thermostat_testbed

    assert thermostat.cooler.state is CoolerState.OFF
    assert thermostat.mains_on() is False
    assert thermostat.compressor_running() is False


def test_mains_on_true_in_all_non_off_states(
    thermostat_testbed: tuple[Thermostat, _FakeRelay, _FakeSensor, _FakeCache],
) -> None:
    """mains_on() is True whenever the cooler unit is engaged (any non-OFF state)."""
    thermostat, _relay, _sensor, _cache = thermostat_testbed

    for state in (
        CoolerState.SOFT_START,
        CoolerState.RUNNING,
        CoolerState.PAUSED,
        CoolerState.SPIN_DOWN,
    ):
        thermostat.cooler.state = state
        assert thermostat.mains_on() is True, state


def test_compressor_running_only_true_in_running_state(
    thermostat_testbed: tuple[Thermostat, _FakeRelay, _FakeSensor, _FakeCache],
) -> None:
    """compressor_running() is True only in the RUNNING state."""
    thermostat, _relay, _sensor, _cache = thermostat_testbed

    thermostat.cooler.state = CoolerState.RUNNING
    assert thermostat.compressor_running() is True

    for state in (
        CoolerState.OFF,
        CoolerState.SOFT_START,
        CoolerState.PAUSED,
        CoolerState.SPIN_DOWN,
    ):
        thermostat.cooler.state = state
        assert thermostat.compressor_running() is False, state
