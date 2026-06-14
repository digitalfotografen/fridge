import logging
import threading
from smbus2 import SMBus
import time
from fridge.enums import ModeState
from fridge.signals import mode_idle, mode_cooling_down, mode_cool

try:
    from gpiozero import Button as GpioZeroButton
except (ImportError, RuntimeError):
    GpioZeroButton = None

logger = logging.getLogger(__name__)

# Global lock for I2C bus operations
_bus_lock = threading.Lock()


def i2c_scan() -> list[int]:
    """
    Scan I2C bus for connected devices and return list of addresses.
    :return: A list of I2C addresses
    """
    with _bus_lock:
        with SMBus(1) as bus:
            addresses = []
            for addr in range(256):
                try:
                    bus.read_byte(addr)
                    addresses.append(addr)
                    logger.info(f"Found I2C device at address: {addr:01x}")
                except OSError:
                    continue
            return addresses


def read_byte_register(addr: int, reg: int) -> int:
    """
    Read a byte register from the device at the specified address and register.
    :param addr: I2C device address
    :param reg: Register address
    :return: Register value
    """
    with _bus_lock:
        with SMBus(1) as bus:
            return bus.read_byte_data(addr, reg)


def write_byte_register(addr: int, reg: int, value: int) -> None:
    """
    Write a byte register to the device at the specified address and register.
    :param addr: I2C device address
    :param reg: Register address
    :param value: Register value to write
    """
    with _bus_lock:
        with SMBus(1) as bus:
            bus.write_byte_data(addr, reg, value)


def read_word_register(addr: int, reg: int) -> int:
    """
    Read a word register from the device at the specified address and register.
    :param addr: I2C device address
    :param reg: Register address
    :return: Register value
    """
    with _bus_lock:
        with SMBus(1) as bus:
            return bus.read_word_data(addr, reg)


def write_word_register(addr: int, reg: int, value: int) -> None:
    """
    Write a word register to the device at the specified address and register.
    :param addr: I2C device address
    :param reg: Register address
    :param value: Register value to write
    :return:
    """
    with _bus_lock:
        with SMBus(1) as bus:
            bus.write_word_data(addr, reg, value)


# =====================================
# SHT30 Temperature and Humidity Sensor
# =====================================

SHT30_ADDRESS = 0x44
SHT30_ART = 0x2B32
SHT30_FETCH_DATA = 0xE000
SHT30_STATUS = 0x3022
SHT30_SOFT_RESET = 0x30A2


