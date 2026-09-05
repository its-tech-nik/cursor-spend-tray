from __future__ import annotations

import logging
import os
import signal
import sys
from pathlib import Path


def _prefer_xcb_on_wayland() -> None:
    """Tray popups need position + grab; native Wayland Qt blocks both for SNI clicks.

    Running under XWayland (xcb) restores near-icon placement and click-away dismiss.
    Skip if the user already set QT_QPA_PLATFORM.
    """
    if os.environ.get("QT_QPA_PLATFORM"):
        return
    session = (os.environ.get("XDG_SESSION_TYPE") or "").lower()
    if session == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
        os.environ["QT_QPA_PLATFORM"] = "xcb"


def _install_posix_quit_handlers(app) -> None:
    """Map Ctrl+C / SIGTERM to a clean Qt quit so aboutToQuit shutdown runs.

    The Qt event loop is mostly in C++, so a short timer lets Python deliver
    signals; the handler then asks QApplication to quit on the GUI thread.
    """
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication

    def _request_quit(*_args) -> None:
        inst = QApplication.instance()
        if inst is not None:
            # Defer to the event loop — unsafe to tear down Qt inside the handler.
            QTimer.singleShot(0, inst.quit)

    signal.signal(signal.SIGINT, _request_quit)
    signal.signal(signal.SIGTERM, _request_quit)

    # Wake the interpreter often enough that pending SIGINT/SIGTERM are handled.
    wake = QTimer(app)
    wake.setInterval(200)
    wake.timeout.connect(lambda: None)
    wake.start()
    app._posix_signal_wake_timer = wake  # type: ignore[attr-defined]


def _acquire_single_instance_lock():
    """Return a held QLockFile, or None if another instance already owns it.

    Plasma session restore plus XDG autostart can start the tray twice; the
    second process should exit quietly instead of registering another SNI icon.
    """
    from PyQt6.QtCore import QLockFile

    from .config import APP_NAME, data_dir

    root = data_dir()
    root.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(root / f"{APP_NAME}.lock"))
    # Allow recovering a lock left behind after a crash (pid no longer alive).
    lock.setStaleLockTime(30_000)
    if not lock.tryLock(0):
        return None
    return lock


def main() -> None:
    _prefer_xcb_on_wayland()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from PyQt6.QtGui import QIcon
    from PyQt6.QtWidgets import QApplication, QMessageBox

    from .app import TrayApp
    from .config import AppConfig

    instance_lock = _acquire_single_instance_lock()
    if instance_lock is None:
        logging.getLogger(__name__).info(
            "Another Cursor Spend Tray instance is already running; exiting."
        )
        sys.exit(0)

    QApplication.setQuitOnLastWindowClosed(False)
    app = QApplication(sys.argv)
    app.setApplicationName("Cursor Spend Tray")
    app.setOrganizationName("cursor-spend-tray")
    # Keep the lock alive for the process lifetime (unlocked on destroy/quit).
    app._instance_lock = instance_lock  # type: ignore[attr-defined]
    icon_path = Path(__file__).resolve().parent / "resources" / "cursor-spend-tray.svg"
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))

    config = AppConfig.load()
    try:
        tray = TrayApp(config)
    except RuntimeError as exc:
        QMessageBox.critical(None, "Cursor Spend Tray", str(exc))
        sys.exit(1)

    app._cursor_spend_tray = tray  # type: ignore[attr-defined]
    _install_posix_quit_handlers(app)
    app.aboutToQuit.connect(tray.shutdown)
    app.aboutToQuit.connect(tray.tray.cleanup)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
