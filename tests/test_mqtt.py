"""
Tests for the MQTT bridge.

The MQTT bridge connects the fridge controller to an MQTT broker.

- It publishes mode/action/energy state changes on the corresponding topics.
- It publishes measured temperature/humidity and current target temperature/humidity
  at a configurable interval.
- It subscribes to a command topic and emits the same blinker signals that
  the physical buttons use, so the LEDs and thermostat react identically.
"""

from __future__ import annotations

from configparser import ConfigParser
from unittest.mock import MagicMock

import pytest

from fridge import (
    ActionState,
    EnergyState,
    ModeState,
    action_state_changed,
    energy_state_changed,
    mode_cool,
    mode_cooling_down,
    mode_idle,
    mode_state_changed,
)
from fridge.mqtt import MqttBridge


PREFIX = "fridge/"


def _make_config() -> ConfigParser:
    cfg = ConfigParser()
    cfg["MQTT"] = {
        "mqtt_broker": "mqtt.local",
        "mqtt_port": "1883",
        "mqtt_topic_prefix": PREFIX,
        "user": "fridge",
        "update_interval": "60",
    }
    return cfg


class _FakeThresholds:
    def __init__(self, target_temperature: float | None, target_humidity: float | None):
        self.target_temperature = target_temperature
        self.target_humidity = target_humidity


class _FakeThermostat:
    def __init__(
        self,
        temperature: float = 5.1,
        humidity: float = 60.2,
        mode: ModeState = ModeState.COOL,
        action: ActionState = ActionState.IDLE,
        energy: EnergyState = EnergyState.NORMAL_PRICE,
        target_temperature: float | None = 5.0,
        target_humidity: float | None = 55.0,
    ):
        self._temperature = temperature
        self._humidity = humidity
        self._mode = mode
        self._action = action
        self._energy = energy
        self.thresholds = _FakeThresholds(target_temperature, target_humidity)

    @property
    def temperature(self) -> float:
        return self._temperature

    @property
    def humidity(self) -> float:
        return self._humidity

    def get_mode(self) -> ModeState:
        return self._mode

    def get_action_state(self) -> ActionState:
        return self._action

    def get_energy_state(self) -> EnergyState:
        return self._energy


@pytest.fixture
def mock_client_factory(mocker):
    """Patch paho mqtt Client and capture the instance created by the bridge."""
    instance = MagicMock(name="MqttClient")
    instance.connect.return_value = 0
    factory = mocker.patch("fridge.mqtt.mqtt_client.Client", return_value=instance)
    return factory, instance


def _published(client: MagicMock) -> dict[str, tuple[str, bool]]:
    """Return dict of topic -> (payload, retain) from publish calls."""
    out: dict[str, tuple[str, bool]] = {}
    for call in client.publish.call_args_list:
        args, kwargs = call
        topic = args[0] if args else kwargs["topic"]
        payload = args[1] if len(args) > 1 else kwargs.get("payload")
        retain = kwargs.get("retain", False)
        if len(args) > 3:
            retain = args[3]
        out[topic] = (str(payload), retain)
    return out


def test_start_connects_with_username_only(mock_client_factory):
    _factory, client = mock_client_factory
    bridge = MqttBridge(_FakeThermostat(), _make_config())
    bridge.start()

    client.username_pw_set.assert_called_once_with("fridge", password=None)
    client.connect.assert_called_once()
    args, kwargs = client.connect.call_args
    assert args[0] == "mqtt.local"
    assert args[1] == 1883
    client.loop_start.assert_called_once()
    bridge.stop()


def test_start_sets_will_offline_and_publishes_online(mock_client_factory):
    _factory, client = mock_client_factory
    bridge = MqttBridge(_FakeThermostat(), _make_config())
    bridge.start()

    # LWT configured before connect
    client.will_set.assert_called_once()
    will_args, will_kwargs = client.will_set.call_args
    topic = will_args[0] if will_args else will_kwargs["topic"]
    payload = will_args[1] if len(will_args) > 1 else will_kwargs.get("payload")
    assert topic == f"{PREFIX}state/online"
    assert "offline" in str(payload)
    assert will_kwargs.get("retain", False) is True

    # Simulate broker connection callback
    bridge._on_connect(client, None, {}, 0)
    published = _published(client)
    assert published[f"{PREFIX}state/online"] == ("online", True)
    bridge.stop()


