from __future__ import annotations

import logging
import subprocess

from PyQt6.QtCore import QPoint, QRect, QRectF, Qt, QTimer
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QApplication, QMenu, QWidget

from .config import AppConfig, UsageSnapshot, zen_is_running
from .popup import SpendPopup
from .scheduler import RefreshScheduler
from .sni import StatusNotifierItem

log = logging.getLogger(__name__)

# Panel icons sit in a thin strip; farther than this from a screen edge is not a panel.
_PANEL_EDGE_PX = 96
_GAP_PX = 6
_ICON_PAD = 12
_LAUNCH_SETTLE_MS = 10_000
_LAUNCH_RETRY_MS = 5_000  # retry interval while waiting for BiDi after launch


def bidi_unavailable(snap: UsageSnapshot) -> bool:
    """True when Zen Remote Agent / remote debugging is not reachable."""
    if snap.source == "unavailable":
        return True
    err = (snap.error or "").lower()
    return "no remote agent" in err or "remote-debugging-port" in err


def tray_tooltip(snap: UsageSnapshot) -> tuple[str, str]:
    """Return (title, body) for the StatusNotifierItem hover tooltip."""
    if bidi_unavailable(snap):
        return (
            "Cursor Spend — Browser inaccessible",
            "Usage hidden until Zen is reachable with remote debugging (see popup).",
        )
    usage = _usage_phrase(snap)
    if usage:
        return "Cursor Spend", usage
    return "Cursor Spend", "Waiting for first reading…"


def _usage_phrase(snap: UsageSnapshot) -> str:
    c, o = snap.cursor_models_pct, snap.other_models_pct
    if c is not None and o is not None:
        return f"Cursor {c}% · Other {o}%"
    if c is not None:
        return f"Cursor {c}%"
    if o is not None:
        return f"Other {o}%"
    return ""


def make_tray_icon(
    cursor_pct: int | None = None,
    other_pct: int | None = None,
    *,
    disconnected: bool = False,
) -> QIcon:
    """Stacked horizontal meters, similar to Computer Stats tray glyphs."""
    size = 64
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    if disconnected:
        _draw_slash_overlay(painter, size)
    else:
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


def _draw_slash_overlay(painter: QPainter, size: int) -> None:
    """Monochrome ⊘ (slash-circle) — remote debugging off; no usage bars."""
    cx = cy = size / 2.0
    r = size * 0.30
    ink = QColor("#E8E8E8")
    pen = QPen(ink, max(3.0, size * 0.07), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))

    inset = r * 0.55
    painter.drawLine(
        QPoint(int(cx - inset), int(cy + inset)),
        QPoint(int(cx + inset), int(cy - inset)),
    )


class TrayApp(QWidget):
    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self.snapshot = UsageSnapshot.load()

        self.popup = SpendPopup()
        self.popup.refresh_requested.connect(self._refresh_now)
        # Screen coords from Plasma's StatusNotifierItem.Activate(x, y).
        self._anchor_pos = QPoint()

        self.tray = StatusNotifierItem("Cursor Spend", "cursor-spend-tray", self)
        self.tray.activated.connect(self._on_activated)
        self.tray.context_menu_requested.connect(self._on_context_menu)

        self._ctx = QMenu()
        self._refresh_action = QAction("Refresh now", self)
        self._refresh_action.triggered.connect(self._refresh_now)
        self._launch_action = QAction("Launch Browser", self)
        self._launch_action.triggered.connect(self._launch_browser)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(QApplication.instance().quit)
        self._ctx.addAction(self._refresh_action)
        self._ctx.addAction(self._launch_action)
        self._ctx.addSeparator()
        self._ctx.addAction(quit_action)

        self._apply_snapshot(self.snapshot)
        self.tray.show()

        self.scheduler = RefreshScheduler(config, self)
        self.scheduler.snapshot_updated.connect(self._on_snapshot)
        self.scheduler.seconds_changed.connect(self.popup.set_remaining)
        self.scheduler.status_changed.connect(self.popup.set_status)
        self.scheduler.refreshing_changed.connect(self._on_refreshing)
        self.scheduler.start()

    def _apply_snapshot(self, snap: UsageSnapshot) -> None:
        disconnected = bidi_unavailable(snap)
        self.popup.apply_snapshot(snap)
        self.popup.set_browser_inaccessible(
            disconnected, self.config.zen_launch_command()
        )
        self._refresh_action.setVisible(not disconnected)
        self._launch_action.setVisible(not zen_is_running())
        self.tray.set_icon(
            make_tray_icon(
                snap.cursor_models_pct,
                snap.other_models_pct,
                disconnected=disconnected,
            )
        )
        title, body = tray_tooltip(snap)
        self.tray.set_tooltip(body, title=title)

    def _refresh_now(self) -> None:
        self.scheduler.refresh()

    def _on_refreshing(self, refreshing: bool) -> None:
        self.popup.set_refreshing(refreshing)
        self.popup.set_remaining(self.scheduler.remaining_seconds())

    def _on_snapshot(self, snap: object) -> None:
        assert isinstance(snap, UsageSnapshot)
        self.snapshot = snap
        self._apply_snapshot(snap)

    def _on_activated(self, pos: QPoint) -> None:
        self._anchor_pos = QPoint(pos)
        # Tray click again while open → close (focus moved to another “item”).
        if self.popup.isVisible():
            self.popup.hide()
            return
        self.popup.show_at(self._popup_position())

    def _on_context_menu(self, pos: QPoint) -> None:
        self._anchor_pos = QPoint(pos)
        # Re-check Zen each time the menu opens so Launch Browser stays accurate.
        self._launch_action.setVisible(not zen_is_running())
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

    def _launch_browser(self) -> None:
        if zen_is_running():
            self._launch_action.setVisible(False)
            self.popup.set_status(
                "Zen is already running — quit it fully, then use Launch Browser "
                "so remote debugging can start."
            )
            if not self.popup.isVisible():
                self.popup.show_at(self._popup_position())
            return

        argv = self.config.zen_launch_argv()
        try:
            subprocess.Popen(
                argv,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            log.exception("Failed to launch Zen")
            self.popup.set_status(f"Could not launch Zen: {exc}")
            self.popup.set_browser_inaccessible(True, self.config.zen_launch_command())
            self._launch_action.setVisible(not zen_is_running())
            if not self.popup.isVisible():
                self.popup.show_at(self._popup_position())
            return

        self._launch_action.setVisible(False)
        self.popup.set_status(
            "Launching Zen with remote debugging… checking in 10 seconds."
        )
        if not self.popup.isVisible():
            self.popup.show_at(self._popup_position())
        if not hasattr(self, "_launch_retry_timer"):
            self._launch_retry_timer = QTimer(self)
            self._launch_retry_timer.setSingleShot(True)
            self._launch_retry_timer.timeout.connect(self._refresh_after_launch)
        self._launch_retry_timer.start(_LAUNCH_SETTLE_MS)

    def _refresh_after_launch(self) -> None:
        """Probe BiDi; if up run a full scrape, otherwise retry every 5s."""
        self.popup.set_status("Checking browser connection…")
        self._launch_action.setVisible(not zen_is_running())
        self.scheduler.probe_or_refresh(
            on_unavailable=self._reschedule_launch_retry
        )

    def _reschedule_launch_retry(self) -> None:
        self.popup.set_status("Browser starting… checking again in 5 seconds.")
        self._launch_retry_timer.start(_LAUNCH_RETRY_MS)
