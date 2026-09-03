from __future__ import annotations

import logging

from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QMenu, QWidget

from .config import AppConfig, UsageSnapshot
from .popup import SpendPopup
from .scheduler import RefreshScheduler
from .sni import StatusNotifierItem

log = logging.getLogger(__name__)

# Panel icons sit in a thin strip; farther than this from a screen edge is not a panel.
_PANEL_EDGE_PX = 96
_GAP_PX = 6
_ICON_PAD = 12


def make_tray_icon(
    cursor_pct: int | None = None,
    other_pct: int | None = None,
) -> QIcon:
    """Stacked horizontal meters, similar to Computer Stats tray glyphs."""
    size = 64
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    margin_x = 6
    margin_y = 12
    gap = 8
    bar_h = (size - 2 * margin_y - gap) // 2
    track_w = size - 2 * margin_x

    tracks = (
        (cursor_pct, QColor("#8BA4C7"), QColor("#2A3340")),
        (other_pct, QColor("#B0B0B0"), QColor("#333333")),
    )
    for i, (pct, fill, track) in enumerate(tracks):
        y = margin_y + i * (bar_h + gap)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(margin_x, y, track_w, bar_h, 4, 4)
        if pct is None:
            painter.setBrush(QColor(fill.red(), fill.green(), fill.blue(), 70))
            painter.drawRoundedRect(margin_x, y, max(4, track_w // 12), bar_h, 4, 4)
            continue
        fill_w = max(4, int(track_w * max(0, min(100, pct)) / 100))
        color = fill
        if pct >= 90:
            color = QColor("#D9897A")
        elif pct >= 70 and i == 1:
            color = QColor("#D0B56C")
        painter.setBrush(color)
        painter.drawRoundedRect(margin_x, y, fill_w, bar_h, 4, 4)

    painter.end()
    return QIcon(pix)


class TrayApp(QWidget):
    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self.snapshot = UsageSnapshot.load()

        self.popup = SpendPopup()
        self.popup.apply_snapshot(self.snapshot)
        self.popup.refresh_requested.connect(self._refresh_now)
        # Screen coords from Plasma's StatusNotifierItem.Activate(x, y).
        self._anchor_pos = QPoint()

        self.tray = StatusNotifierItem("Cursor Spend", "cursor-spend-tray", self)
        self.tray.set_icon(
            make_tray_icon(self.snapshot.cursor_models_pct, self.snapshot.other_models_pct)
        )
        self.tray.set_tooltip("Cursor Spend")
        self.tray.activated.connect(self._on_activated)
        self.tray.context_menu_requested.connect(self._on_context_menu)

        self._ctx = QMenu()
        refresh_action = QAction("Refresh now", self)
        refresh_action.triggered.connect(self._refresh_now)
        open_action = QAction("Open spending page hint", self)
        open_action.triggered.connect(self._show_connect_hint)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(QApplication.instance().quit)
        self._ctx.addAction(refresh_action)
        self._ctx.addAction(open_action)
        self._ctx.addSeparator()
        self._ctx.addAction(quit_action)

        self.tray.show()

        self.scheduler = RefreshScheduler(config, self)
        self.scheduler.snapshot_updated.connect(self._on_snapshot)
        self.scheduler.seconds_changed.connect(self.popup.set_remaining)
        self.scheduler.status_changed.connect(self.popup.set_status)
        self.scheduler.refreshing_changed.connect(self._on_refreshing)
        self.scheduler.start()

    def _refresh_now(self) -> None:
        self.scheduler.refresh()

    def _on_refreshing(self, refreshing: bool) -> None:
        self.popup.set_refreshing(refreshing)
        self.popup.set_remaining(self.scheduler.remaining_seconds())

    def _on_snapshot(self, snap: object) -> None:
        assert isinstance(snap, UsageSnapshot)
        self.snapshot = snap
        self.popup.apply_snapshot(snap)
        self.tray.set_icon(make_tray_icon(snap.cursor_models_pct, snap.other_models_pct))
        tip = "Cursor Spend"
        if snap.cursor_models_pct is not None and snap.other_models_pct is not None:
            tip = f"Cursor {snap.cursor_models_pct}% · Other {snap.other_models_pct}%"
        self.tray.set_tooltip(tip)

    def _on_activated(self, pos: QPoint) -> None:
        self._anchor_pos = QPoint(pos)
        # Tray click again while open → close (focus moved to another “item”).
        if self.popup.isVisible():
            self.popup.hide()
            return
        self.popup.show_at(self._popup_position())

    def _on_context_menu(self, pos: QPoint) -> None:
        self._anchor_pos = QPoint(pos)
        self._ctx.popup(pos)

    def _anchor_rect(self) -> QRect:
        """Icon rect from Plasma Activate(x, y). Those are screen coordinates."""
        if self._anchor_pos.isNull():
            return QRect(0, 0, 24, 24)
        return QRect(
            self._anchor_pos.x() - _ICON_PAD,
            self._anchor_pos.y() - _ICON_PAD,
            _ICON_PAD * 2,
            _ICON_PAD * 2,
        )

    def _panel_is_top(self, anchor: QRect, screen_geo: QRect) -> bool:
        """True when the icon sits on a top panel; else bottom (or lower) panel."""
        cy = anchor.center().y()
        dist_top = cy - screen_geo.top()
        dist_bottom = screen_geo.bottom() - cy
        if dist_top <= _PANEL_EDGE_PX:
            return True
        if dist_bottom <= _PANEL_EDGE_PX:
            return False
        return dist_top <= dist_bottom

    def _popup_position(self) -> QPoint:
        anchor = self._anchor_rect()
        self.popup.adjustSize()
        pw = max(self.popup.width(), self.popup.sizeHint().width(), 420)
        ph = max(self.popup.height(), self.popup.sizeHint().height(), 1)
        screen = QApplication.screenAt(anchor.center()) or QApplication.primaryScreen()
        # XWayland availableGeometry often ignores Plasma panels — use full geometry.
        bounds = screen.geometry() if screen else QRect(0, 0, 1920, 1080)

        x = anchor.center().x() - pw // 2
        top_panel = self._panel_is_top(anchor, bounds)
        if top_panel:
            y = anchor.bottom() + _GAP_PX
            if y + ph > bounds.bottom() - 8:
                y = max(bounds.top() + 8, bounds.bottom() - ph - 8)
        else:
            y = anchor.top() - ph - _GAP_PX
            if y < bounds.top() + 8:
                y = bounds.top() + 8

        x = max(bounds.left() + 8, min(x, bounds.right() - pw - 8))
        y = max(bounds.top() + 8, min(y, bounds.bottom() - ph - 8))
        log.info(
            "popup place click=%s,%s anchor=%s top_panel=%s -> %s,%s (%sx%s)",
            self._anchor_pos.x(),
            self._anchor_pos.y(),
            anchor.getRect(),
            top_panel,
            x,
            y,
            pw,
            ph,
        )
        return QPoint(x, y)

    def _show_connect_hint(self) -> None:
        self.popup.set_status(
            "Start Zen with remote debugging enabled on port 9222, keep the spending tab open, then click the countdown to refresh."
        )
        if not self.popup.isVisible():
            self.popup.show_at(self._popup_position())