class Sht30:
    """
    Interface for the SHT30 temperature and humidity sensor.
    This will be used to read temperature and humidity from the sensor.
    The sensor will be initialized to use the ART (Accelerated Response Time) mode, to measure at 4Hz.
    """

    def __init__(self, addr: int = SHT30_ADDRESS):
        self.addr = addr

    def read_status(self) -> int:
        """
        Read the 16-bit status register from the SHT30 sensor.
        :return: Status register value
        """
        # Note: If the sensor is in ART mode, it might not respond to some commands.
        # However, data fetch command works.
        try:
            with _bus_lock:
                with SMBus(1) as bus:
                    # Write command 0x3022
                    bus.write_i2c_block_data(self.addr, SHT30_STATUS >> 8, [SHT30_STATUS & 0xFF])
                    import time

                    time.sleep(0.01)
                    # Read 3 bytes (2 bytes status + 1 byte CRC)
                    data = bus.read_i2c_block_data(self.addr, 0, 3)
                    if self._calculate_crc(bytes(data[:2])) != data[2]:
                        expected = self._calculate_crc(bytes(data[:2]))
                        logger.error(
                            f"CRC check failed for status register: {data[2]} != {expected}"
                        )
                        raise ValueError("CRC check failed")
                    return (data[0] << 8) | data[1]
        except OSError as e:
            logger.warning(
                f"Failed to read status register: {e}. Sensor might be busy or in ART mode."
            )
            return 0  # Fallback

    def soft_reset(self):
        """
        Perform a soft reset on the SHT30 sensor.
        """
        with _bus_lock:
            with SMBus(1) as bus:
                bus.write_i2c_block_data(
                    self.addr, SHT30_SOFT_RESET >> 8, [SHT30_SOFT_RESET & 0xFF]
                )
        time.sleep(0.01)

    _READ_RETRIES = 3
    _RETRY_DELAY = 0.05

    def read_data(self) -> tuple[float, float]:
        """
        Fetch the last temperature and humidity sample from the SHT30 sensor.

        The method retries up to :attr:`_READ_RETRIES` times on transient
        I/O errors before re-raising the last exception.

        :return: A tuple of temperature and humidity values.
        :raises OSError: If all retry attempts fail.
        """
        last_error: OSError | None = None
        for attempt in range(self._READ_RETRIES):
            try:
                return self._fetch_sample()
            except OSError as exc:
                last_error = exc
                logger.warning(
                    "SHT30 read attempt %d/%d failed: %s",
                    attempt + 1,
                    self._READ_RETRIES,
                    exc,
                )
                time.sleep(self._RETRY_DELAY)
        raise last_error  # type: ignore[misc]

    def _fetch_sample(self) -> tuple[float, float]:
        """
        Perform a single fetch of temperature and humidity from the sensor.

        :return: A tuple of temperature and humidity values.
        :raises OSError: On I2C communication failure.
        :raises ValueError: On CRC check failure.
        """
        with _bus_lock:
            with SMBus(1) as bus:
                # To fetch data we use the fetch data command
                bus.write_i2c_block_data(
                    self.addr, SHT30_FETCH_DATA >> 8, [SHT30_FETCH_DATA & 0xFF]
                )
                # Read 6 bytes (2 bytes temp + 1 byte CRC + 2 bytes hum + 1 byte CRC)
                data = bus.read_i2c_block_data(self.addr, 0, 6)

                # Check temperature CRC
                if self._calculate_crc(bytes(data[0:2])) != data[2]:
                    logger.error("CRC check failed for temperature data")
                    raise ValueError("CRC check failed")

                # Check humidity CRC
                if self._calculate_crc(bytes(data[3:5])) != data[5]:
                    logger.error("CRC check failed for humidity data")
                    raise ValueError("CRC check failed")

                # Convert data
                temp_raw = (data[0] << 8) | data[1]
                hum_raw = (data[3] << 8) | data[4]

                temperature = -45 + 175 * (temp_raw / 65535.0)
                humidity = 100 * (hum_raw / 65535.0)

                return temperature, humidity

    def start_art(self):
        """
        Start the Accelerated Response Time (ART) mode.
        """
        with _bus_lock:
            with SMBus(1) as bus:
                bus.write_i2c_block_data(self.addr, SHT30_ART >> 8, [SHT30_ART & 0xFF])

    @staticmethod
    def _calculate_crc(data: bytes) -> int:
        """
        Calculate the CRC-8 checksum for the given data.
        Polynomial: 0x31 (x^8 + x^5 + x^4 + 1)
        Initialization: 0xFF
        """
        crc = 0xFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 0x80:
                    crc = (crc << 1) ^ 0x31
                else:
                    crc <<= 1
                crc &= 0xFF
        return crc


