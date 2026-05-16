from fridge.i2c import i2c_scan


def test_i2c_scan():
    """
    Scan the I2C bus for connected devices.
    :return:
    """
    addresses = i2c_scan()
    assert 0x20 in addresses  # GPIO expander
    assert 0x44 in addresses  # SHT30 sensor


def test_read_byte_register():
    """
    Read a byte from a MCP23017 configuration register.
    :return:
    """
    pass


def test_write_byte_register():
    pass


def test_read_word_register():
    """
    Read a word from a SHT30 status register.
    :return:
    """
    pass


def test_write_word_register():
    """
    Write a SoftReset to the SHT30 semsor.
    :return:
    """
    pass
