from __future__ import annotations

from blinker import Namespace

_signals = Namespace()

# Mode signals issued by button press or MQTT message
mode_idle = _signals.signal("mode_idle")
mode_cooling_down = _signals.signal("mode_cooling_down")
mode_cool = _signals.signal("mode_cool")

# Status signals issued by the fridge
cooler_started = _signals.signal("cooler_started")
cooler_stopped = _signals.signal("cooler_stopped")
heater_started = _signals.signal("heater_started")
heater_stopped = _signals.signal("heater_stopped")
fan_started = _signals.signal("fan_started")
fan_stopped = _signals.signal("fan_stopped")