# =====================================
# MCP23017 GPIO
# =====================================
MCP23017_ADDRESS = 0x20
# Define registers for MCP23017
MCP23017_IODIRA = 0x00  # IO direction A - 1= input 0 = output
MCP23017_IODIRB = 0x01  # IO direction B - 1= input 0 = output
MCP23017_IPOLA = 0x02  # Input polarity A
MCP23017_IPOLB = 0x03  # Input polarity B
MCP23017_GPINTENA = 0x04  # Interrupt-onchange A
MCP23017_GPINTENB = 0x05  # Interrupt-onchange B
MCP23017_DEFVALA = 0x06  # Default value for port A
MCP23017_DEFVALB = 0x07  # Default value for port B
MCP23017_INTCONA = 0x08  # Interrupt control register for port A
MCP23017_INTCONB = 0x09  # Interrupt control register for port B
MCP23017_IOCON = 0x0A  # Configuration register
MCP23017_GPPUA = 0x0C  # Pull-up resistors for port A
MCP23017_GPPUB = 0x0D  # Pull-up resistors for port B
MCP23017_INTFA = 0x0E  # Interrupt condition for port A
MCP23017_INTFB = 0x0F  # Interrupt condition for port B
MCP23017_INTCAPA = 0x10  # Interrupt capture for port A
MCP23017_INTCAPB = 0x11  # Interrupt capture for port B
MCP23017_GPIOA = 0x12  # Data port A
MCP23017_GPIOB = 0x13  # Data port B
MCP23017_OLATA = 0x14  # Output latches A
MCP23017_OLATB = 0x15  # Output latches B


class I2cGpio:
    """
    Custom GPIO interface using MCP23017 to create eight input pins and eight output pins.
    GPA0-7 is used as inputs.
    GPB0-7 is used as outputs.
    """

    _BUTTON_INTERRUPT_MASK = (1 << 4) | (1 << 5) | (1 << 6)

    def __init__(self, addr: int = MCP23017_ADDRESS, interrupt_gpio: int | None = None):
        """
        Initialize the I2cGpio object.
        :param addr: I2C address
        :param interrupt_gpio: Raspberry Pi GPIO pin used as interrupt input for port A.
        """
        self.interrupt_gpio = interrupt_gpio
        self.addr = addr

        # INT pin as an open-drain output
        self._write_byte_register(MCP23017_IOCON, 0x04)
        # GPA as inputs
        self._write_byte_register(MCP23017_IODIRA, 0xFF)
        # Interrupt on change mode
        self._write_byte_register(MCP23017_INTCONA, 0x00)
        # Enable interrupts for button bits only (GPA4-6)
        self._write_byte_register(MCP23017_GPINTENA, self._BUTTON_INTERRUPT_MASK)
        # Use 0 as default
        self._write_byte_register(MCP23017_DEFVALA, 0x00)

        # GPB as outputs
        self._write_byte_register(MCP23017_IODIRB, 0x00)

    def read(self, port: str = "A") -> int:
        """
        Read all bits of a port and return as an int value.
        :param port: Port to read, either ``A`` (inputs) or ``B`` (outputs)
        :return: Port value as an int
        """
        match port:
            case "A":
                return read_byte_register(self.addr, MCP23017_GPIOA)
            case "B":
                return read_byte_register(self.addr, MCP23017_GPIOB)
            case _:
                raise ValueError(f"Invalid port {port}. Allowed values are A and B.")

    def write(self, port: str, value: int) -> None:
        """
        Write a byte value to all bits of a port.
        :param port: Port to write, either ``A`` or ``B``
        :param value: Byte value to write (0–255)
        :raises ValueError: If port is not ``A`` or ``B``
        """
        match port:
            case "A":
                self._write_byte_register(MCP23017_OLATA, value)
            case "B":
                self._write_byte_register(MCP23017_OLATB, value)
            case _:
                raise ValueError(f"Invalid port {port}. Allowed values are A and B.")

    def _read_byte_register(self, reg: int) -> int:
        """
        Read a byte register from the device.
        :param reg: Register address
        :return: Register value
        """
        with _bus_lock:
            with SMBus(1) as bus:
                return bus.read_byte_data(self.addr, reg)

    def _write_byte_register(self, reg: int, value: int) -> None:
        """
        Write a byte register to the device.
        :param reg: Register address
        :param value: Register value to write
        """
        with _bus_lock:
            with SMBus(1) as bus:
                bus.write_byte_data(self.addr, reg, value)

    def clear_port_a_interrupt(self) -> int:
        """
        Clear a pending port A interrupt by reading interrupt capture register.
        :return: Captured state for port A at interrupt time
        """
        return self._read_byte_register(MCP23017_INTCAPA)


