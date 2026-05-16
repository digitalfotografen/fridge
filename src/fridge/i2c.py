import logging
import threading
from smbus2 import SMBus
import time

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

    def read_data(self) -> tuple[float, float]:
        """
        Featch the last temperature and humidity sample from the SHT30 sensor.
        :return: A tuple of temperature and humidity values.
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
        # Disable all interrupts
        self._write_byte_register(MCP23017_GPINTENA, 0x00)
        # Use 0 as default
        self._write_byte_register(MCP23017_DEFVALA, 0x00)

        # GPB as outputs
        self._write_byte_register(MCP23017_IODIRB, 0x00)

    def read(self, port: str = "A") -> int:
        """
        Read the all bits of a port and return as an int value.
        :param port: Return the value of port A or B
        :return: Port value as an int
        """
        match port:
            case "A":
                return read_byte_register(self.addr, MCP23017_GPIOA)
            case "B":
                return read_byte_register(self.addr, MCP23017_GPIOB)
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
