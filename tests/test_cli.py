from configparser import ConfigParser
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fridge.cli import cli


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    """Create a temporary data directory for tests."""
    data_dir = tmp_path / "fridge_data"
    data_dir.mkdir()
    return data_dir


@pytest.fixture
def runner() -> CliRunner:
    """Create a Click test runner."""
    return CliRunner()


# ---------------------------------------------------------------------------
# Help tests
# ---------------------------------------------------------------------------


def test_help_option(runner: CliRunner) -> None:
    """--help displays usage information."""
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Usage" in result.output


def test_help_short_option(runner: CliRunner) -> None:
    """-h displays usage information."""
    result = runner.invoke(cli, ["-h"])
    assert result.exit_code == 0
    assert "Usage" in result.output


# ---------------------------------------------------------------------------
# Config command tests
# ---------------------------------------------------------------------------


def test_config_creates_ini_from_default(runner: CliRunner, tmp_data_dir: Path) -> None:
    """config command creates fridge.ini from config_default.ini if it does not exist."""
    with patch("fridge.cli.data_path", str(tmp_data_dir)):
        result = runner.invoke(cli, ["config"])

    assert result.exit_code == 0
    ini_path = tmp_data_dir / "fridge.ini"
    assert ini_path.exists()
    config = ConfigParser()
    config.read(str(ini_path))
    assert config.has_section("General")
    assert config.has_section("CoolDownMode")


def test_config_does_not_overwrite_existing(runner: CliRunner, tmp_data_dir: Path) -> None:
    """config command does not overwrite an existing fridge.ini."""
    ini_path = tmp_data_dir / "fridge.ini"
    ini_path.write_text("[Custom]\nkey = value\n")

    with patch("fridge.cli.data_path", str(tmp_data_dir)):
        result = runner.invoke(cli, ["config"])

    assert result.exit_code == 0
    config = ConfigParser()
    config.read(str(ini_path))
    assert config.has_section("Custom")


def test_config_displays_sections(runner: CliRunner, tmp_data_dir: Path) -> None:
    """config command displays configuration sections and values."""
    with patch("fridge.cli.data_path", str(tmp_data_dir)):
        result = runner.invoke(cli, ["config"])

    assert result.exit_code == 0
    assert "General" in result.output
    assert "CoolDownMode" in result.output
    assert "CoolMode" in result.output


def test_config_creates_data_dir_if_missing(runner: CliRunner, tmp_path: Path) -> None:
    """config command creates the data directory if it does not exist."""
    data_dir = tmp_path / "new_fridge_data"

    with patch("fridge.cli.data_path", str(data_dir)):
        result = runner.invoke(cli, ["config"])

    assert result.exit_code == 0
    assert data_dir.exists()
    assert (data_dir / "fridge.ini").exists()


# ---------------------------------------------------------------------------
# Show command tests
# ---------------------------------------------------------------------------


def test_show_displays_relay_states(runner: CliRunner) -> None:
    """show command displays relay and button states."""
    mock_gpio = MagicMock()
    # Port B: cooler=bit0 on, heater=bit1 off, fan=bit2 on, off_led=bit4 on
    mock_gpio.read.side_effect = lambda port: 0x15 if port == "B" else 0xF0

    mock_sensor = MagicMock()
    mock_sensor.read_data.return_value = (6.5, 72.3)

    with (
        patch("fridge.cli.I2cGpio", return_value=mock_gpio),
        patch("fridge.cli.RelayGpio") as mock_relay_cls,
        patch("fridge.cli.Cache") as mock_cache_cls,
        patch("fridge.cli.Sht30", return_value=mock_sensor),
    ):
        mock_relay = mock_relay_cls.return_value
        mock_relay.read_cooler_relay.return_value = True
        mock_relay.read_heater_relay.return_value = False
        mock_relay.read_vent_fan_relay.return_value = True

        mock_cache = mock_cache_cls.return_value
        mock_cache.get.return_value = None

        result = runner.invoke(cli, ["show"])

    assert result.exit_code == 0
    assert "Cooler" in result.output
    assert "Heater" in result.output
    assert "Fan" in result.output
    assert "6.5" in result.output
    assert "72.3" in result.output


