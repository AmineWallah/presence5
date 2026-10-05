import socket
import select
import json
import struct
import os
import uuid
from typing import Any
from enum import IntEnum
import logging
import os

log = logging.getLogger(__name__)

APP_ID = "1556265892646494298"


class Op(IntEnum):
    HANDSHAKE = 0
    FRAME = 1
    CLOSE = 2


class DiscordUnavailable(Exception): pass


class DiscordIPC:
    def __init__(self, client_id: str, path: str):
        self.client_id = client_id
        self.path = path
        self.sock = None

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.connect(self.path)

            handshake_data = {"v": 1, "client_id": self.client_id}
            send_frame(self.sock, Op.HANDSHAKE, handshake_data)
            opcode, data = recv_frame(self.sock)
        except (OSError, ValueError) as e:
            self.close()
            raise DiscordUnavailable from e

        if data.get("evt") != "READY":
            log.debug("handshake rejected: %s", data)
            self.close()
            raise DiscordUnavailable

    def is_alive(self) -> bool:
        if self.sock is None:
            return False
        try:
            readable, _, _ = select.select([self.sock], [], [], 0)
            if not readable:
                return True
            return self.sock.recv(1, socket.MSG_PEEK) != b""
        except OSError:
            return False

    def set_activity(self, activity: dict | None) -> None:
        if self.sock is not None:
            activity_data = {
                "cmd": "SET_ACTIVITY",
                "args": {
                        "pid": os.getpid(),
                        "activity": activity,
                    },
                "nonce": str(uuid.uuid4()),
            }
            try:
                send_frame(self.sock, Op.FRAME, activity_data)
                opcode, reply = recv_frame(self.sock)
            except (OSError, ValueError) as e:
                self.close()
                log.debug("SET_ACTIVITY failed: %r", e)
                raise DiscordUnavailable from e

            if reply.get("evt") == "ERROR":
                log.warning("Discord rejected the activity: %s", reply.get("data"))
            else:
                log.debug("SET_ACTIVITY reply: %s", reply)
        else:
            raise DiscordUnavailable

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None


def recv_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Discord closed the connection")
        buf += chunk
    return buf


def send_frame(sock: socket.socket, opcode: int, data: dict[str, Any]) -> None:
    payload = json.dumps(data).encode("utf-8")
    header = struct.pack("<II", opcode, len(payload))
    sock.sendall(header + payload)


def recv_frame(sock: socket.socket) -> tuple[int, dict[str, Any]]:
    header = recv_exact(sock, 8)
    opcode, length = struct.unpack("<II", header)
    payload = recv_exact(sock, length)
    return opcode, json.loads(payload.decode("utf-8"))

def default_discord_path() -> str:
    if os.environ.get("XDG_RUNTIME_DIR"):
        return f"{os.environ.get('XDG_RUNTIME_DIR')}/discord-ipc-0"
    else:
        return f"/tmp/discord-ipc-0"