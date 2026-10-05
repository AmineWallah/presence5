"""Talking to the presence payload running on the PS5."""
import json
import logging
import socket
from dataclasses import dataclass

log = logging.getLogger(__name__)


# Classes
@dataclass
class GameState:
    title_id: str
    elapsed: int


class ConsoleUnreachable(Exception): pass


# Functions
def parse_game(message: list[str]) -> GameState | None:
    if not message:
        raise ConsoleUnreachable
    if message[0] == "IDLE":
        return None
    elif message[0] == "GAME":
        return GameState(message[1], int(message[2]))
    else:
        raise ConsoleUnreachable


def fetch_state(host: str, port: int = 8000) -> GameState | None:
    try:
        with socket.create_connection((host, port), timeout=3) as sock:
            message = sock.makefile("r").readline().split()
            state = parse_game(message)
            log.debug("polled %s:%s -> %s", host, port, state)
            return state
    except OSError as e:
        raise ConsoleUnreachable from e


def fetch_param(host: str, port: int = 8000) -> dict | None:
    """Ask the payload for the running game's param.json.

    Returns None when the console sent no file, which is the normal case for
    PS4 titles.
    """
    try:
        with socket.create_connection((host, port), timeout=3) as sock:
            sock.sendall(b"PARAM\n")
            data = sock.makefile("r", encoding="utf-8").read()
            line, _, rest = data.partition("\n")
            log.debug("PARAM reply: state line %r, %d bytes of JSON", line, len(rest))
            try:
                return json.loads(rest)
            except json.JSONDecodeError:
                return None

    except OSError as e:
        raise ConsoleUnreachable from e


if __name__ == "__main__":
    # Quick standalone check against the console.
    logging.basicConfig(level=logging.DEBUG)
    print(fetch_state("192.168.1.18"))