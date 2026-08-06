"""
Local IPC for controlling the running fridge daemon.

This module provides a small Unix-domain-socket server that runs inside
``fridge run`` and a matching client used by the ``fridge mode`` CLI
command. The server accepts a single-line text command per connection
(currently only ``MODE <NAME>``) and emits the same blinker signals a
physical button press or MQTT command would emit.

Using a local socket means the command works without network access or
an MQTT broker and integrates naturally with the existing signal-based
architecture.
"""

from __future__ import annotations

import logging
import os
import socket
import socketserver
import threading
from pathlib import Path

from fridge.enums import ModeState
from fridge.signals import (
    mode_cool,
    mode_cooling_down,
    mode_idle,
    mode_special,
)

logger = logging.getLogger(__name__)


DEFAULT_SOCKET_PATH = "~/fridge_data/fridge.sock"

_MODE_BY_NAME: dict[str, ModeState] = {state.name: state for state in ModeState}
_MODE_SIGNAL = {
    ModeState.IDLE: mode_idle,
    ModeState.COOL_DOWN: mode_cooling_down,
    ModeState.COOL: mode_cool,
    ModeState.SPECIAL: mode_special,
}


class ModeCommandError(RuntimeError):
    """Raised when the local mode command cannot be delivered."""


def resolve_socket_path(path: str | os.PathLike[str] = DEFAULT_SOCKET_PATH) -> str:
    """Expand ``~`` in the given socket path and return an absolute string."""
    return os.path.abspath(os.path.expanduser(os.fspath(path)))


def _parse_command(payload: str) -> tuple[str, str] | None:
    """Parse ``MODE <NAME>`` command, returning (command, argument) or None."""
    parts = payload.strip().split()
    if len(parts) != 2:
        return None
    return parts[0].upper(), parts[1].upper()


class _Handler(socketserver.StreamRequestHandler):
    """Handle a single client connection: one line in, one line out."""

    server: LocalControlServer  # type: ignore[assignment]

    def handle(self) -> None:  # noqa: D401 - stdlib API
        raw = self.rfile.readline()
        try:
            payload = raw.decode("utf-8")
        except UnicodeDecodeError:
            self._reply("ERR invalid-encoding")
            return

        parsed = _parse_command(payload)
        if parsed is None:
            self._reply("ERR invalid-command")
            return

        command, argument = parsed
        if command != "MODE":
            self._reply(f"ERR unknown-command {command}")
            return

        mode = _MODE_BY_NAME.get(argument)
        if mode is None:
            self._reply(f"ERR unknown-mode {argument}")
            return

        logger.info("Local CLI mode command received: %s", mode.name)
        _MODE_SIGNAL[mode].send("local_control")
        self._reply(f"OK {mode.name}")

    def _reply(self, message: str) -> None:
        try:
            self.wfile.write(f"{message}\n".encode())
        except OSError as exc:  # pragma: no cover - defensive
            logger.debug("Failed to reply to local client: %s", exc)


class _ThreadedUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class LocalControlServer:
    """
    Unix-domain-socket server accepting local mode commands.

    The server is started in a background daemon thread and stopped via
    :meth:`stop`. The socket file is created with ``0600`` permissions so
    only the owning user can send commands.

    :param socket_path: Filesystem path for the listening socket. ``~`` is
        expanded. Defaults to ``~/fridge_data/fridge.sock``.
    """

    def __init__(self, socket_path: str | os.PathLike[str] = DEFAULT_SOCKET_PATH) -> None:
        self.socket_path = resolve_socket_path(socket_path)
        self._server: _ThreadedUnixServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start listening for local commands."""
        if self._server is not None:
            return

        Path(self.socket_path).parent.mkdir(parents=True, exist_ok=True)
        # Remove stale socket left behind by a previous run/crash.
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

        self._server = _ThreadedUnixServer(self.socket_path, _Handler)
        try:
            os.chmod(self.socket_path, 0o600)
        except OSError as exc:  # pragma: no cover - defensive
            logger.debug("Could not chmod socket %s: %s", self.socket_path, exc)

        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="LocalControlServer",
            daemon=True,
        )
        self._thread.start()
        logger.info("Local control server listening on %s", self.socket_path)

    def stop(self) -> None:
        """Stop the server and remove the socket file."""
        if self._server is None:
            return
        try:
            self._server.shutdown()
            self._server.server_close()
        finally:
            self._server = None
            self._thread = None
            try:
                os.unlink(self.socket_path)
            except FileNotFoundError:
                pass


def send_mode(
    mode: str,
    *,
    socket_path: str | os.PathLike[str] = DEFAULT_SOCKET_PATH,
    timeout: float = 2.0,
) -> str:
    """
    Send a mode command to the running fridge daemon and return its reply.

    :param mode: Canonical mode name (``IDLE``, ``COOL_DOWN``, ``COOL``,
        ``SPECIAL``). Case-insensitive.
    :param socket_path: Path to the daemon's control socket.
    :param timeout: Socket timeout in seconds.
    :raises ModeCommandError: If the daemon is not running or the command
        was rejected.
    :return: The daemon's reply (e.g. ``OK COOL``).
    """
    resolved = resolve_socket_path(socket_path)
    payload = f"MODE {mode.upper()}\n".encode()

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            try:
                sock.connect(resolved)
            except (FileNotFoundError, ConnectionRefusedError) as exc:
                raise ModeCommandError(
                    f"fridge daemon not running (no socket at {resolved})"
                ) from exc
            sock.sendall(payload)
            reply = sock.makefile("r", encoding="utf-8").readline().strip()
    except OSError as exc:
        raise ModeCommandError(f"local control communication failed: {exc}") from exc

    if not reply:
        raise ModeCommandError("empty reply from daemon")
    if reply.startswith("ERR"):
        raise ModeCommandError(reply)
    return reply
