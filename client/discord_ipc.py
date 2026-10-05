import json
import logging
import os
import select
import socket
import struct
import sys
import uuid
from enum import IntEnum
from typing import Any

log = logging.getLogger(__name__)

APP_ID = "1556265892646494298"

IS_WINDOWS = sys.platform == "win32"


class Op(IntEnum):
    HANDSHAKE = 0
    FRAME = 1
    CLOSE = 2


class DiscordUnavailable(Exception): pass


class UnixTransport:
    """A Unix domain socket (Linux, macOS)."""

    def __init__(self, path: str):
        self.path = path
        self.sock: socket.socket | None = None

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)  # so a hung Discord can't block us forever
        try:
            sock.connect(self.path)
        except OSError:
            sock.close()
            raise
        self.sock = sock

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("Discord closed the connection")
            buf += chunk
        return buf

    def is_alive(self) -> bool:
        # A closed connection shows up as "readable" with nothing to read.
        # MSG_PEEK looks at the data without removing it.
        try:
            readable, _, _ = select.select([self.sock], [], [], 0)
            if not readable:
                return True
            return self.sock.recv(1, socket.MSG_PEEK) != b""
        except OSError:
            return False

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None


class WindowsTransport:
    """A named pipe (Windows), opened like a file."""

    def __init__(self, path: str):
        self.path = path
        self.pipe = None

    def connect(self) -> None:
        # buffering=0 gives a raw file object, so every write goes straight
        # to the pipe and reads return as soon as data is there.
        self.pipe = open(self.path, "r+b", buffering=0)

    def send(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = self.pipe.write(view)
            if not written:
                raise ConnectionError("Discord closed the connection")
            view = view[written:]

    def recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.pipe.read(n - len(buf))
            if not chunk:
                raise ConnectionError("Discord closed the connection")
            buf += chunk
        return buf

    def is_alive(self) -> bool:
        # PeekNamedPipe fails once the other end of the pipe has gone away.
        # It reads nothing, so it never interferes with real messages.
        try:
            import ctypes
            import msvcrt
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            peek = kernel32.PeekNamedPipe
            peek.argtypes = [
                wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                wintypes.LPDWORD, wintypes.LPDWORD, wintypes.LPDWORD,
            ]
            peek.restype = wintypes.BOOL

            handle = msvcrt.get_osfhandle(self.pipe.fileno())
            return bool(peek(handle, None, 0, None, None, None))
        except (OSError, ValueError):
            return False

    def close(self) -> None:
        if self.pipe is not None:
            try:
                self.pipe.close()
            except OSError:
                pass
            self.pipe = None


Transport = WindowsTransport if IS_WINDOWS else UnixTransport

def discord_path_candidates() -> list[str]:
    """Every place Discord's IPC channel may be, most likely first."""
    if IS_WINDOWS:
        return [rf"\\.\pipe\discord-ipc-{n}" for n in range(10)]

    base = (os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp")
    folders = [
        base,                                           # normal install
        os.path.join(base, "app", "com.discordapp.Discord"),  # Flatpak
        os.path.join(base, "snap.discord"),             # Snap
    ]
    return [os.path.join(folder, f"discord-ipc-{n}")
            for n in range(10) for folder in folders]


def default_discord_path() -> str:
    """The standard location, for showing to the user as the default."""
    return discord_path_candidates()[0]


class DiscordIPC:
    def __init__(self, client_id: str, path: str | None = None):
        """path=None means: try every known location on each connect."""
        self.client_id = client_id
        self.requested_path = path
        self.path = path or default_discord_path()  # the one in use / last tried
        self.transport = None

    @property
    def connected(self) -> bool:
        return self.transport is not None

    def connect(self) -> None:
        if self.requested_path:
            candidates = [self.requested_path]
        else:
            candidates = discord_path_candidates()

        last_error: Exception | None = None
        for path in candidates:
            transport = Transport(path)
            try:
                transport.connect()
                send_frame(transport, Op.HANDSHAKE, {"v": 1, "client_id": self.client_id})
                opcode, data = recv_frame(transport)
            except (OSError, ValueError) as e:
                # OSError: nothing there, refused, or closed mid-handshake.
                # ValueError: the answer wasn't JSON.
                transport.close()
                last_error = e
                continue

            if data.get("evt") != "READY":
                log.debug("handshake rejected at %s: %s", path, data)
                transport.close()
                last_error = None
                continue

            self.transport = transport
            self.path = path
            log.debug("Discord IPC connected at %s", path)
            return

        raise DiscordUnavailable from last_error

    def is_alive(self) -> bool:
        """Check whether Discord still holds its end of the connection."""
        return self.transport is not None and self.transport.is_alive()

    def set_activity(self, activity: dict | None) -> None:
        if self.transport is None:
            raise DiscordUnavailable

        activity_data = {
            "cmd": "SET_ACTIVITY",
            "args": {
                    "pid": os.getpid(),
                    "activity": activity,
                },
            "nonce": str(uuid.uuid4()),
        }
        try:
            send_frame(self.transport, Op.FRAME, activity_data)
            opcode, reply = recv_frame(self.transport)
        except (OSError, ValueError) as e:
            self.close()
            log.debug("SET_ACTIVITY failed: %r", e)
            raise DiscordUnavailable from e

        if reply.get("evt") == "ERROR":
            # Discord is up but refused this activity (a bad field, usually).
            log.warning("Discord rejected the activity: %s", reply.get("data"))
        else:
            log.debug("SET_ACTIVITY reply: %s", reply)

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None

def send_frame(transport, opcode: int, data: dict[str, Any]) -> None:
    payload = json.dumps(data).encode("utf-8")
    header = struct.pack("<II", opcode, len(payload))
    transport.send(header + payload)


def recv_frame(transport) -> tuple[int, dict[str, Any]]:
    header = transport.recv_exact(8)
    opcode, length = struct.unpack("<II", header)
    payload = transport.recv_exact(length)
    return opcode, json.loads(payload.decode("utf-8"))