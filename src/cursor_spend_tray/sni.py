"""Plasma StatusNotifierItem that keeps Activate(x, y) click coordinates.

Qt's QSystemTrayIcon D-Bus backend always returns empty geometry() and never
surfaces the x/y Plasma passes on Activate. We register our own SNI so popup
placement can follow the tray icon (top or bottom panel).
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from PyQt6.QtCore import QObject, QPoint, pyqtClassInfo, pyqtProperty, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusAbstractAdaptor, QDBusConnection, QDBusMessage
from PyQt6.QtGui import QIcon, QPixmap

log = logging.getLogger(__name__)

_SNI_PATH = "/StatusNotifierItem"
_SNI_IFACE = "org.kde.StatusNotifierItem"
_WATCHER_SERVICE = "org.kde.StatusNotifierWatcher"
_WATCHER_PATH = "/StatusNotifierWatcher"
_WATCHER_IFACE = "org.kde.StatusNotifierWatcher"


@pyqtClassInfo("D-Bus Interface", _SNI_IFACE)
class _StatusNotifierAdaptor(QDBusAbstractAdaptor):
    def __init__(self, item: StatusNotifierItem) -> None:
        super().__init__(item)
        self.setAutoRelaySignals(True)

    def _item(self) -> StatusNotifierItem:
        return self.parent()  # type: ignore[return-value]

    @pyqtProperty(str)
    def Category(self) -> str:
        return "ApplicationStatus"

    @pyqtProperty(str)
    def Id(self) -> str:
        return self._item().item_id

    @pyqtProperty(str)
    def Title(self) -> str:
        return self._item().title

    @pyqtProperty(str)
    def Status(self) -> str:
        return "Active"

    @pyqtProperty(int)
    def WindowId(self) -> int:
        return 0

    @pyqtProperty(bool)
    def ItemIsMenu(self) -> bool:
        # Left click must call Activate(x, y) so we receive icon coordinates.
        return False

    @pyqtProperty(str)
    def IconName(self) -> str:
        return self._item().icon_name

    @pyqtProperty(str)
    def IconThemePath(self) -> str:
        return self._item().icon_theme_path

    @pyqtProperty(str)
    def OverlayIconName(self) -> str:
        return ""

    @pyqtProperty(str)
    def AttentionIconName(self) -> str:
        return ""

    @pyqtProperty(str)
    def AttentionMovieName(self) -> str:
        return ""

    @pyqtSlot(int, int)
    def Activate(self, x: int, y: int) -> None:
        self._item().handle_activate(x, y)

    @pyqtSlot(int, int)
    def SecondaryActivate(self, x: int, y: int) -> None:
        self._item().handle_activate(x, y)

    @pyqtSlot(int, int)
    def ContextMenu(self, x: int, y: int) -> None:
        self._item().handle_context_menu(x, y)

    @pyqtSlot(int, str)
    def Scroll(self, _delta: int, _orientation: str) -> None:
        return

    @pyqtSlot(str)
    def ProvideXdgActivationToken(self, _token: str) -> None:
        return


class StatusNotifierItem(QObject):
    """Tray icon via org.kde.StatusNotifierItem (Plasma-compatible)."""

    activated = pyqtSignal(QPoint)
    context_menu_requested = pyqtSignal(QPoint)

    def __init__(
        self,
        title: str = "Cursor Spend",
        item_id: str = "cursor-spend-tray",
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.title = title
        self.item_id = item_id
        self.tooltip = title
        self.icon_name = item_id
        self._icon_dir = Path(tempfile.mkdtemp(prefix="cursor-spend-tray-icons-"))
        self.icon_theme_path = str(self._icon_dir)
        self._icon_file = self._icon_dir / f"{self.icon_name}.png"
        self._registered = False
        self._bus = QDBusConnection.sessionBus()
        self._adaptor = _StatusNotifierAdaptor(self)

    def show(self) -> None:
        if self._registered:
            return
        if not self._bus.isConnected():
            raise RuntimeError("No D-Bus session bus; cannot register tray icon.")

        if not self._bus.registerObject(
            _SNI_PATH,
            self,
            QDBusConnection.RegisterOption.ExportAdaptors,
        ):
            raise RuntimeError(f"Failed to register {_SNI_PATH} on D-Bus.")

        service = self._bus.baseService()
        msg = QDBusMessage.createMethodCall(
            _WATCHER_SERVICE,
            _WATCHER_PATH,
            _WATCHER_IFACE,
            "RegisterStatusNotifierItem",
        )
        msg.setArguments([service])
        reply = self._bus.call(msg)
        if reply.type() == QDBusMessage.MessageType.ErrorMessage:
            msg.setArguments([f"{service}{_SNI_PATH}"])
            reply = self._bus.call(msg)
            if reply.type() == QDBusMessage.MessageType.ErrorMessage:
                self._bus.unregisterObject(_SNI_PATH)
                raise RuntimeError(
                    f"StatusNotifierWatcher register failed: {reply.errorMessage()}"
                )

        self._registered = True
        log.info("Registered StatusNotifierItem %s%s", service, _SNI_PATH)

    def hide(self) -> None:
        if not self._registered:
            return
        self._bus.unregisterObject(_SNI_PATH)
        self._registered = False

    def set_icon(self, icon: QIcon) -> None:
        size = 64
        pix = icon.pixmap(size, size)
        if pix.isNull():
            pix = QPixmap(size, size)
            pix.fill()
        if not pix.save(str(self._icon_file), "PNG"):
            log.warning("Failed to write tray icon to %s", self._icon_file)
            return
        os.utime(self._icon_file, None)
        if self._registered:
            self._emit(_SNI_IFACE, "NewIcon")

    def set_tooltip(self, text: str) -> None:
        self.tooltip = text
        if self._registered:
            self._emit(_SNI_IFACE, "NewToolTip")

    def handle_activate(self, x: int, y: int) -> None:
        log.info("SNI Activate at %s,%s", x, y)
        self.activated.emit(QPoint(x, y))

    def handle_context_menu(self, x: int, y: int) -> None:
        log.info("SNI ContextMenu at %s,%s", x, y)
        self.context_menu_requested.emit(QPoint(x, y))

    def _emit(self, iface: str, name: str) -> None:
        self._bus.send(QDBusMessage.createSignal(_SNI_PATH, iface, name))

    def cleanup(self) -> None:
        self.hide()
        try:
            if self._icon_file.exists():
                self._icon_file.unlink()
            self._icon_dir.rmdir()
        except OSError:
            pass
