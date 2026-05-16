from configparser import ConfigParser
from pathlib import Path

import pytest

from fridge import ActionState, I2cGpio, ModeState
from fridge.thermostat import Thermostat


pytestmark = pytest.mark.hardware


def _load_default_config() -> ConfigParser:
    config = ConfigParser()
    config.read("config_default.ini")
    return config


@pytest.fixture
def hardware_thermostat() -> Thermostat:
    if not Path("/dev/i2c-1").exists():
        pytest.skip("Hardware thermostat tests require /dev/i2c-1")

    config = _load_default_config()
    config.set("General", "refresh_rate", "1.0")

    gpio = I2cGpio(interrupt_gpio=config.getint("Hardware", "interrupt_gpio", fallback=27))
    thermostat = Thermostat(gpio=gpio, config=config)
    thermostat.set_mode(ModeState.IDLE)

    yield thermostat

    thermostat.stop()


def test_thermostat_hw_can_read_sensor_data(hardware_thermostat: Thermostat) -> None:
    hardware_thermostat._read_sensor()

    assert isinstance(hardware_thermostat.temperature, float)
    assert isinstance(hardware_thermostat.humidity, float)
    assert -40.0 <= hardware_thermostat.temperature <= 125.0
    assert 0.0 <= hardware_thermostat.humidity <= 100.0


def test_thermostat_hw_stop_in_idle_keeps_relays_off(hardware_thermostat: Thermostat) -> None:
    hardware_thermostat.stop()

    assert hardware_thermostat.stop_event.is_set()
    assert hardware_thermostat.action_state == ActionState.IDLE
    assert hardware_thermostat.relay.read_cooler_relay() is False
    assert hardware_thermostat.relay.read_heater_relay() is False
    assert hardware_thermostat.relay.read_vent_fan_relay() is False
