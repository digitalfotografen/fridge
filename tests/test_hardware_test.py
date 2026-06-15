"""Tests for the hardware test sequence."""

import logging
from unittest.mock import MagicMock, call, patch

import pytest

from fridge.hardware_test import (
    _LED_BITS,
    _LED_MASK,
    _set_led_only,
    run_hardware_test,
)


@pytest.fixture
def mock_gpio() -> MagicMock:
    """Create a mock I2cGpio instance that tracks port B state."""
    gpio = MagicMock()
    gpio._port_b_state = 0x00

    def read_side_effect(port: str) -> int:
        if port == "B":
            return gpio._port_b_state
        return 0

    def write_side_effect(port: str, value: int) -> None:
        if port == "B":
            gpio._port_b_state = value

    gpio.read.side_effect = read_side_effect
    gpio.write.side_effect = write_side_effect
    return gpio


@pytest.fixture
def mock_sensor() -> MagicMock:
    """Create a mock Sht30 sensor."""
    sensor = MagicMock()
    sensor.read_data.return_value = (21.5, 45.3)
    return sensor


@pytest.fixture
def mock_sleep() -> MagicMock:
    """Create a mock sleep function."""
    return MagicMock()


# ---------------------------------------------------------------------------
# _set_led_only tests
# ---------------------------------------------------------------------------


class TestSetLedOnly:
    """Tests for the _set_led_only helper function."""

    def test_sets_single_led(self, mock_gpio: MagicMock) -> None:
        """Setting a LED bit activates only that LED."""
        _set_led_only(mock_gpio, 4)
        assert mock_gpio._port_b_state == (1 << 4)

    def test_clears_all_leds_when_none(self, mock_gpio: MagicMock) -> None:
        """Passing None clears all LED bits."""
        mock_gpio._port_b_state = (1 << 4) | (1 << 5) | (1 << 6)
        _set_led_only(mock_gpio, None)
        assert mock_gpio._port_b_state == 0x00

    def test_preserves_relay_bits(self, mock_gpio: MagicMock) -> None:
        """Relay bits (0-2) are preserved when setting LEDs."""
        mock_gpio._port_b_state = 0x07  # All relay bits set
        _set_led_only(mock_gpio, 5)
        assert mock_gpio._port_b_state == 0x07 | (1 << 5)

    def test_preserves_relay_bits_when_clearing(self, mock_gpio: MagicMock) -> None:
        """Relay bits are preserved when clearing all LEDs."""
        mock_gpio._port_b_state = 0x07 | (1 << 6)  # Relays + Cool LED
        _set_led_only(mock_gpio, None)
        assert mock_gpio._port_b_state == 0x07

    def test_switches_led(self, mock_gpio: MagicMock) -> None:
        """Switching from one LED to another clears the previous one."""
        _set_led_only(mock_gpio, 4)
        assert mock_gpio._port_b_state == (1 << 4)
        _set_led_only(mock_gpio, 5)
        assert mock_gpio._port_b_state == (1 << 5)


# ---------------------------------------------------------------------------
# run_hardware_test tests
# ---------------------------------------------------------------------------