class RelayGpio:
    """
    Relay GPIO class for controlling the relays on the fridge.
    This class provides methods to control the relays on the fridge using the MCP23017 GPIO expander.
    The relays are connected to the GPIO pins GPB0-2.
    Bit 0: Cooler relay
    Bit 1: Heater relay
    Bit 2: Vent fan relay
    """

    def __init__(self, gpio: I2cGpio):
        """
        This expects an initialized instance of the I2cGpio class.
        :param gpio: I2cGpio GPIO expander instance
        """
        self.gpio = gpio

    _COOLER_BIT = 0
    _HEATER_BIT = 1
    _VENT_FAN_BIT = 2

    def _set_relay_bit(self, bit: int, state: bool) -> None:
        """
        Set or clear a single relay bit on port B without affecting other bits.
        :param bit: Bit position (0–7)
        :param state: ``True`` to activate the relay, ``False`` to deactivate
        """
        current = self.gpio.read("B")
        if state:
            new_value = current | (1 << bit)
        else:
            new_value = current & ~(1 << bit)
        self.gpio.write("B", new_value)

    def _read_relay_bit(self, bit: int) -> bool:
        """
        Read the state of a single relay bit from port B.
        :param bit: Bit position (0–7)
        :return: ``True`` if the relay is active, ``False`` otherwise
        """
        return bool(self.gpio.read("B") & (1 << bit))

    def set_cooler_relay(self, state: bool) -> None:
        """
        Set the cooler relay state.
        :param state: ``True`` to activate the cooler, ``False`` to deactivate
        """
        self._set_relay_bit(self._COOLER_BIT, state)

    def read_cooler_relay(self) -> bool:
        """
        Read the current state of the cooler relay.
        :return: ``True`` if the cooler relay is active, ``False`` otherwise
        """
        return self._read_relay_bit(self._COOLER_BIT)

    def set_heater_relay(self, state: bool) -> None:
        """
        Set the heater relay state.
        :param state: ``True`` to activate the heater, ``False`` to deactivate
        """
        self._set_relay_bit(self._HEATER_BIT, state)

    def read_heater_relay(self) -> bool:
        """
        Read the current state of the heater relay.
        :return: ``True`` if the heater relay is active, ``False`` otherwise
        """
        return self._read_relay_bit(self._HEATER_BIT)

    def set_vent_fan_relay(self, state: bool) -> None:
        """
        Set the ventilation fan relay state.
        :param state: ``True`` to activate the vent fan, ``False`` to deactivate
        """
        self._set_relay_bit(self._VENT_FAN_BIT, state)

    def read_vent_fan_relay(self) -> bool:
        """
        Read the current state of the ventilation fan relay.
        :return: ``True`` if the vent fan relay is active, ``False`` otherwise
        """
        return self._read_relay_bit(self._VENT_FAN_BIT)


