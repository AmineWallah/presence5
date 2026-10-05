import ipaddress
import logging
import signal
import sys
import time
import urllib.request
from dataclasses import dataclass

from PySide6.QtCore import QSettings, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu,
    QPushButton, QSpinBox, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from discord_ipc import default_discord_path
from main import PAYLOAD_PORT, PresenceClient

log = logging.getLogger(__name__)

COVER_SIZE = 128
GREEN, RED, GREY = "#3ba55d", "#ed4245", "#8e9297"


@dataclass
class Snapshot:
    console_reachable: bool
    discord_connected: bool
    title_id: str | None
    name: str | None
    image_url: str | None
    started_at: int | None


class Worker(QThread):

    updated = Signal(object)        # a Snapshot, after every poll
    cover_loaded = Signal(str, bytes)  # image URL, image bytes (empty if it failed)

    def __init__(self, host: str, discord_path: str, interval: int):
        super().__init__()
        self.host = host
        self.discord_path = discord_path
        self.interval = interval

    def run(self) -> None:
        client = PresenceClient(self.host, PAYLOAD_PORT, self.discord_path)
        last_image_url = None
        try:
            while not self.isInterruptionRequested():
                client.step()

                image_url = client.title.image_url if client.title else None
                self.updated.emit(Snapshot(
                    console_reachable=client.console_reachable,
                    discord_connected=client.discord_connected,
                    title_id=client.title_id,
                    name=(client.title.name if client.title else client.title_id),
                    image_url=image_url,
                    started_at=client.started_at,
                ))

                if image_url != last_image_url:
                    last_image_url = image_url
                    if image_url:
                        self.cover_loaded.emit(image_url, download(image_url))

                # Sleep in short slices so Stop takes effect quickly.
                waited = 0
                while waited < self.interval * 1000 and not self.isInterruptionRequested():
                    self.msleep(100)
                    waited += 100
        finally:
            # Closing the connection is what removes the presence from Discord.
            client.close()


def download(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.read()
    except (OSError, ValueError) as e:
        log.warning("could not download cover image: %r", e)
        return b""


def make_icon() -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#2f6fed"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(2, 2, 60, 60, 14, 14)
    font = QFont()
    font.setBold(True)
    font.setPixelSize(30)
    painter.setFont(font)
    painter.setPen(QColor("white"))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "P5")
    painter.end()
    return QIcon(pixmap)


