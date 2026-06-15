"""
Hardware test sequence for installation verification.

Runs a predefined sequence of hardware checks to verify that
all components (sensor, LEDs, relays) are functioning correctly.
"""

import logging
import time
from collections.abc import Callable

from fridge.i2c import I2cGpio, RelayGpio, Sht30

logger = logging.getLogger(__name__)

# LED bit positions on port B
_LED_BITS = [4, 5, 6]
_LED_MASK = (1 << 4) | (1 << 5) | (1 << 6)

# Running light duration
_RUNNING_LIGHT_DURATION = 5.0
_LED_ON_TIME = 1.0

# Hold durations for relay tests
_RELAY_HOLD_DURATION = 60.0

# LED bit aliases
_IDLE_LED_BIT = 4
_COOL_DOWN_LED_BIT = 5
_COOL_LED_BIT = 6


def _set_led_only(gpio: I2cGpio, led_bit: int | None) -> None:
    """
    Set a single LED on port B while preserving relay bits (0-2).

    Clears all LED bits first, then sets the specified LED if not None.

    :param gpio: I2cGpio instance
    :param led_bit: Bit position of the LED to activate, or None to clear all LEDs
    """
    current = gpio.read("B")
    new_value = current & ~_LED_MASK
    if led_bit is not None:
        new_value |= 1 << led_bit
    gpio.write("B", new_value)


def run_hardware_test(
    gpio: I2cGpio,
    sensor: Sht30,
    sleep_func: Callable[[float], None] = time.sleep,
) -> None:
    """
    Run the full hardware test sequence.

    :param gpio: Initialized I2cGpio instance
    :param sensor: Initialized Sht30 sensor instance
    :param sleep_func: Sleep function (injectable for testing)
    """
    relay = RelayGpio(gpio)

    try:
        # Step 1: Measure temperature and humidity
        logger.info("Step 1: Reading temperature and humidity sensor")
        temperature, humidity = sensor.read_data()
        logger.info(
            "Temperature: %.1f °C, Humidity: %.1f %%",
            temperature,
            humidity,
        )

        # Step 2: Running light over all LEDs for 5 seconds
        logger.info(
            "Step 2: Running light sequence over %d LEDs for %.0f seconds",
            len(_LED_BITS),
            _RUNNING_LIGHT_DURATION,
        )
        elapsed = 0.0
        while elapsed < _RUNNING_LIGHT_DURATION:
            for led_bit in _LED_BITS:
                if elapsed >= _RUNNING_LIGHT_DURATION:
                    break
                _set_led_only(gpio, led_bit)
                sleep_func(_LED_ON_TIME)
                elapsed += _LED_ON_TIME
        _set_led_only(gpio, None)

        # Step 3: Fan + Idle LED for 60 seconds
        logger.info(
            "Step 3: Vent fan relay ON + Idle LED ON, holding for %.0f seconds",
            _RELAY_HOLD_DURATION,
        )
        relay.set_vent_fan_relay(True)
        _set_led_only(gpio, _IDLE_LED_BIT)
        sleep_func(_RELAY_HOLD_DURATION)

        # Step 4: Fan off, Idle LED off -> Heater + Cool Down LED for 60 seconds
        logger.info(
            "Step 4: Heater relay ON + Cool Down LED ON, holding for %.0f seconds",
            _RELAY_HOLD_DURATION,
        )
        relay.set_vent_fan_relay(False)
        relay.set_heater_relay(True)
        _set_led_only(gpio, _COOL_DOWN_LED_BIT)
        sleep_func(_RELAY_HOLD_DURATION)

        # Step 5: Heater off, Cool Down LED off -> Cooler + Cool LED for 60 seconds
        logger.info(
            "Step 5: Cooler relay ON + Cool LED ON, holding for %.0f seconds",
            _RELAY_HOLD_DURATION,
        )
        relay.set_heater_relay(False)
        relay.set_cooler_relay(True)
        _set_led_only(gpio, _COOL_LED_BIT)
        sleep_func(_RELAY_HOLD_DURATION)

        # Step 6: All off
        logger.info("Step 6: Switching all off and exiting")
        relay.set_cooler_relay(False)
        _set_led_only(gpio, None)

    except Exception:
        # Safety: turn everything off on error
        logger.exception("Hardware test failed, turning everything off")
        relay.set_cooler_relay(False)
        relay.set_heater_relay(False)
        relay.set_vent_fan_relay(False)
        _set_led_only(gpio, None)
        raise

    logger.info("Hardware test completed successfully")
