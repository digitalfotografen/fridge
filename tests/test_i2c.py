from fridge.i2c import i2c_scan


def test_i2c_scan():
    addresses = i2c_scan()
    assert len(addresses) > 0