def test_on_connect_subscribes_to_command_topic(mock_client_factory):
    _factory, client = mock_client_factory
    bridge = MqttBridge(_FakeThermostat(), _make_config())
    bridge.start()

    bridge._on_connect(client, None, {}, 0)
    subscribed_topics = [call.args[0] for call in client.subscribe.call_args_list]
    assert f"{PREFIX}cmd/mode" in subscribed_topics
    bridge.stop()


def test_on_connect_publishes_current_state(mock_client_factory):
    _factory, client = mock_client_factory
    bridge = MqttBridge(
        _FakeThermostat(
            mode=ModeState.COOL,
            action=ActionState.COOLING,
            energy=EnergyState.HIGH_PRICE,
        ),
        _make_config(),
    )
    bridge.start()
    bridge._on_connect(client, None, {}, 0)

    p = _published(client)
    assert p[f"{PREFIX}state/mode"] == ("COOL", True)
    assert p[f"{PREFIX}state/action"] == ("COOLING", True)
    assert p[f"{PREFIX}state/energy"] == ("HIGH_PRICE", True)
    bridge.stop()


def test_mode_state_change_is_published(mock_client_factory):
    _factory, client = mock_client_factory
    bridge = MqttBridge(_FakeThermostat(), _make_config())
    bridge.start()
    client.publish.reset_mock()

    mode_state_changed.send(object(), state=ModeState.COOL_DOWN)

    p = _published(client)
    assert p[f"{PREFIX}state/mode"] == ("COOL_DOWN", True)
    bridge.stop()


def test_action_state_change_is_published(mock_client_factory):
    _factory, client = mock_client_factory
    bridge = MqttBridge(_FakeThermostat(), _make_config())
    bridge.start()
    client.publish.reset_mock()

    action_state_changed.send(object(), state=ActionState.HEATING)

    p = _published(client)
    assert p[f"{PREFIX}state/action"] == ("HEATING", True)
    bridge.stop()


def test_energy_state_change_is_published(mock_client_factory):
    _factory, client = mock_client_factory
    bridge = MqttBridge(_FakeThermostat(), _make_config())
    bridge.start()
    client.publish.reset_mock()

    energy_state_changed.send(object(), state=EnergyState.LOW_PRICE)

    p = _published(client)
    assert p[f"{PREFIX}state/energy"] == ("LOW_PRICE", True)
    bridge.stop()


def test_publish_measurements_publishes_single_json_status(mock_client_factory):
    """The periodic update must be a single JSON payload on ``status``."""
    import json

    _factory, client = mock_client_factory
    thermostat = _FakeThermostat(
        temperature=4.2,
        humidity=58.6,
        mode=ModeState.COOL,
        action=ActionState.COOLING,
        energy=EnergyState.HIGH_PRICE,
        target_temperature=5.0,
        target_humidity=55.0,
    )
    bridge = MqttBridge(thermostat, _make_config())
    bridge.start()
    client.publish.reset_mock()

    bridge.publish_measurements()

    p = _published(client)
    assert f"{PREFIX}status" in p
    raw, retain = p[f"{PREFIX}status"]
    assert retain is True
    data = json.loads(raw)
    assert data == {
        "mode": "COOL",
        "action": "COOLING",
        "energy": "HIGH_PRICE",
        "temperature": 4.2,
        "humidity": 58.6,
        "target_temperature": 5.0,
        "target_humidity": 55.0,
        "price": None,
        "price_currency": None,
    }
    # No per-value topics anymore.
    assert f"{PREFIX}measurement/temperature" not in p
    assert f"{PREFIX}measurement/humidity" not in p
    assert f"{PREFIX}target/temperature" not in p
    assert f"{PREFIX}target/humidity" not in p
    bridge.stop()


