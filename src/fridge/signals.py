from __future__ import annotations

from blinker import Namespace

_signals = Namespace()

# Mode signals issued by button press or MQTT message
mode_idle = _signals.signal("mode_idle")
mode_cooling_down = _signals.signal("mode_cooling_down")
mode_cool = _signals.signal("mode_cool")
mode_special = _signals.signal("mode_special")


# State change signals issued by the thermostat
# Senders pass the new state value as keyword argument ``state``.
mode_state_changed = _signals.signal("mode_state_changed")
action_state_changed = _signals.signal("action_state_changed")


# Status signals issued by the fridge
cooler_started = _signals.signal("cooler_started")
cooler_stopped = _signals.signal("cooler_stopped")
heater_started = _signals.signal("heater_started")
heater_stopped = _signals.signal("heater_stopped")
fan_started = _signals.signal("fan_started")
fan_stopped = _signals.signal("fan_stopped")
energy_state_changed = _signals.signal("energy_state_changed")
