from ps5 import fetch_state, GameState, ConsoleUnreachable, fetch_param
from titles import TitleInfo, title_from_param, lookup_ps4_title
from discord_ipc import DiscordIPC, DiscordUnavailable, APP_ID, default_discord_path
import time
import logging
import argparse
import ipaddress

log = logging.getLogger(__name__)

PAYLOAD_PORT = 8000  # Fixed in the PS5 payload


class PresenceClient:
    def __init__(self, host: str, port: int, discord_path: str | None = None):
        self.host = host
        self.port = port
        self.ipc = DiscordIPC(APP_ID, discord_path)
        self.previous_id = None
        self.desired = None
        self.synced = False
        self.discord_down = False
        self.title_cache = {}

        self.title_id: str | None = None
        self.title: TitleInfo | None = None
        self.started_at: int | None = None  # Unix time the game started

    @property
    def console_reachable(self) -> bool:
        return self.previous_id != "unreachable"

    @property
    def discord_connected(self) -> bool:
        return self.ipc.connected

    def resolve_title(self, title_id: str) -> TitleInfo | None:
        if title_id in self.title_cache:
            log.debug("title cache hit for %s", title_id)
            return self.title_cache[title_id]

        # 1. The game's own param.json, from the console (PS5 titles, homebrew).
        try:
            param = fetch_param(self.host, self.port)
        except ConsoleUnreachable:
            log.warning("could not fetch title info for %s, console unreachable", title_id)
            return None

        info = title_from_param(param) if param is not None else None
        source = "param.json"

        # 2. Sony's title metadata service (PS4 titles, which have no param.json).
        if info is None:
            log.debug("no param.json for %s, trying the metadata service", title_id)
            info = lookup_ps4_title(title_id)
            source = "metadata service"

        self.title_cache[title_id] = info

        if info is None:
            log.info("no title info for %s, using the title ID", title_id)
        else:
            log.info("resolved %s -> %s (from %s)", title_id, info.name, source)
        return info

    def step(self) -> None:
        # 1. What is the console doing?
        try:
            state = fetch_state(self.host, self.port)
            current_id = state.title_id if state else None

            if current_id != self.previous_id:
                if state is not None:
                    info = self.resolve_title(state.title_id)
                    log.info("game started: %s (%s, elapsed %ss)",
                             info.name if info else state.title_id, state.title_id, state.elapsed)
                    self.desired = build_activity(state, info)
                    self.synced = False
                    self.title_id = state.title_id
                    self.title = info
                    self.started_at = self.desired["timestamps"]["start"]
                else:
                    log.info("console idle, clearing presence")
                    self.desired = None
                    self.synced = False
                    self._clear_game()
                self.previous_id = current_id

        except ConsoleUnreachable:
            if self.previous_id != "unreachable":
                log.warning("console unreachable at %s:%s, clearing presence", self.host, self.port)
                self.desired = None
                self.synced = False
                self._clear_game()
            self.previous_id = "unreachable"

        # 2. Did Discord go away while nothing else changed?
        if self.synced and not self.ipc.is_alive():
            log.warning("lost the connection to Discord")
            self.ipc.close()
            self.synced = False

        # 3. Bring Discord in line with what should be showing.
        if not self.synced:
            try:
                if not self.ipc.connected:
                    self.ipc.connect()
                    log.info("connected to Discord at %s", self.ipc.path)
                self.ipc.set_activity(self.desired)
                self.synced = True
                self.discord_down = False
                log.debug("presence %s", "updated" if self.desired else "cleared")
            except DiscordUnavailable:
                if not self.discord_down:
                    log.warning("Discord is not reachable (%s). Is it running? Will keep retrying.",
                                self.ipc.requested_path or "looked in the usual places")
                self.discord_down = True

    def _clear_game(self) -> None:
        self.title_id = None
        self.title = None
        self.started_at = None

    def close(self) -> None:
        self.ipc.close()


def build_activity(state: GameState, info: TitleInfo | None) -> dict:
    activity = {
        "name": info.name if info else state.title_id,
        "timestamps": {"start": int(time.time()) - state.elapsed},
    }
    if info and info.image_url:
        activity["assets"] = {"large_image": info.image_url}

    log.debug("built activity: %s", activity)
    return activity


def valid_ipv4(ip_str: str) -> str:
    """For argparse validation"""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{ip_str}' is not a valid IPv4 address.") from None
    if ip.version != 4:
        raise argparse.ArgumentTypeError(f"'{ip_str}' is an IPv6 address, expected IPv4.")
    return ip_str


def positive_seconds(value: str) -> float:
    """For argparse validation"""
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not a number.") from None
    if seconds <= 0:
        raise argparse.ArgumentTypeError("the interval must be greater than 0.")
    return seconds


def main():
    parser = argparse.ArgumentParser(
        prog='presence5',
        description='A Discord Rich Presence client for PlayStation 5 game activity display'
    )

    parser.add_argument('ps5_ip', type=valid_ipv4, help="Target PS5 IP Address (e.g, 192.168.1.1)")
    parser.add_argument('--discord', '-d', default=None, metavar='PATH',
                        help="Path to Discord's IPC socket or pipe, if auto-detection fails "
                             f"(usually {default_discord_path()})".replace('%', '%%'))
    parser.add_argument('--interval', '-i', type=positive_seconds, default=5,
                        help="Activity refresh interval in seconds (default: %(default)s)")
    parser.add_argument('--verbose', '-v', action='store_true', help="Enable verbose logging")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    client = PresenceClient(args.ps5_ip, PAYLOAD_PORT, args.discord)
    try:
        while True:
            client.step()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        client.close()


if __name__ == "__main__":
    main()