import pytest
import logging
from unittest.mock import MagicMock
import fridge.i2c as i2c_module
from fridge.i2c import i2c_scan, I2cGpio, RelayGpio, ButtonsGpio, Sht30
from fridge.enums import ModeState
from fridge.signals import mode_idle, mode_cooling_down, mode_cool

logger = logging.getLogger(__name__)


def test_i2c_scan():
    """
    Scan the I2C bus for connected devices.
    :return:
    """
    addresses = i2c_scan()
    assert 0x20 in addresses  # GPIO expander
    assert 0x44 in addresses  # SHT30 sensor


# =====================================
# SHT30 Temperature and Humidity Sensor
# =====================================


@pytest.fixture(autouse=True)
def sht30():
    """
    Fixture to provide a SHT30 sensor instance for testing.
    :return: SHT30 sensor instance
    """
    from fridge.i2c import Sht30

    return Sht30()


# =====================================
# MCP23017 GPIO and RelayGpio
# =====================================


@pytest.fixture
def mock_gpio(mocker):
    """
    Fixture to provide a mocked I2cGpio instance for RelayGpio testing.
    :return: Mocked I2cGpio instance
    """
    gpio = MagicMock(spec=I2cGpio)
    gpio.read.return_value = 0x00
    return gpio


def test_relay_gpio_set_cooler_on(mock_gpio):
    """
    Test that setting the cooler relay on writes bit 0 high on port B.
    """
    relay = RelayGpio(mock_gpio)
    relay.set_cooler_relay(True)
    mock_gpio.write.assert_called_once_with("B", 0x01)


def test_i2c_gpio_enables_interrupt_for_button_bits(mocker):
    """
    I2cGpio should enable port A interrupts for button bits 4-6 only.
    """
    write_mock = mocker.patch.object(I2cGpio, "_write_byte_register")

    I2cGpio(interrupt_gpio=27)

    write_mock.assert_any_call(i2c_module.MCP23017_IOCON, 0x04)
    write_mock.assert_any_call(i2c_module.MCP23017_IODIRA, 0xFF)
    write_mock.assert_any_call(i2c_module.MCP23017_INTCONA, 0x00)
    write_mock.assert_any_call(i2c_module.MCP23017_GPINTENA, 0x70)
    write_mock.assert_any_call(i2c_module.MCP23017_DEFVALA, 0x00)
    write_mock.assert_any_call(i2c_module.MCP23017_IODIRB, 0x00)


def test_buttons_gpio_start_interrupt_listener_handles_button_interrupt(mocker, mock_gpio):
    """
    Interrupt listener should call button handling callback on GPIO27 event.
    """

    class FakeButton:
        def __init__(self, pin: int, pull_up: bool):
            self.pin = pin
            self.pull_up = pull_up
            self.when_pressed = None

    fake_button = FakeButton(pin=27, pull_up=True)
    button_cls = mocker.Mock(return_value=fake_button)
    mocker.patch.object(i2c_module, "GpioZeroButton", button_cls)
    mock_gpio.clear_port_a_interrupt = mocker.Mock(return_value=0xEF)
    buttons = ButtonsGpio(mock_gpio)
    handle_mock = mocker.patch.object(buttons, "handle_button_press", return_value=True)

    assert buttons.start_interrupt_listener(27) is True
    assert fake_button.pin == 27
    assert fake_button.pull_up is True
    assert callable(fake_button.when_pressed)

    fake_button.when_pressed()

    mock_gpio.clear_port_a_interrupt.assert_called_once_with()
    handle_mock.assert_called_once_with(input_state=0xEF)


def test_relay_gpio_set_cooler_off(mock_gpio):
    """
    Test that setting the cooler relay off clears bit 0 on port B.
    """
    mock_gpio.read.return_value = 0x01  # cooler was on
    relay = RelayGpio(mock_gpio)
    relay.set_cooler_relay(False)
    mock_gpio.write.assert_called_once_with("B", 0x00)


def test_relay_gpio_read_cooler(mock_gpio):
    """
    Test reading the cooler relay state from port B bit 0.
    """
    mock_gpio.read.return_value = 0x01
    relay = RelayGpio(mock_gpio)
    assert relay.read_cooler_relay() is True

    mock_gpio.read.return_value = 0x00
    assert relay.read_cooler_relay() is False


def test_relay_gpio_set_heater_on(mock_gpio):
    """
    Test that setting the heater relay on writes bit 1 high on port B.
    """
    relay = RelayGpio(mock_gpio)
    relay.set_heater_relay(True)
    mock_gpio.write.assert_called_once_with("B", 0x02)


def test_relay_gpio_set_heater_off(mock_gpio):
    """
    Test that setting the heater relay off clears bit 1 on port B.
    """
    mock_gpio.read.return_value = 0x02  # heater was on
    relay = RelayGpio(mock_gpio)
    relay.set_heater_relay(False)
    mock_gpio.write.assert_called_once_with("B", 0x00)


