"""Tests for the local Unix-socket mode-control channel."""

from __future__ import annotations

import socket
import time
from pathlib import Path

import pytest

from fridge.enums import ModeState
from fridge.local_control import (
    DEFAULT_SOCKET_PATH,
    LocalControlServer,
    ModeCommandError,
    resolve_socket_path,
    send_mode,
)
from fridge.signals import mode_cool, mode_cooling_down, mode_idle, mode_special


@pytest.fixture
def socket_path(tmp_path: Path) -> Path:
    return tmp_path / "fridge.sock"


@pytest.fixture
def running_server(socket_path: Path):
    server = LocalControlServer(socket_path=str(socket_path))
    server.start()
    # Give the background thread a moment to bind.
    for _ in range(50):
        if socket_path.exists():
            break
        time.sleep(0.01)
    yield server
    server.stop()


def test_resolve_socket_path_expands_home() -> None:
    resolved = resolve_socket_path(DEFAULT_SOCKET_PATH)
    assert resolved.endswith("/fridge_data/fridge.sock")
    assert "~" not in resolved


def test_server_creates_socket_file(running_server: LocalControlServer, socket_path: Path) -> None:
    assert socket_path.exists()


def test_server_stop_removes_socket_file(socket_path: Path) -> None:
    server = LocalControlServer(socket_path=str(socket_path))
    server.start()
    assert socket_path.exists()
    server.stop()
    assert not socket_path.exists()


def test_server_stop_is_idempotent(socket_path: Path) -> None:
    server = LocalControlServer(socket_path=str(socket_path))
    server.stop()  # never started
    server.start()
    server.stop()
    server.stop()  # already stopped


def test_server_removes_stale_socket_on_start(socket_path: Path) -> None:
    socket_path.write_text("")  # stale file where socket should be
    server = LocalControlServer(socket_path=str(socket_path))
    try:
        server.start()
        assert socket_path.exists()
    finally:
        server.stop()


@pytest.mark.parametrize(
    "cli_arg, expected_state, expected_signal",
    [
        ("IDLE", ModeState.IDLE, mode_idle),
        ("COOL_DOWN", ModeState.COOL_DOWN, mode_cooling_down),
        ("COOL", ModeState.COOL, mode_cool),
        ("SPECIAL", ModeState.SPECIAL, mode_special),
    ],
)
def test_send_mode_emits_matching_signal(
    running_server: LocalControlServer,
    socket_path: Path,
    cli_arg: str,
    expected_state: ModeState,
    expected_signal,
) -> None:
    received: list[str] = []

    def _listener(sender, **_kwargs) -> None:
        received.append(sender if isinstance(sender, str) else "unknown")

    expected_signal.connect(_listener)
    try:
        reply = send_mode(cli_arg, socket_path=str(socket_path))
    finally:
        expected_signal.disconnect(_listener)

    assert reply == f"OK {expected_state.name}"
    assert received == ["local_control"]


def test_send_mode_is_case_insensitive(
    running_server: LocalControlServer, socket_path: Path
) -> None:
    reply = send_mode("cool", socket_path=str(socket_path))
    assert reply == "OK COOL"


def test_send_mode_unknown_mode_raises(
    running_server: LocalControlServer, socket_path: Path
) -> None:
    with pytest.raises(ModeCommandError, match="unknown-mode"):
        send_mode("FROZEN", socket_path=str(socket_path))


def test_send_mode_no_daemon_running(tmp_path: Path) -> None:
    missing = tmp_path / "not_a_socket.sock"
    with pytest.raises(ModeCommandError, match="not running"):
        send_mode("IDLE", socket_path=str(missing))


def test_server_rejects_invalid_command(
    running_server: LocalControlServer, socket_path: Path
) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(2.0)
        sock.connect(str(socket_path))
        sock.sendall(b"garbage\n")
        reply = sock.makefile("r", encoding="utf-8").readline().strip()
    assert reply.startswith("ERR")


def test_server_rejects_unknown_command(
    running_server: LocalControlServer, socket_path: Path
) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(2.0)
        sock.connect(str(socket_path))
        sock.sendall(b"REBOOT NOW\n")
        reply = sock.makefile("r", encoding="utf-8").readline().strip()
    assert reply.startswith("ERR unknown-command")
