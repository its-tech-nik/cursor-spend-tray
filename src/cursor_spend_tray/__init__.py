from __future__ import annotations

import logging
import os
import sys


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


def main() -> None:
    _prefer_xcb_on_wayland()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from PyQt6.QtWidgets import QApplication, QMessageBox

    from .app import TrayApp
    from .config import AppConfig

    QApplication.setQuitOnLastWindowClosed(False)
    app = QApplication(sys.argv)
    app.setApplicationName("Cursor Spend Tray")
    app.setOrganizationName("cursor-spend-tray")

    config = AppConfig.load()
    try:
        tray = TrayApp(config)
    except RuntimeError as exc:
        QMessageBox.critical(None, "Cursor Spend Tray", str(exc))
        sys.exit(1)

    app._cursor_spend_tray = tray  # type: ignore[attr-defined]
    app.aboutToQuit.connect(tray.tray.cleanup)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
