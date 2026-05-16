from __future__ import annotations

from blinker import Namespace

_signals = Namespace()

mode_idle = _signals.signal("mode_idle")
mode_cooling = _signals.signal("mode_cooling_down")
mode_warming = _signals.signal("mode_cool")

cooler_started = _signals.signal("cooler_started")
cooler_stopped = _signals.signal("cooler_stopped")
heater_started = _signals.signal("heater_started")
heater_stopped = _signals.signal("heater_stopped")
fan_started = _signals.signal("fan_started")
fan_stopped = _signals.signal("fan_stopped")
