"""
MQTT bridge for the fridge controller.

The :class:`MqttBridge` connects the fridge controller to an MQTT broker.

It performs three jobs:

1. Publish state changes (mode, action and energy) as they happen.
2. Publish measured and target temperature/humidity at a configurable
   interval so that consumers can plot how the target is adjusted by the
   energy state.
3. Listen for commands and emit the same blinker signals that the physical
   buttons emit, so that LEDs and thermostat react identically.

Topic layout (under ``mqtt_topic_prefix``)::

    state/online          -> "online" / "offline"  (retained, LWT)
    state/mode            -> ModeState.name        (retained)
    state/action          -> ActionState.name      (retained)
    state/energy          -> EnergyState.name      (retained)
    measurement/temperature
    measurement/humidity
    target/temperature
    target/humidity
    cmd/mode              -> IDLE | COOL_DOWN | COOL (subscribed)

The bridge fails soft: if the broker cannot be reached, the rest of the
application keeps running and the bridge reconnects automatically as soon
as the broker becomes available again.
"""

from __future__ import annotations

import json
import logging
import threading
from configparser import ConfigParser
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import paho.mqtt.client as mqtt_client

from fridge.enums import ActionState, EnergyState, ModeState
from fridge.signals import (
    action_state_changed,
    energy_state_changed,
    mode_cool,
    mode_cooling_down,
    mode_idle,
    mode_special,
    mode_state_changed,
)

if TYPE_CHECKING:
    from fridge.energy_db import EnergyDatabase
    from fridge.thermostat import Thermostat

logger = logging.getLogger(__name__)


DEFAULT_BROKER = "localhost"
DEFAULT_PORT = 1883
DEFAULT_PREFIX = "fridge/"
DEFAULT_UPDATE_INTERVAL = 60
DEFAULT_QOS = 1


_MODE_BY_NAME = {state.name: state for state in ModeState}
_MODE_SIGNAL = {
    ModeState.IDLE: mode_idle,
    ModeState.COOL_DOWN: mode_cooling_down,
    ModeState.COOL: mode_cool,
    ModeState.SPECIAL: mode_special,
}


