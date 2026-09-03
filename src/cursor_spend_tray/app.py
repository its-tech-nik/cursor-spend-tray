from __future__ import annotations

import logging
import subprocess

from PyQt6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, Qt, QTimer
from PyQt6.QtGui import (
    QAction,
    QColor,
    QConicalGradient,
    QGuiApplication,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import autostart
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
_LAUNCH_RETRY_MS = 5_000      # retry interval while waiting for BiDi after launch
_SPIN_INTERVAL_MS = 80        # icon animation frame interval (~12 fps)


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


def make_spinner_icon(angle_deg: float) -> QIcon:
    """Monochrome rotating arc icon shown while waiting for Zen to start."""
    size = 64
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    cx = cy = size / 2.0
    r = size * 0.28
    stroke = max(4.0, size * 0.10)
    rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)

    # Dim track ring
    track_pen = QPen(QColor(80, 80, 80, 160), stroke, Qt.PenStyle.SolidLine,
                     Qt.PenCapStyle.RoundCap)
    painter.setPen(track_pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(rect)

    # Bright arc (270° sweep, rotated by angle_deg)
    arc_pen = QPen(QColor("#E8E8E8"), stroke, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap)
    painter.setPen(arc_pen)
    # Qt drawArc uses 1/16th degrees, start from top (90°) rotated by angle_deg
    start = int((90 - angle_deg) * 16)
    span = int(-270 * 16)
    painter.drawArc(rect, start, span)

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


class TrayContextMenu(QFrame):
    """Tray menu as a Tool window.

    QMenu is a Qt Popup; under Plasma/XWayland those often never deactivate on
    outside click, so the menu sticks until the tray is clicked again. Tool +
    focus/outside tracking matches SpendPopup dismiss behaviour.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._dismiss_armed = False
        self._arm_timer = QTimer(self)
        self._arm_timer.setSingleShot(True)
        self._arm_timer.timeout.connect(self._arm_dismiss)
        self._outside_filter = _CtxOutsideClickFilter(self)
        self._rows: list[QWidget] = []

        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumWidth(220)
        self.setStyleSheet(
            """
            TrayContextMenu {
                background: #2B2B2B;
                border: 1px solid #3A3A3A;
                border-radius: 10px;
            }
            QFrame#ctxSep {
                background: #3A3A3A;
                border: none;
                max-height: 1px;
                margin: 6px 10px;
            }
            """
        )

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(6, 6, 6, 6)
        self._layout.setSpacing(2)

    def add_action(self, action: QAction) -> QWidget:
        row = _CtxMenuRow(action, self)
        action.changed.connect(lambda r=row, a=action: self._sync_row(r, a))
        action.triggered.connect(self.hide)
        self._sync_row(row, action)
        self._layout.addWidget(row)
        self._rows.append(row)
        return row

    def add_separator(self) -> QFrame:
        sep = QFrame(self)
        sep.setObjectName("ctxSep")
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        self._layout.addWidget(sep)
        self._rows.append(sep)
        return sep

    @staticmethod
    def _sync_row(row: QWidget, action: QAction) -> None:
        row.setVisible(action.isVisible())
        row.setEnabled(action.isEnabled())

    def popup_at(self, pos: QPoint) -> None:
        """Show near the tray icon; dismiss when focus leaves or user clicks away."""
        self._dismiss_armed = False
        self._arm_timer.stop()
        # Drop empty separators left by hidden actions (e.g. Launch Browser).
        self._refresh_separator_visibility()
        self.adjustSize()
        screen = QApplication.screenAt(pos) or QApplication.primaryScreen()
        bounds = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        w = max(self.sizeHint().width(), 220)
        h = max(self.sizeHint().height(), 1)
        x = min(max(pos.x(), bounds.left() + 4), bounds.right() - w - 4)
        y = min(max(pos.y(), bounds.top() + 4), bounds.bottom() - h - 4)

        self.setFixedWidth(w)
        self.setGeometry(x, y, w, h)
        self.show()
        self.move(x, y)
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        QTimer.singleShot(0, lambda: self.move(x, y) if self.isVisible() else None)

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self._outside_filter)
            try:
                app.focusWindowChanged.disconnect(self._on_focus_window_changed)
            except TypeError:
                pass
            app.focusWindowChanged.connect(self._on_focus_window_changed)

        self._arm_timer.start(200)

    def _refresh_separator_visibility(self) -> None:
        """Hide a separator when nothing visible sits above or below it."""
        items = self._rows
        for i, widget in enumerate(items):
            if widget.objectName() != "ctxSep":
                continue
            above = any(
                w.isVisibleTo(self) and w.objectName() != "ctxSep"
                for w in items[:i]
            )
            below = any(
                w.isVisibleTo(self) and w.objectName() != "ctxSep"
                for w in items[i + 1 :]
            )
            widget.setVisible(above and below)

    def _arm_dismiss(self) -> None:
        self._dismiss_armed = True
        if self.isVisible() and QGuiApplication.focusWindow() is not self.windowHandle():
            self.hide()

    def _on_focus_window_changed(self, window) -> None:  # noqa: ANN001
        if not self._dismiss_armed or not self.isVisible():
            return
        if window is self.windowHandle():
            return
        self.hide()

    def changeEvent(self, event) -> None:  # noqa: ANN001
        if (
            event.type() == QEvent.Type.WindowDeactivate
            and self.isVisible()
            and self._dismiss_armed
        ):
            self.hide()
        super().changeEvent(event)

    def hideEvent(self, event) -> None:  # noqa: ANN001
        self._arm_timer.stop()
        self._dismiss_armed = False
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self._outside_filter)
            try:
                app.focusWindowChanged.disconnect(self._on_focus_window_changed)
            except TypeError:
                pass
        super().hideEvent(event)


class _CtxMenuRow(QFrame):
    """Left-aligned menu row with optional trailing checkmark (Notion-style)."""

    def __init__(self, action: QAction, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._action = action
        self.setObjectName("ctxRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(34)
        self.setStyleSheet(
            """
            QFrame#ctxRow {
                background: transparent;
                border: none;
                border-radius: 6px;
            }
            QFrame#ctxRow:hover {
                background: #3A3A3A;
            }
            QLabel {
                background: transparent;
                border: none;
            }
            """
        )

        self._label = QLabel(action.text())
        label_font = self._label.font()
        label_font.setPointSize(10)
        self._label.setFont(label_font)
        self._label.setStyleSheet("color: #F2F2F2; padding-left: 10px;")
        self._label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )

        self._trailing = QLabel("")
        self._trailing.setFixedWidth(22)
        self._trailing.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._trailing.setStyleSheet("color: #A0A0A0; padding-right: 10px;")

        row = QHBoxLayout(self)
        row.setContentsMargins(2, 0, 2, 0)
        row.setSpacing(8)
        row.addWidget(self._label, stretch=1)
        row.addWidget(self._trailing)

        action.changed.connect(self._sync_from_action)
        self._sync_from_action()

    def _sync_from_action(self) -> None:
        self._label.setText(self._action.text())
        if self._action.isCheckable() and self._action.isChecked():
            self._trailing.setText("✓")
        else:
            self._trailing.setText("")

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.MouseButton.LeftButton and self._action.isEnabled():
            self._action.trigger()
        super().mouseReleaseEvent(event)


class _CtxOutsideClickFilter(QObject):
    """Dismiss the tray menu when a press lands outside its geometry."""

    def __init__(self, menu: TrayContextMenu) -> None:
        super().__init__(menu)
        self._menu = menu

    def eventFilter(self, obj, event) -> bool:  # noqa: ANN001
        if event.type() not in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonDblClick,
        ):
            return False
        if not self._menu.isVisible() or not self._menu._dismiss_armed:
            return False
        try:
            global_pos = event.globalPosition().toPoint()
        except AttributeError:
            global_pos = event.globalPos()
        if self._menu.frameGeometry().contains(global_pos):
            return False
        self._menu.hide()
        return False


class TrayApp(QWidget):
    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        # Hidden native window so grab/focus parenting works for tray UI.
        self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.setWindowFlags(Qt.WindowType.Tool)
        self.config = config
        self.snapshot = UsageSnapshot.load()

        self.popup = SpendPopup()
        self.popup.refresh_requested.connect(self._refresh_now)
        # Screen coords from Plasma's StatusNotifierItem.Activate(x, y).
        self._anchor_pos = QPoint()

        self.tray = StatusNotifierItem("Cursor Spend", "cursor-spend-tray", self)
        self.tray.activated.connect(self._on_activated)
        self.tray.context_menu_requested.connect(self._on_context_menu)

        self._ctx = TrayContextMenu()
        self._refresh_action = QAction("Refresh now", self)
        self._refresh_action.triggered.connect(self._refresh_now)
        self._launch_action = QAction("Launch Browser", self)
        self._launch_action.triggered.connect(self._launch_browser)
        self._autostart_action = QAction("Launch at login", self)
        self._autostart_action.setCheckable(True)
        self._autostart_action.setChecked(autostart.is_enabled())
        self._autostart_action.toggled.connect(self._on_autostart_toggled)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(QApplication.instance().quit)
        self._ctx.add_action(self._refresh_action)
        self._ctx.add_action(self._launch_action)
        self._ctx.add_separator()
        self._ctx.add_action(self._autostart_action)
        self._ctx.add_separator()
        self._ctx.add_action(quit_action)

        # Spinner animation (used while waiting for Zen to start after Launch Browser)
        self._spin_angle = 0.0
        self._spin_timer = QTimer(self)
        self._spin_timer.setInterval(_SPIN_INTERVAL_MS)
        self._spin_timer.timeout.connect(self._on_spin_tick)

        self._apply_snapshot(self.snapshot)
        self.show()
        self.tray.show()

        self.scheduler = RefreshScheduler(config, self)
        self.scheduler.snapshot_updated.connect(self._on_snapshot)
        self.scheduler.seconds_changed.connect(self.popup.set_remaining)
        self.scheduler.status_changed.connect(self.popup.set_status)
        self.scheduler.refreshing_changed.connect(self._on_refreshing)
        self.scheduler.start()

    def _apply_snapshot(self, snap: UsageSnapshot) -> None:
        # If we're still in the launch-wait loop and the scrape came back unavailable,
        # keep the spinner running and let the retry handle it instead of flashing the
        # slash icon.
        launch_pending = (
            hasattr(self, "_launch_retry_timer")
            and self._launch_retry_timer.isActive()
        )
        if launch_pending and bidi_unavailable(snap):
            return

        self._stop_spinner()
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
        if self._ctx.isVisible():
            self._ctx.hide()
            return
        # Tray click again while open → close (focus moved to another “item”).
        if self.popup.isVisible():
            self.popup.hide()
            return
        self.popup.show_at(self._popup_position())

    def _on_context_menu(self, pos: QPoint) -> None:
        self._anchor_pos = QPoint(pos)
        if self.popup.isVisible():
            self.popup.hide()
        if self._ctx.isVisible():
            self._ctx.hide()
            return
        # Re-check Zen each time the menu opens so Launch Browser stays accurate.
        self._launch_action.setVisible(not zen_is_running())
        self._autostart_action.blockSignals(True)
        self._autostart_action.setChecked(autostart.is_enabled())
        self._autostart_action.blockSignals(False)
        self._ctx.popup_at(pos)

    def _on_autostart_toggled(self, enabled: bool) -> None:
        try:
            autostart.set_enabled(enabled)
        except OSError as exc:
            log.exception("Failed to %s launch at login", "enable" if enabled else "disable")
            self._autostart_action.blockSignals(True)
            self._autostart_action.setChecked(autostart.is_enabled())
            self._autostart_action.blockSignals(False)
            QMessageBox.warning(
                None,
                "Cursor Spend Tray",
                f"Could not update launch at login:\n{exc}",
            )

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

    def _start_spinner(self) -> None:
        self._spin_angle = 0.0
        self._spin_timer.start()

    def _stop_spinner(self) -> None:
        self._spin_timer.stop()

    def _on_spin_tick(self) -> None:
        self._spin_angle = (self._spin_angle + 18) % 360  # one full rotation per ~1.6s
        self.tray.set_icon(make_spinner_icon(self._spin_angle))

    def _launch_browser(self) -> None:
        if zen_is_running():
            self._launch_action.setVisible(False)
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
            self._launch_action.setVisible(not zen_is_running())
            return

        self._launch_action.setVisible(False)
        self._start_spinner()
        if not hasattr(self, "_launch_retry_timer"):
            self._launch_retry_timer = QTimer(self)
            self._launch_retry_timer.setSingleShot(True)
            self._launch_retry_timer.timeout.connect(self._refresh_after_launch)
        self._launch_retry_timer.start(_LAUNCH_SETTLE_MS)

    def _refresh_after_launch(self) -> None:
        """Probe BiDi; if up scrape (spinner stops on snapshot), otherwise retry."""
        self._launch_action.setVisible(not zen_is_running())
        self.scheduler.probe_or_refresh(
            on_unavailable=self._reschedule_launch_retry,
        )

    def _reschedule_launch_retry(self) -> None:
        self._launch_retry_timer.start(_LAUNCH_RETRY_MS)