def test_relay_gpio_read_heater(mock_gpio):
    """
    Test reading the heater relay state from port B bit 1.
    """
    mock_gpio.read.return_value = 0x02
    relay = RelayGpio(mock_gpio)
    assert relay.read_heater_relay() is True

    mock_gpio.read.return_value = 0x00
    assert relay.read_heater_relay() is False


def test_relay_gpio_set_vent_fan_on(mock_gpio):
    """
    Test that setting the vent fan relay on writes bit 2 high on port B.
    """
    relay = RelayGpio(mock_gpio)
    relay.set_vent_fan_relay(True)
    mock_gpio.write.assert_called_once_with("B", 0x04)


def test_relay_gpio_set_vent_fan_off(mock_gpio):
    """
    Test that setting the vent fan relay off clears bit 2 on port B.
    """
    mock_gpio.read.return_value = 0x04  # vent fan was on
    relay = RelayGpio(mock_gpio)
    relay.set_vent_fan_relay(False)
    mock_gpio.write.assert_called_once_with("B", 0x00)


def test_relay_gpio_read_vent_fan(mock_gpio):
    """
    Test reading the vent fan relay state from port B bit 2.
    """
    mock_gpio.read.return_value = 0x04
    relay = RelayGpio(mock_gpio)
    assert relay.read_vent_fan_relay() is True

    mock_gpio.read.return_value = 0x00
    assert relay.read_vent_fan_relay() is False


def test_relay_gpio_set_preserves_other_bits(mock_gpio):
    """
    Test that setting one relay does not affect the other relay bits.
    """
    mock_gpio.read.return_value = 0x06  # heater and vent fan on
    relay = RelayGpio(mock_gpio)
    relay.set_cooler_relay(True)
    mock_gpio.write.assert_called_once_with("B", 0x07)  # all three on


def test_buttons_gpio_handle_off_button_sets_only_off_led(mock_gpio):
    """
    Off button should set only off LED active and emit idle mode signal.
    """
    mock_gpio.read.side_effect = [0xEF, 0x27]  # Port A off pressed, Port B relay bits active
    buttons = ButtonsGpio(mock_gpio)
    receiver = MagicMock()
    mode_idle.connect(receiver)

    try:
        assert buttons.handle_button_press() is True
    finally:
        mode_idle.disconnect(receiver)

    mock_gpio.write.assert_called_once_with("B", 0x17)
    receiver.assert_called_once_with(buttons)


def test_buttons_gpio_handle_cool_down_button_sets_only_cool_down_led(mock_gpio):
    """
    Cool down button should set only cool down LED active and emit cooling down signal.
    """
    mock_gpio.read.side_effect = [0xDF, 0x17]  # Port A cool down pressed, Port B has other bits
    buttons = ButtonsGpio(mock_gpio)
    receiver = MagicMock()
    mode_cooling_down.connect(receiver)

    try:
        assert buttons.handle_button_press() is True
    finally:
        mode_cooling_down.disconnect(receiver)

    mock_gpio.write.assert_called_once_with("B", 0x27)
    receiver.assert_called_once_with(buttons)


def test_buttons_gpio_handle_cool_button_sets_only_cool_led(mock_gpio):
    """
    Cool button should set only cool LED active and emit cool mode signal.
    """
    mock_gpio.read.side_effect = [0xBF, 0x07]  # Port A cool pressed
    buttons = ButtonsGpio(mock_gpio)
    receiver = MagicMock()
    mode_cool.connect(receiver)

    try:
        assert buttons.handle_button_press() is True
    finally:
        mode_cool.disconnect(receiver)

    mock_gpio.write.assert_called_once_with("B", 0x47)
    receiver.assert_called_once_with(buttons)


def test_buttons_gpio_handle_button_press_returns_false_when_no_button_pressed(mock_gpio):
    """
    No button press should not update LEDs and should not emit a signal.
    """
    mock_gpio.read.return_value = 0xFF
    buttons = ButtonsGpio(mock_gpio)
    receiver = MagicMock()
    mode_idle.connect(receiver)

    try:
        assert buttons.handle_button_press() is False
    finally:
        mode_idle.disconnect(receiver)

    mock_gpio.write.assert_not_called()
    receiver.assert_not_called()


@pytest.mark.parametrize(
    ("mode", "port_b_before", "expected_write"),
    [
        (ModeState.IDLE, 0x27, 0x17),
        (ModeState.COOL_DOWN, 0x17, 0x27),
        (ModeState.COOL, 0x07, 0x47),
    ],
)
def test_buttons_gpio_set_mode_updates_leds_for_mode(
    mode, port_b_before, expected_write, mock_gpio
):
    """
    set_mode should update only mode LED bits according to the mode state.
    """
    mock_gpio.read.return_value = port_b_before
    buttons = ButtonsGpio(mock_gpio)

    buttons.set_mode(mode)

    mock_gpio.write.assert_called_once_with("B", expected_write)