def format_elapsed(seconds: int) -> str:
    hours, rest = divmod(max(seconds, 0), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("presence5")
        self.setWindowIcon(make_icon())
        self.setMinimumWidth(380)

        self.worker: Worker | None = None
        self.started_at: int | None = None
        self.shown_image_url: str | None = None
        self.settings = QSettings("presence5", "presence5")

        # --- Settings ---------------------------------------------------
        self.ip_input = QLineEdit(self.settings.value("ps5_ip", "", str))
        self.ip_input.setPlaceholderText("192.168.1.18")
        self.ip_input.returnPressed.connect(self.on_button_clicked)

        self.discord_input = QLineEdit(self.settings.value("discord_path", "", str))
        self.discord_input.setPlaceholderText(default_discord_path())
        self.discord_input.setToolTip("Leave empty to use the default location.")

        self.interval_input = QSpinBox()
        self.interval_input.setRange(1, 60)
        self.interval_input.setSuffix(" s")
        self.interval_input.setValue(self.settings.value("interval", 5, int))

        form = QFormLayout()
        form.addRow("PS5 IP address", self.ip_input)
        form.addRow("Discord socket", self.discord_input)
        form.addRow("Refresh every", self.interval_input)

        self.error_label = QLabel()
        self.error_label.setStyleSheet(f"color: {RED};")
        self.error_label.setWordWrap(True)
        self.error_label.hide()

        self.button = QPushButton("Start")
        self.button.setDefault(True)
        self.button.clicked.connect(self.on_button_clicked)

        # --- Connection status ------------------------------------------
        self.console_status = QLabel()
        self.discord_status = QLabel()

        # --- Now playing ------------------------------------------------
        self.cover = QLabel()
        self.cover.setFixedSize(COVER_SIZE, COVER_SIZE)
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover.setStyleSheet(
            f"background: palette(alternate-base); border-radius: 8px; color: {GREY};"
        )

        self.game_name = QLabel()
        self.game_name.setWordWrap(True)
        name_font = QFont(self.font())
        name_font.setPointSizeF(name_font.pointSizeF() * 1.25)
        name_font.setBold(True)
        self.game_name.setFont(name_font)

        self.game_id = QLabel()
        self.game_id.setStyleSheet(f"color: {GREY};")
        self.game_id.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.elapsed = QLabel()

        details = QVBoxLayout()
        details.addStretch()
        details.addWidget(self.game_name)
        details.addWidget(self.game_id)
        details.addWidget(self.elapsed)
        details.addStretch()

        now_playing = QHBoxLayout()
        now_playing.setSpacing(14)
        now_playing.addWidget(self.cover)
        now_playing.addLayout(details, 1)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFrameShadow(QFrame.Shadow.Sunken)

        # --- The whole thing --------------------------------------------
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)
        layout.addLayout(form)
        layout.addWidget(self.error_label)
        layout.addWidget(self.button)
        layout.addWidget(divider)
        layout.addWidget(self.console_status)
        layout.addWidget(self.discord_status)
        layout.addSpacing(6)
        layout.addLayout(now_playing)

        # Ticks the elapsed time once a second, independently of polling.
        self.clock = QTimer(self)
        self.clock.setInterval(1000)
        self.clock.timeout.connect(self.refresh_elapsed)

        self.tray: QSystemTrayIcon | None = None
        self.tray_hint_shown = False
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.setup_tray()

        self.show_stopped()

    # --- Tray icon ---------------------------------------------------------

    def setup_tray(self) -> None:
        self.tray = QSystemTrayIcon(make_icon(), self)

        menu = QMenu(self)
        self.tray_show_action = QAction("Show window", self)
        self.tray_show_action.triggered.connect(self.show_window)
        self.tray_toggle_action = QAction("Start", self)
        self.tray_toggle_action.triggered.connect(self.on_button_clicked)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.quit)
        menu.addAction(self.tray_show_action)
        menu.addAction(self.tray_toggle_action)
        menu.addSeparator()
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.setToolTip("presence5")
        self.tray.show()

        # With a tray icon, closing the window must not end the program.
        QApplication.instance().setQuitOnLastWindowClosed(False)

    def on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:  # left click
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self.show_window()

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit(self) -> None:
        self.shutdown_worker()
        if self.tray is not None:
            self.tray.hide()
        QApplication.instance().quit()

    def shutdown_worker(self) -> None:
        # Let the worker finish its pass so the presence is cleared properly.
        if self.worker is not None:
            self.worker.requestInterruption()
            self.worker.wait(8000)

    # --- Start / stop ----------------------------------------------------

    def on_button_clicked(self) -> None:
        if self.worker is None:
            self.start()
        else:
            self.stop()

    def start(self) -> None:
        host = self.ip_input.text().strip()
        try:
            if ipaddress.ip_address(host).version != 4:
                raise ValueError
        except ValueError:
            self.show_error("Enter the PS5's IPv4 address, for example 192.168.1.18.")
            self.ip_input.setFocus()
            return

        discord_path = self.discord_input.text().strip() or default_discord_path()
        interval = self.interval_input.value()

        self.settings.setValue("ps5_ip", host)
        self.settings.setValue("discord_path", self.discord_input.text().strip())
        self.settings.setValue("interval", interval)

        self.error_label.hide()
        self.set_inputs_enabled(False)
        self.set_button_text("Stop")
        self.set_status(self.console_status, GREY, "Console: connecting…")
        self.set_status(self.discord_status, GREY, "Discord: connecting…")

        self.worker = Worker(host, discord_path, interval)
        self.worker.updated.connect(self.on_updated)
        self.worker.cover_loaded.connect(self.on_cover_loaded)
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.start()
        self.clock.start()

    def stop(self) -> None:
        if self.worker is None:
            return
        # The worker may be in the middle of a network call, so don't wait
        # here. on_worker_finished runs when it has actually stopped.
        self.button.setEnabled(False)
        self.set_button_text("Stopping…")
        self.worker.requestInterruption()

    def on_worker_finished(self) -> None:
        self.worker = None
        self.clock.stop()
        self.button.setEnabled(True)
        self.set_button_text("Start")
        self.set_inputs_enabled(True)
        self.show_stopped()

    # --- Updates from the worker -----------------------------------------

    def on_updated(self, snap: Snapshot) -> None:
        if self.worker is None or self.sender() is not self.worker:
            return  # a late update from a worker that has since been stopped

        if snap.console_reachable:
            self.set_status(self.console_status, GREEN, "Console: connected")
        else:
            self.set_status(self.console_status, RED,
                            "Console: not reachable. Is the payload running?")

        if snap.discord_connected:
            self.set_status(self.discord_status, GREEN, "Discord: connected")
        else:
            self.set_status(self.discord_status, RED, "Discord: not reachable. Is it open?")

        self.started_at = snap.started_at

        if snap.title_id is None:
            self.show_no_game("Console is offline" if not snap.console_reachable
                              else "No game running")
            return

        if self.tray is not None:
            self.tray.setToolTip(f"presence5: {snap.name or snap.title_id}")
        self.game_name.setText(snap.name or snap.title_id)
        self.game_id.setText(snap.title_id)
        self.game_id.show()
        self.elapsed.show()
        self.refresh_elapsed()

        if snap.image_url != self.shown_image_url:
            # A different game, or one with no cover: drop the old picture.
            self.shown_image_url = None
            self.cover.setPixmap(QPixmap())
            self.cover.setText("…" if snap.image_url else "No cover")

    def on_cover_loaded(self, url: str, data: bytes) -> None:
        if self.worker is None or self.sender() is not self.worker:
            return
        pixmap = QPixmap()
        if not data or not pixmap.loadFromData(data):
            self.cover.setPixmap(QPixmap())
            self.cover.setText("No cover")
            return

        ratio = self.devicePixelRatioF()
        scaled = pixmap.scaled(
            int(COVER_SIZE * ratio), int(COVER_SIZE * ratio),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        scaled.setDevicePixelRatio(ratio)
        self.cover.setText("")
        self.cover.setPixmap(scaled)
        self.shown_image_url = url

    def refresh_elapsed(self) -> None:
        if self.started_at is not None:
            self.elapsed.setText(format_elapsed(int(time.time()) - self.started_at))

    # --- Small helpers ---------------------------------------------------

    def show_stopped(self) -> None:
        self.set_status(self.console_status, GREY, "Console: not started")
        self.set_status(self.discord_status, GREY, "Discord: not started")
        self.started_at = None
        self.show_no_game("Press Start to begin")

    def show_no_game(self, message: str) -> None:
        if self.tray is not None:
            self.tray.setToolTip("presence5")
        self.game_name.setText(message)
        self.game_id.hide()
        self.elapsed.hide()
        self.shown_image_url = None
        self.cover.setPixmap(QPixmap())
        self.cover.setText("")

    def show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    def set_button_text(self, text: str) -> None:
        self.button.setText(text)
        if self.tray is not None:
            self.tray_toggle_action.setText(text)
            self.tray_toggle_action.setEnabled(self.button.isEnabled())

    def set_inputs_enabled(self, enabled: bool) -> None:
        for widget in (self.ip_input, self.discord_input, self.interval_input):
            widget.setEnabled(enabled)

    @staticmethod
    def set_status(label: QLabel, colour: str, text: str) -> None:
        label.setText(f'<span style="color: {colour};">●</span>&nbsp; {text}')

    def closeEvent(self, event) -> None:
        if self.tray is not None:
            # Close to tray: keep running in the background.
            event.ignore()
            self.hide()
            if not self.tray_hint_shown:
                self.tray_hint_shown = True
                self.tray.showMessage(
                    "presence5 is still running",
                    "Click the tray icon to reopen it, or right-click it to quit.",
                    make_icon(), 4000,
                )
            return

        # No system tray on this desktop: closing the window quits.
        self.shutdown_worker()
        event.accept()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    signal.signal(signal.SIGINT, signal.SIG_DFL)  # let Ctrl+C in the terminal quit

    app = QApplication(sys.argv)
    app.setApplicationName("presence5")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()