class ButtonsGpio:
    """
    GPIO interface for buttons and LED indicators.
    Each button is connected to a GPIO input pin,
    and each LED indicator is connected to a corresponding GPIO output pin.

    Pressing a button will trigger an interrupt on the GPIO 27 pin.
    This should light the corresponding LED indicator, snd clear the other LED indicators.
    It should the issue a corresponding blinker signal to alert other parts of the system.

    The button GPIO pin is configured as an input with pull-up resistor,
    and the LED GPIO pins are configured as outputs with active-low logic level.

    Port A inputs:
    Bit 4: Off button
    Bit 5: Cool down button
    Bit 6: Cool button

    Port B inputs:
    Bit 0: Cooler relay
    Bit 1: Heater relay
    Bit 2: Vent fan relay

    Note: The GPIO is used for other purposes, don't change other pins
    """

    def __init__(self, gpio: I2cGpio):
        """
        This expects an initialized instance of the I2cGpio class.
        :param gpio: I2cGpio GPIO expander instance
        """
        self.gpio = gpio
        self._interrupt_button = None

    _OFF_BUTTON_BIT = 4
    _COOL_DOWN_BUTTON_BIT = 5
    _COOL_BUTTON_BIT = 6

    _OFF_LED_BIT = 4
    _COOL_DOWN_LED_BIT = 5
    _COOL_LED_BIT = 6

    _BUTTON_MASK = (1 << _OFF_BUTTON_BIT) | (1 << _COOL_DOWN_BUTTON_BIT) | (1 << _COOL_BUTTON_BIT)
    _LED_MASK = (1 << _OFF_LED_BIT) | (1 << _COOL_DOWN_LED_BIT) | (1 << _COOL_LED_BIT)

    def handle_button_press(self, input_state: int | None = None) -> bool:
        """
        Handle a button press event.

        When called from an interrupt handler, ``input_state`` should be the
        value captured by the MCP23017 INTCAP register at interrupt time so
        that a quick button release does not cause the press to be missed.

        :param input_state: Captured port A value, or ``None`` to read live
        :return: ``True`` if a button press was handled, ``False`` otherwise
        """
        if input_state is None:
            input_state = self.gpio.read("A")

        if not (input_state & (1 << self._OFF_BUTTON_BIT)):
            self._set_active_led(self._OFF_LED_BIT)
            mode_idle.send(self)
            return True

        if not (input_state & (1 << self._COOL_DOWN_BUTTON_BIT)):
            self._set_active_led(self._COOL_DOWN_LED_BIT)
            mode_cooling_down.send(self)
            return True

        if not (input_state & (1 << self._COOL_BUTTON_BIT)):
            self._set_active_led(self._COOL_LED_BIT)
            mode_cool.send(self)
            return True

        return False

    def _set_active_led(self, led_bit: int) -> None:
        """
        Set one active mode LED while preserving unrelated port B bits.
        :param led_bit: Bit for LED to activate
        """
        current_output = self.gpio.read("B")
        new_output = (current_output & ~self._LED_MASK) | (1 << led_bit)
        self.gpio.write("B", new_output)

    def set_mode(self, mode: ModeState) -> None:
        """
        Set mode LED indicators based on a mode state.

        This updates only the mode LED bits and preserves all unrelated
        output bits on port B.

        :param mode: The mode state to indicate
        :raises ValueError: If ``mode`` is not a supported ``ModeState``
        """
        led_bits_by_mode = {
            ModeState.IDLE: self._OFF_LED_BIT,
            ModeState.COOL_DOWN: self._COOL_DOWN_LED_BIT,
            ModeState.COOL: self._COOL_LED_BIT,
        }

        led_bit = led_bits_by_mode.get(mode)
        if led_bit is None:
            raise ValueError(f"Unsupported mode: {mode}")

        self._set_active_led(led_bit)

    def start_interrupt_listener(self, gpio_pin: int = 27) -> bool:
        """
        Start Raspberry Pi GPIO interrupt listener for button presses.
        :param gpio_pin: BCM GPIO pin connected to MCP23017 INTA
        :return: ``True`` if listener started, ``False`` if GPIO backend unavailable
        """
        if GpioZeroButton is None:
            logger.warning("gpiozero Button backend unavailable, interrupt listener not started")
            return False

        if self._interrupt_button is not None:
            return True

        self._interrupt_button = GpioZeroButton(gpio_pin, pull_up=True)
        self._interrupt_button.when_pressed = self._on_interrupt
        return True

    def stop_interrupt_listener(self) -> None:
        """
        Stop GPIO interrupt listener and release GPIO resources.
        """
        if self._interrupt_button is None:
            return

        self._interrupt_button.close()
        self._interrupt_button = None

    def _on_interrupt(self) -> None:
        """
        Handle MCP23017 interrupt event from GPIO27.

        Reads the INTCAP register (which also clears the interrupt) and
        forwards the captured pin state to :meth:`handle_button_press` so
        that a quick button release is not missed.
        """
        try:
            captured_state = self.gpio.clear_port_a_interrupt()
            self.handle_button_press(input_state=captured_state)
        except OSError:
            logger.warning("I2C error during interrupt handling, skipping")