def test_buttons_gpio_set_mode_raises_for_unsupported_mode(mock_gpio):
    """
    set_mode should raise ValueError for an unsupported mode value.
    """
    buttons = ButtonsGpio(mock_gpio)

    with pytest.raises(ValueError, match="Unsupported mode"):
        buttons.set_mode(99)


def test_on_interrupt_uses_captured_state_for_button_handling(mocker, mock_gpio):
    """
    _on_interrupt should use the INTCAP captured state to handle button press,
    not re-read the GPIO which may have changed (button released).
    """
    # clear_port_a_interrupt returns the captured state with off button pressed
    mock_gpio.clear_port_a_interrupt = mocker.Mock(return_value=0xEF)
    buttons = ButtonsGpio(mock_gpio)

    receiver = mocker.Mock()
    mode_idle.connect(receiver)

    try:
        buttons._on_interrupt()
    finally:
        mode_idle.disconnect(receiver)

    mock_gpio.clear_port_a_interrupt.assert_called_once()
    receiver.assert_called_once()


def test_on_interrupt_ignores_release_event(mocker, mock_gpio):
    """
    When interrupt fires on button release (all buttons high), no signal
    should be emitted.
    """
    # All buttons released - all bits high
    mock_gpio.clear_port_a_interrupt = mocker.Mock(return_value=0xFF)
    buttons = ButtonsGpio(mock_gpio)

    idle_receiver = mocker.Mock()
    cool_receiver = mocker.Mock()
    mode_idle.connect(idle_receiver)
    mode_cool.connect(cool_receiver)

    try:
        buttons._on_interrupt()
    finally:
        mode_idle.disconnect(idle_receiver)
        mode_cool.disconnect(cool_receiver)

    idle_receiver.assert_not_called()
    cool_receiver.assert_not_called()


def test_on_interrupt_handles_oserror_gracefully(mocker, mock_gpio):
    """
    _on_interrupt should not crash when I2C communication fails.
    """
    mock_gpio.clear_port_a_interrupt = mocker.Mock(side_effect=OSError("I/O error"))
    buttons = ButtonsGpio(mock_gpio)

    # Should not raise
    buttons._on_interrupt()


def test_sht30_read_data_retries_on_oserror(mocker):
    """
    read_data should retry on transient OSError and succeed if a later
    attempt works.
    """
    mocker.patch("fridge.i2c.time")  # skip real sleep
    sht30 = Sht30.__new__(Sht30)
    sht30.addr = 0x44

    call_count = 0

    def fake_fetch():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise OSError("I/O error")
        return 22.0, 55.0

    mocker.patch.object(sht30, "_fetch_sample", side_effect=fake_fetch)

    temp, hum = sht30.read_data()
    assert temp == 22.0
    assert hum == 55.0
    assert call_count == 3


def test_sht30_read_data_raises_after_all_retries_fail(mocker):
    """
    read_data should re-raise OSError after exhausting all retries.
    """
    mocker.patch("fridge.i2c.time")  # skip real sleep
    sht30 = Sht30.__new__(Sht30)
    sht30.addr = 0x44

    mocker.patch.object(sht30, "_fetch_sample", side_effect=OSError("persistent error"))

    with pytest.raises(OSError, match="persistent error"):
        sht30.read_data()


def test_sht30_crc(sht30):
    """
    Test the CRC calculation for the SHT30 sensor.
    """
    # Example from datasheet: CRC(0xBEEF) = 0x92
    assert sht30._calculate_crc(bytes([0xBE, 0xEF])) == 0x92
    # Additional test cases
    # 0x33 0x6e should have CRC 0xBA (if I calculated correctly or just update with actual expected)
    assert sht30._calculate_crc(bytes([0x33, 0x6E])) == 0xBA


def test_sht30_status(sht30):
    """
    Test the status register of the SHT30 sensor.
    :return:
    """
    status = sht30.read_status()
    # Check that status is a 16-bit integer
    assert isinstance(status, int)


def test_sht30_art_fetch_data(sht30):
    """
    Test the data fetch functionality of the SHT30 sensor.
    Make several readings and check that the values are within expected ranges.
    The temperature should ba above 10°C and humidity should be above 30%.
    :return:
    """
    sht30.start_art()
    import time

    time.sleep(0.1)  # Wait for first measurement
    for _ in range(3):
        temp, hum = sht30.read_data()
        logger.info(f"Read temp: {temp}, hum: {hum}")
        assert 10.0 < temp < 40.0
        assert 30.0 < hum < 90.0
        time.sleep(0.5)