def test_show_does_not_write_gpio(runner: CliRunner) -> None:
    """show command must not change any relay states (read-only)."""
    mock_gpio = MagicMock()
    mock_gpio.read.return_value = 0x00

    with (
        patch("fridge.cli.I2cGpio", return_value=mock_gpio),
        patch("fridge.cli.RelayGpio") as mock_relay_cls,
        patch("fridge.cli.Cache") as mock_cache_cls,
        patch("fridge.cli.Sht30") as mock_sht30_cls,
    ):
        mock_relay = mock_relay_cls.return_value
        mock_relay.read_cooler_relay.return_value = False
        mock_relay.read_heater_relay.return_value = False
        mock_relay.read_vent_fan_relay.return_value = False

        mock_cache = mock_cache_cls.return_value
        mock_cache.get.return_value = None

        mock_sht30_cls.return_value.read_data.return_value = (5.0, 50.0)

        result = runner.invoke(cli, ["show"])

    assert result.exit_code == 0
    mock_gpio.write.assert_not_called()
    mock_relay.set_cooler_relay.assert_not_called()
    mock_relay.set_heater_relay.assert_not_called()
    mock_relay.set_vent_fan_relay.assert_not_called()


def test_show_sensor_error_handled(runner: CliRunner) -> None:
    """show command handles sensor read errors gracefully."""
    mock_gpio = MagicMock()
    mock_gpio.read.return_value = 0x00

    with (
        patch("fridge.cli.I2cGpio", return_value=mock_gpio),
        patch("fridge.cli.RelayGpio") as mock_relay_cls,
        patch("fridge.cli.Cache") as mock_cache_cls,
        patch("fridge.cli.Sht30") as mock_sht30_cls,
    ):
        mock_relay = mock_relay_cls.return_value
        mock_relay.read_cooler_relay.return_value = False
        mock_relay.read_heater_relay.return_value = False
        mock_relay.read_vent_fan_relay.return_value = False

        mock_cache = mock_cache_cls.return_value
        mock_cache.get.return_value = None

        mock_sht30_cls.return_value.read_data.side_effect = OSError("No device")

        result = runner.invoke(cli, ["show"])

    assert result.exit_code == 0
    assert "Error reading sensor" in result.output


# ---------------------------------------------------------------------------
# Scan command tests
# ---------------------------------------------------------------------------


def test_scan_displays_devices(runner: CliRunner) -> None:
    """scan command displays found I2C devices."""
    with patch("fridge.cli.i2c_scan", return_value=[0x20, 0x44]):
        result = runner.invoke(cli, ["scan"])

    assert result.exit_code == 0
    assert "20" in result.output
    assert "44" in result.output


# ---------------------------------------------------------------------------
# Run command tests
# ---------------------------------------------------------------------------


def test_run_starts_and_stops_thermostat(runner: CliRunner, tmp_data_dir: Path) -> None:
    """run command starts thermostat and button listener, stops on interrupt."""
    mock_gpio = MagicMock()
    mock_thermostat = MagicMock()
    mock_buttons = MagicMock()
    mock_buttons.start_interrupt_listener.return_value = True

    with (
        patch("fridge.cli.data_path", str(tmp_data_dir)),
        patch("fridge.cli.I2cGpio", return_value=mock_gpio),
        patch("fridge.cli.Thermostat", return_value=mock_thermostat),
        patch("fridge.cli.ButtonsGpio", return_value=mock_buttons),
        patch("fridge.cli.load_config") as mock_load_config,
        patch("fridge.cli.signal"),
        patch("fridge.cli.threading") as mock_threading,
    ):
        mock_load_config.return_value = ConfigParser()
        # Make the shutdown event fire immediately
        mock_event = MagicMock()
        mock_event.wait.return_value = True
        mock_threading.Event.return_value = mock_event

        result = runner.invoke(cli, ["run"])

    assert result.exit_code == 0
    mock_thermostat.start.assert_called_once()
    mock_thermostat.stop.assert_called_once()
    mock_buttons.stop_interrupt_listener.assert_called_once()
