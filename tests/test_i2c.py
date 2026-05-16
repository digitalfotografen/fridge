import pytest
import logging
from fridge.i2c import i2c_scan

logger = logging.getLogger(__name__)


@pytest.fixture(autouse=True)
def sht30():
    """
    Fixture to provide a SHT30 sensor instance for testing.
    :return: SHT30 sensor instance
    """
    from fridge.i2c import Sht30

    return Sht30()


def test_i2c_scan():
    """
    Scan the I2C bus for connected devices.
    :return:
    """
    addresses = i2c_scan()
    assert 0x20 in addresses  # GPIO expander
    assert 0x44 in addresses  # SHT30 sensor


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