class MqttBridge:
    """
    Bridge between the fridge controller and an MQTT broker.

    :param thermostat: Thermostat instance to read measurements and state from
    :param config: Application configuration (uses ``[MQTT]`` section)
    :param energy_database: Optional :class:`~fridge.energy_db.EnergyDatabase`
        used to look up the current Tibber price for the status payload. When
        ``None`` the ``price`` and ``price_currency`` fields are reported as
        ``null``.
    """

    def __init__(
        self,
        thermostat: Thermostat,
        config: ConfigParser,
        *,
        energy_database: EnergyDatabase | None = None,
    ):
        self._thermostat = thermostat
        self._energy_database = energy_database
        self._broker = config.get("MQTT", "mqtt_broker", fallback=DEFAULT_BROKER)
        self._port = config.getint("MQTT", "mqtt_port", fallback=DEFAULT_PORT)
        self._prefix = config.get("MQTT", "mqtt_topic_prefix", fallback=DEFAULT_PREFIX)
        if not self._prefix.endswith("/"):
            self._prefix += "/"
        self._user = config.get("MQTT", "user", fallback=None) or None
        self._password = config.get("MQTT", "password", fallback=None) or None
        self._update_interval = config.getint(
            "MQTT", "update_interval", fallback=DEFAULT_UPDATE_INTERVAL
        )

        # paho-mqtt v2 introduced the CallbackAPIVersion argument. Use it when
        # available, but fall back to the v1 signature so we work with whatever
        # version is installed on the Raspberry Pi.
        try:
            self._client = mqtt_client.Client(
                callback_api_version=mqtt_client.CallbackAPIVersion.VERSION1
            )
        except (AttributeError, TypeError):
            self._client = mqtt_client.Client()

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

        self._stop_event = threading.Event()
        self._publisher_thread: threading.Thread | None = None
        self._started = False

        # Topics
        self._topic_online = f"{self._prefix}state/online"
        self._topic_mode = f"{self._prefix}state/mode"
        self._topic_action = f"{self._prefix}state/action"
        self._topic_energy = f"{self._prefix}state/energy"
        self._topic_status = f"{self._prefix}status"
        self._topic_cmd_mode = f"{self._prefix}cmd/mode"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """
        Connect to the broker and start background publisher/listener threads.

        Failures to connect are logged but do not raise — the rest of the
        application must keep running without network access.
        """
        if self._started:
            return
        self._started = True

        if self._user is not None:
            self._client.username_pw_set(self._user, password=self._password)

        self._client.will_set(self._topic_online, payload="offline", qos=DEFAULT_QOS, retain=True)

        # Subscribe to signals BEFORE connecting so that we don't miss
        # transitions that happen during start-up.
        mode_state_changed.connect(self._on_mode_state_changed)
        action_state_changed.connect(self._on_action_state_changed)
        energy_state_changed.connect(self._on_energy_state_changed)

        try:
            self._client.connect(self._broker, self._port, keepalive=60)
            self._client.loop_start()
        except OSError as exc:
            logger.warning(
                "Could not connect to MQTT broker %s:%s (%s); continuing without MQTT",
                self._broker,
                self._port,
                exc,
            )

        self._stop_event.clear()
        self._publisher_thread = threading.Thread(
            target=self._publisher_loop, name="MqttPublisher", daemon=True
        )
        self._publisher_thread.start()

    def stop(self) -> None:
        """Disconnect from the broker and stop background threads."""
        if not self._started:
            return
        self._started = False
        self._stop_event.set()

        mode_state_changed.disconnect(self._on_mode_state_changed)
        action_state_changed.disconnect(self._on_action_state_changed)
        energy_state_changed.disconnect(self._on_energy_state_changed)

        try:
            # Publish offline before disconnecting (LWT only fires on ungraceful
            # disconnects).
            self._client.publish(
                self._topic_online, payload="offline", qos=DEFAULT_QOS, retain=True
            )
            self._client.loop_stop()
            self._client.disconnect()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Error stopping MQTT client: %s", exc)

        if self._publisher_thread is not None:
            self._publisher_thread.join(timeout=1)
            self._publisher_thread = None

    # ------------------------------------------------------------------
    # paho-mqtt callbacks
    # ------------------------------------------------------------------
    def _on_connect(self, client, _userdata, _flags, rc, *_args, **_kwargs) -> None:
        if rc != 0:
            logger.warning("MQTT connection failed with code %s", rc)
            return
        logger.info("MQTT connected to %s:%s", self._broker, self._port)

        client.publish(self._topic_online, payload="online", qos=DEFAULT_QOS, retain=True)
        client.subscribe(self._topic_cmd_mode, qos=DEFAULT_QOS)

        # Re-publish current state so a fresh subscriber sees it immediately
        # (also useful after a reconnect).
        self._publish_state(self._topic_mode, self._thermostat.get_mode().name)
        self._publish_state(self._topic_action, self._thermostat.get_action_state().name)
        self._publish_state(self._topic_energy, self._thermostat.get_energy_state().name)

    def _on_disconnect(self, _client, _userdata, rc, *_args, **_kwargs) -> None:
        if rc != 0:
            logger.warning("MQTT disconnected unexpectedly (rc=%s)", rc)
        else:
            logger.info("MQTT disconnected")

    def _on_message(self, _client, _userdata, msg) -> None:
        try:
            payload = msg.payload.decode("utf-8").strip()
        except (UnicodeDecodeError, AttributeError):
            logger.warning("Ignoring non-text MQTT payload on %s", msg.topic)
            return

        if msg.topic == self._topic_cmd_mode:
            self._handle_mode_command(payload)
        else:
            logger.debug("Ignoring message on unhandled topic %s", msg.topic)

    def _handle_mode_command(self, payload: str) -> None:
        mode = _MODE_BY_NAME.get(payload.upper())
        if mode is None:
            logger.warning("Ignoring unknown mode command payload: %r", payload)
            return
        logger.info("MQTT mode command received: %s", mode.name)
        # Emit the same signal a button press would emit. The application
        # wires those signals to both the LEDs and the thermostat, so this
        # keeps everything in sync.
        _MODE_SIGNAL[mode].send(self)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------
    def _on_mode_state_changed(self, _sender, state: ModeState, **_kwargs) -> None:
        self._publish_state(self._topic_mode, state.name)

    def _on_action_state_changed(self, _sender, state: ActionState, **_kwargs) -> None:
        self._publish_state(self._topic_action, state.name)

    def _on_energy_state_changed(self, _sender, state: EnergyState, **_kwargs) -> None:
        self._publish_state(self._topic_energy, state.name)

    # ------------------------------------------------------------------
    # Publishing helpers
    # ------------------------------------------------------------------
    def _publish_state(self, topic: str, payload: str) -> None:
        try:
            self._client.publish(topic, payload, qos=DEFAULT_QOS, retain=True)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Failed to publish %s: %s", topic, exc)

    def publish_measurements(self) -> None:
        """
        Publish a combined status snapshot as a single JSON payload.

        The payload contains the current mode/action/energy state, the
        measured temperature and humidity, and the active target
        temperature/humidity. Targets that are not active in the current
        mode are reported as ``null``.
        """
        payload = self._build_status_payload()
        try:
            self._client.publish(
                self._topic_status,
                json.dumps(payload),
                qos=0,
                retain=True,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Failed to publish status: %s", exc)

    def _build_status_payload(self) -> dict:
        """Build the JSON-serialisable status snapshot."""
        thresholds = getattr(self._thermostat, "thresholds", None)
        target_temperature: float | None = None
        target_humidity: float | None = None
        if thresholds is not None:
            target_temperature = thresholds.target_temperature
            target_humidity = thresholds.target_humidity

        price: float | None = None
        price_currency: str | None = None
        if self._energy_database is not None:
            try:
                slot = self._energy_database.get_price_at(datetime.now(tz=UTC))
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Failed to read current price: %s", exc)
                slot = None
            if slot is not None:
                price = round(slot.total, 4)
                price_currency = slot.currency or None

        return {
            "mode": self._thermostat.get_mode().name,
            "action": self._thermostat.get_action_state().name,
            "energy": self._thermostat.get_energy_state().name,
            "temperature": round(self._thermostat.temperature, 1),
            "humidity": round(self._thermostat.humidity, 1),
            "target_temperature": (
                round(target_temperature, 1) if target_temperature is not None else None
            ),
            "target_humidity": (round(target_humidity, 1) if target_humidity is not None else None),
            "price": price,
            "price_currency": price_currency,
        }

    # ------------------------------------------------------------------
    # Periodic publisher thread
    # ------------------------------------------------------------------
    def _publish_one_tick(self) -> None:
        """One iteration of the publisher loop (extracted for testing)."""
        self.publish_measurements()

    def _publisher_loop(self) -> None:
        while not self._stop_event.is_set():
            self._publish_one_tick()
            if self._stop_event.wait(self._update_interval):
                break