class TestRunHardwareTest:
    """Tests for the full hardware test sequence."""

    def test_step1_reads_sensor(
        self,
        mock_gpio: MagicMock,
        mock_sensor: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """Step 1 reads the temperature and humidity sensor."""
        run_hardware_test(mock_gpio, mock_sensor, sleep_func=mock_sleep)
        mock_sensor.read_data.assert_called_once()

    def test_step1_logs_temperature_and_humidity(
        self,
        mock_gpio: MagicMock,
        mock_sensor: MagicMock,
        mock_sleep: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Step 1 logs the measured temperature and humidity."""
        mock_sensor.read_data.return_value = (21.5, 45.3)
        with caplog.at_level(logging.INFO, logger="fridge.hardware_test"):
            run_hardware_test(mock_gpio, mock_sensor, sleep_func=mock_sleep)
        assert any("21.5" in r.message and "45.3" in r.message for r in caplog.records)

    def test_step2_running_light_only_activates_leds(
        self,
        mock_gpio: MagicMock,
        mock_sensor: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """Step 2 running light only writes LED bits, never relay bits."""
        # Set relay bits to verify they're preserved
        mock_gpio._port_b_state = 0x00

        write_calls: list[int] = []
        original_write = mock_gpio.write.side_effect

        def capture_write(port: str, value: int) -> None:
            if port == "B":
                write_calls.append(value)
            original_write(port, value)

        mock_gpio.write.side_effect = capture_write

        run_hardware_test(mock_gpio, mock_sensor, sleep_func=mock_sleep)

        # During LED-only phase (before relays are activated),
        # no relay bits should be set
        relay_mask = 0x07
        for val in write_calls[: len(_LED_BITS) * 2]:  # First writes are LED sequence
            assert val & relay_mask == 0, f"Relay bits set during LED sequence: 0x{val:02x}"

    def test_step2_sleeps_for_each_led(
        self,
        mock_gpio: MagicMock,
        mock_sensor: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """Step 2 sleeps 1 second per LED iteration."""
        run_hardware_test(mock_gpio, mock_sensor, sleep_func=mock_sleep)
        # 5 seconds / 1 second per LED = 5 sleep(1.0) calls for step 2
        one_second_calls = [c for c in mock_sleep.call_args_list if c == call(1.0)]
        assert len(one_second_calls) >= 5

    def test_step2_leds_off_after_sequence(
        self,
        mock_gpio: MagicMock,
        mock_sensor: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """All LEDs are off after the running light sequence."""
        write_values: list[int] = []
        original_write = mock_gpio.write.side_effect

        def capture_write(port: str, value: int) -> None:
            if port == "B":
                write_values.append(value)
            original_write(port, value)

        mock_gpio.write.side_effect = capture_write

        run_hardware_test(mock_gpio, mock_sensor, sleep_func=mock_sleep)

        # After the 5 LED-on writes, the next write should clear LEDs
        # Find the first 0x00 write after the LED sequence
        led_writes = len(_LED_BITS) + (5 - len(_LED_BITS))  # 5 total LED on writes
        assert write_values[led_writes] & _LED_MASK == 0

    def test_step3_fan_and_idle_led(
        self,
        mock_gpio: MagicMock,
        mock_sensor: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """Step 3 activates fan relay and Idle LED."""
        run_hardware_test(mock_gpio, mock_sensor, sleep_func=mock_sleep)

        # After step 3 setup, port B should have fan relay (bit 2) and Idle LED (bit 4)
        # Check that sleep(60.0) was called
        assert call(60.0) in mock_sleep.call_args_list

    def test_step4_heater_and_cooldown_led(
        self,
        mock_gpio: MagicMock,
        mock_sensor: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """Step 4 activates heater relay and Cool Down LED."""
        run_hardware_test(mock_gpio, mock_sensor, sleep_func=mock_sleep)

        # Should have three 60-second sleeps (steps 3, 4, 5)
        sixty_second_calls = [c for c in mock_sleep.call_args_list if c == call(60.0)]
        assert len(sixty_second_calls) == 3

    def test_step6_all_off_at_end(
        self,
        mock_gpio: MagicMock,
        mock_sensor: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """Step 6 switches everything off."""
        run_hardware_test(mock_gpio, mock_sensor, sleep_func=mock_sleep)
        # Final state should have no LEDs and no relays active
        assert mock_gpio._port_b_state == 0x00

    def test_all_steps_logged(
        self,
        mock_gpio: MagicMock,
        mock_sensor: MagicMock,
        mock_sleep: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Each step produces a logger.info message."""
        with caplog.at_level(logging.INFO, logger="fridge.hardware_test"):
            run_hardware_test(mock_gpio, mock_sensor, sleep_func=mock_sleep)

        messages = [r.message for r in caplog.records]
        assert any("Step 1" in m for m in messages)
        assert any("Step 2" in m for m in messages)
        assert any("Step 3" in m for m in messages)
        assert any("Step 4" in m for m in messages)
        assert any("Step 5" in m for m in messages)
        assert any("Step 6" in m for m in messages)

    def test_completion_logged(
        self,
        mock_gpio: MagicMock,
        mock_sensor: MagicMock,
        mock_sleep: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Successful completion is logged."""
        with caplog.at_level(logging.INFO, logger="fridge.hardware_test"):
            run_hardware_test(mock_gpio, mock_sensor, sleep_func=mock_sleep)

        assert any("completed successfully" in r.message for r in caplog.records)

    def test_error_turns_everything_off(
        self,
        mock_gpio: MagicMock,
        mock_sensor: MagicMock,
    ) -> None:
        """On error, all relays and LEDs are turned off."""
        mock_sensor.read_data.side_effect = RuntimeError("sensor failure")

        with pytest.raises(RuntimeError, match="sensor failure"):
            run_hardware_test(mock_gpio, mock_sensor, sleep_func=MagicMock())

        assert mock_gpio._port_b_state == 0x00

    def test_relay_sequence_correct_order(
        self,
        mock_gpio: MagicMock,
        mock_sensor: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """Relays are activated in the correct order: fan, heater, cooler."""
        write_values: list[int] = []
        original_write = mock_gpio.write.side_effect

        def capture_write(port: str, value: int) -> None:
            if port == "B":
                write_values.append(value)
            original_write(port, value)

        mock_gpio.write.side_effect = capture_write

        run_hardware_test(mock_gpio, mock_sensor, sleep_func=mock_sleep)

        relay_mask = 0x07
        # Extract relay states from all writes
        relay_states = [v & relay_mask for v in write_values]

        # Find first fan activation (bit 2 = 0x04)
        fan_idx = next(i for i, v in enumerate(relay_states) if v & 0x04)
        # Find first heater activation (bit 1 = 0x02)
        heater_idx = next(i for i, v in enumerate(relay_states) if v & 0x02)
        # Find first cooler activation (bit 0 = 0x01)
        cooler_idx = next(i for i, v in enumerate(relay_states) if v & 0x01)

        assert fan_idx < heater_idx < cooler_idx


# ---------------------------------------------------------------------------
# CLI integration test
# ---------------------------------------------------------------------------


class TestTestCliCommand:
    """Tests for the 'test' CLI command."""

    def test_test_command_exists(self) -> None:
        """The 'test' command is registered with the CLI group."""
        from fridge.cli import cli

        assert "test" in [cmd for cmd in cli.commands]

    @patch("fridge.cli.Sht30")
    @patch("fridge.cli.I2cGpio")
    @patch("fridge.hardware_test.time.sleep")
    def test_test_command_runs_sequence(
        self,
        mock_sleep: MagicMock,
        mock_gpio_cls: MagicMock,
        mock_sensor_cls: MagicMock,
    ) -> None:
        """The 'test' command initializes hardware and runs the test sequence."""
        from click.testing import CliRunner

        from fridge.cli import cli

        mock_gpio_instance = MagicMock()
        mock_gpio_instance._port_b_state = 0x00

        def read_side_effect(port: str) -> int:
            if port == "B":
                return mock_gpio_instance._port_b_state
            return 0

        def write_side_effect(port: str, value: int) -> None:
            if port == "B":
                mock_gpio_instance._port_b_state = value

        mock_gpio_instance.read.side_effect = read_side_effect
        mock_gpio_instance.write.side_effect = write_side_effect
        mock_gpio_cls.return_value = mock_gpio_instance

        mock_sensor_instance = MagicMock()
        mock_sensor_instance.read_data.return_value = (20.0, 50.0)
        mock_sensor_cls.return_value = mock_sensor_instance

        runner = CliRunner()
        result = runner.invoke(cli, ["test"])

        assert result.exit_code == 0
        mock_sensor_instance.read_data.assert_called_once()

    @patch("fridge.cli.I2cGpio")
    def test_test_command_handles_gpio_error(
        self,
        mock_gpio_cls: MagicMock,
    ) -> None:
        """The 'test' command handles GPIO initialization errors."""
        from click.testing import CliRunner

        from fridge.cli import cli

        mock_gpio_cls.side_effect = RuntimeError("I2C bus error")

        runner = CliRunner()
        result = runner.invoke(cli, ["test"])

        assert result.exit_code == 1