def test_publish_measurements_includes_current_price(mock_client_factory):
    """When an energy database is provided, the status payload exposes the
    current 15-minute Tibber price and currency."""
    import json
    from datetime import UTC, datetime

    from fridge.energy_db import EnergyDatabase, PriceSlot

    _factory, client = mock_client_factory
    database = EnergyDatabase(":memory:")
    try:
        now = datetime.now(tz=UTC).replace(second=0, microsecond=0)
        slot_start = now.replace(minute=(now.minute // 15) * 15)
        database.upsert_prices([PriceSlot(starts_at=slot_start, total=1.2345, currency="SEK")])
        bridge = MqttBridge(_FakeThermostat(), _make_config(), energy_database=database)
        bridge.start()
        client.publish.reset_mock()

        bridge.publish_measurements()

        p = _published(client)
        data = json.loads(p[f"{PREFIX}status"][0])
        assert data["price"] == 1.2345
        assert data["price_currency"] == "SEK"
        bridge.stop()
    finally:
        database.close()


def test_publish_measurements_reports_none_targets_as_null(mock_client_factory):
    import json

    _factory, client = mock_client_factory
    thermostat = _FakeThermostat(target_temperature=None, target_humidity=None)
    bridge = MqttBridge(thermostat, _make_config())
    bridge.start()
    client.publish.reset_mock()

    bridge.publish_measurements()

    p = _published(client)
    data = json.loads(p[f"{PREFIX}status"][0])
    assert data["target_temperature"] is None
    assert data["target_humidity"] is None
    bridge.stop()


@pytest.mark.parametrize(
    "payload,expected_signal",
    [
        ("IDLE", mode_idle),
        ("COOL_DOWN", mode_cooling_down),
        ("COOL", mode_cool),
    ],
)
def test_command_message_emits_button_signal(mock_client_factory, payload, expected_signal):
    """
    Incoming command messages must emit the same blinker signals as buttons,
    so the LEDs and the thermostat react in exactly the same way.
    """
    _factory, client = mock_client_factory
    bridge = MqttBridge(_FakeThermostat(), _make_config())
    bridge.start()

    received: list[object] = []

    def listener(sender, **_kwargs):
        received.append(sender)

    expected_signal.connect(listener)
    try:
        msg = MagicMock()
        msg.topic = f"{PREFIX}cmd/mode"
        msg.payload = payload.encode()
        bridge._on_message(client, None, msg)
        assert len(received) == 1
    finally:
        expected_signal.disconnect(listener)
    bridge.stop()


def test_command_message_with_unknown_payload_is_ignored(mock_client_factory):
    _factory, client = mock_client_factory
    bridge = MqttBridge(_FakeThermostat(), _make_config())
    bridge.start()

    triggered: list[str] = []
    for sig, name in (
        (mode_idle, "idle"),
        (mode_cooling_down, "cool_down"),
        (mode_cool, "cool"),
    ):
        sig.connect(lambda sender, n=name, **_kw: triggered.append(n))

    msg = MagicMock()
    msg.topic = f"{PREFIX}cmd/mode"
    msg.payload = b"BOGUS"
    bridge._on_message(client, None, msg)

    assert triggered == []
    bridge.stop()


def test_stop_disconnects_and_stops_loop(mock_client_factory):
    _factory, client = mock_client_factory
    bridge = MqttBridge(_FakeThermostat(), _make_config())
    bridge.start()
    bridge.stop()

    client.loop_stop.assert_called_once()
    client.disconnect.assert_called_once()


def test_start_failure_is_logged_and_does_not_raise(mock_client_factory, caplog):
    """Application must keep running if the broker is unreachable."""
    _factory, client = mock_client_factory
    client.connect.side_effect = OSError("no broker")

    bridge = MqttBridge(_FakeThermostat(), _make_config())
    with caplog.at_level("WARNING"):
        bridge.start()  # must not raise

    assert any("MQTT" in rec.message or "broker" in rec.message for rec in caplog.records)
    bridge.stop()


def test_periodic_publisher_calls_publish_measurements(mock_client_factory, mocker):
    _factory, _client = mock_client_factory
    bridge = MqttBridge(_FakeThermostat(), _make_config())
    spy = mocker.spy(bridge, "publish_measurements")

    # Run one tick of the periodic loop using a stub event
    bridge._publish_one_tick()

    assert spy.call_count == 1
