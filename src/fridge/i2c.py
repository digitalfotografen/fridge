import logging
import threading
from smbus2 import SMBus

logger = logging.getLogger(__name__)

_bus_lock = threading.Lock()
_bus = SMBus(1)


def i2c_scan() -> list[int]:
    """
    Scan I2C bus for connected devices and return list of addresses.
    :return: A list of I2C addresses
    """
    with _bus_lock:
        addresses = []
        for addr in range(256):
            try:
                _bus.read_byte(addr)
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
        return _bus.read_byte_data(addr, reg)


def write_byte_register(addr: int, reg: int, value: int) -> None:
    """
    Write a byte register to the device at the specified address and register.
    :param addr: I2C device address
    :param reg: Register address
    :param value: Register value to write
    """
    with _bus_lock:
        _bus.write_byte_data(addr, reg, value)


def read_word_register(addr: int, reg: int) -> int:
    """
    Read a word register from the device at the specified address and register.
    :param addr: I2C device address
    :param reg: Register address
    :return: Register value
    """
    with _bus_lock:
        return _bus.read_word_data(addr, reg)


def write_word_register(addr: int, reg: int, value: int) -> None:
    """
    Write a word register to the device at the specified address and register.
    :param addr: I2C device address
    :param reg: Register address
    :param value: Register value to write
    :return:
    """
    with _bus_lock:
        _bus.write_word_data(addr, reg, value)


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
    def __init__(self, addr: int = MCP23017_ADDRESS, interrupt_gpio: int | None = None):
        """
        Custom GPIO interface using MCP23017 to create eight input pins and eight output pins.
        GPA0-7 is used as inputs.
        GPB0-7 is used as outputs.
        :param addr: I2C address
        :param interrupt_gpio: Raspberry Pi GPIO pin used as interrupt input for port A.
        """
        self.interrupt_gpio = interrupt_gpio
        self.addr = addr

        # INT pin as an open-drain output
        write_byte_register(self.addr, MCP23017_IOCON, 0x04)
        # GPA as inputs
        write_byte_register(self.addr, MCP23017_IODIRA, 0xFF)
        # Interrupt on change mode
        write_byte_register(self.addr, MCP23017_INTCONA, 0x00)
        # Disable all interrupts
        write_byte_register(self.addr, MCP23017_GPINTENA, 0x00)
        # Use 0 as default
        write_byte_register(self.addr, MCP23017_DEFVALA, 0x00)

        # GPB as outputs
        write_byte_register(self.addr, MCP23017_IODIRB, 0x00)

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
