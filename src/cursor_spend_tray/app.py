from __future__ import annotations

import logging
import math
import subprocess
from collections.abc import Callable

import httpx
from PyQt6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt, QTimer
from PyQt6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QConicalGradient,
    QGuiApplication,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
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
from .auth_detect import snapshot_needs_login
from .config import (
    APP_NAME,
    POLL_INTERVAL_MINUTES,
    AppConfig,
    BetweenScrapesMode,
    UsageSnapshot,
    data_dir,
    poll_interval_label,
)
from .popup import SpendPopup
from .login_network_watch import LoginNetworkWatcher, LogoutNetworkWatcher
from .scheduler import RefreshScheduler
from .session_cookie import profile_has_cursor_session_cookie
from .sni import StatusNotifierItem

log = logging.getLogger(__name__)

# Panel icons sit in a thin strip; farther than this from a screen edge is not a panel.
_PANEL_EDGE_PX = 96
_GAP_PX = 6
_ICON_PAD = 12
_LAUNCH_SETTLE_MS = 10_000
_LAUNCH_RETRY_MS = 5_000      # retry interval while waiting for BiDi after launch
_SPIN_INTERVAL_MS = 80        # icon animation frame interval (~12 fps)
# Keep in sync with scheduler.BROWSER_WARMUP_SECONDS / quit-between pre-launch.
_CTX_ROW_STYLE = """
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
_CTX_ACCOUNT_STYLE = """
QFrame#ctxAccount {
    background: transparent;
    border: none;
    border-radius: 8px;
}
QLabel {
    background: transparent;
    border: none;
}
"""
_CTX_MENU_STYLE = """
TrayContextMenu, TrayContextSubmenu {
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

_ACCOUNT_AVATAR_PX = 40


def _circular_avatar(src: QPixmap, size: int = _ACCOUNT_AVATAR_PX) -> QPixmap:
    """Center-crop to a smooth circular mask (full color)."""
    if src.isNull():
        return QPixmap()
    scaled = src.scaled(
        size,
        size,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    x = max(0, (scaled.width() - size) // 2)
    y = max(0, (scaled.height() - size) // 2)
    out = QPixmap(size, size)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    path = QPainterPath()
    path.addEllipse(0.0, 0.0, float(size), float(size))
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, scaled, x, y, size, size)
    painter.end()
    return out


def bidi_unavailable(snap: UsageSnapshot) -> bool:
    """True when Zen Remote Agent / remote debugging is not reachable."""
    if snap.source == "unavailable":
        return True
    err = (snap.error or "").lower()
    return "no remote agent" in err or "remote-debugging-port" in err


def session_logged_out(snap: UsageSnapshot) -> bool:
    """True when scrape shows auth redirect / logged-out (needs headed sign-in)."""
    return snapshot_needs_login(snap)


def tray_tooltip(snap: UsageSnapshot) -> tuple[str, str]:
    """Return (title, body) for the StatusNotifierItem hover tooltip."""
    if bidi_unavailable(snap):
        return (
            "Cursor Spend — Browser inaccessible",
            "Usage hidden until the automation browser is reachable with remote debugging (see popup).",
        )
    if session_logged_out(snap):
        return (
            "Cursor Spend — Sign in required",
            "Sign in to Cursor in the dedicated browser window; usage refreshes automatically.",
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
    """Rotating circular-arrow (refresh) glyph while waiting for the browser."""
    size = 64
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    cx = cy = size / 2.0
    painter.translate(cx, cy)
    painter.rotate(angle_deg)

    r = size * 0.30
    stroke = max(4.0, size * 0.10)
    ink = QColor("#E8E8E8")
    pen = QPen(
        ink,
        stroke,
        Qt.PenStyle.SolidLine,
        Qt.PenCapStyle.RoundCap,
        Qt.PenJoinStyle.RoundJoin,
    )
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    # Open ring; gap leaves room for the arrowhead (Qt angles: 0° = east, CCW).
    tip_qt_deg = 55.0
    painter.drawArc(QRectF(-r, -r, 2 * r, 2 * r), int(tip_qt_deg * 16), int(-295 * 16))

    tip_a = math.radians(tip_qt_deg)
    tx = r * math.cos(tip_a)
    ty = -r * math.sin(tip_a)
    # Clockwise tangent / outward normal in screen space.
    tangent = (math.sin(tip_a), math.cos(tip_a))
    normal = (math.cos(tip_a), -math.sin(tip_a))
    ah = r * 0.48
    tip = QPointF(tx + tangent[0] * stroke * 0.15, ty + tangent[1] * stroke * 0.15)
    back = QPointF(tx - tangent[0] * ah * 0.55, ty - tangent[1] * ah * 0.55)
    left = QPointF(back.x() + normal[0] * ah * 0.5, back.y() + normal[1] * ah * 0.5)
    right = QPointF(back.x() - normal[0] * ah * 0.5, back.y() - normal[1] * ah * 0.5)

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(ink)
    painter.drawPolygon(QPolygonF([tip, left, right]))

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
        self._submenus: list[TrayContextSubmenu] = []

        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumWidth(220)
        self.setStyleSheet(_CTX_MENU_STYLE)

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

    def add_account_card(self) -> _CtxAccountCard:
        """Non-interactive account summary (avatar + email + plan)."""
        card = _CtxAccountCard(self)
        self._layout.addWidget(card)
        self._rows.append(card)
        return card

    def add_submenu(
        self,
        title: str,
        actions: list[QAction] | None = None,
    ) -> TrayContextSubmenu:
        submenu = TrayContextSubmenu(self, root_menu=self, actions=actions or [])
        self._submenus.append(submenu)
        row = _CtxSubmenuRow(title, submenu, host=self, root_menu=self)
        self._layout.addWidget(row)
        self._rows.append(row)
        return submenu

    def add_separator(self) -> QFrame:
        sep = QFrame(self)
        sep.setObjectName("ctxSep")
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        self._layout.addWidget(sep)
        self._rows.append(sep)
        return sep

    def close_child_flyouts(self) -> None:
        for submenu in self._submenus:
            submenu.hide()

    def close_submenus(self) -> None:
        self.close_child_flyouts()

    @staticmethod
    def _sync_row(row: QWidget, action: QAction) -> None:
        row.setVisible(action.isVisible())
        row.setEnabled(action.isEnabled())

    def popup_at(self, pos: QPoint) -> None:
        """Show near the tray icon; dismiss when focus leaves or user clicks away."""
        self._dismiss_armed = False
        self._arm_timer.stop()
        self.close_submenus()
        # Drop empty separators left by hidden actions.
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

    def _owns_window(self, window) -> bool:  # noqa: ANN001
        if window is None:
            return False
        if window is self.windowHandle():
            return True
        return any(submenu.owns_window(window) for submenu in self._submenus)

    def _contains_global(self, global_pos: QPoint) -> bool:
        if self.frameGeometry().contains(global_pos):
            return True
        return any(submenu.contains_global(global_pos) for submenu in self._submenus)

    def _arm_dismiss(self) -> None:
        self._dismiss_armed = True
        if self.isVisible() and not self._owns_window(QGuiApplication.focusWindow()):
            self.hide()

    def _on_focus_window_changed(self, window) -> None:  # noqa: ANN001
        if not self._dismiss_armed or not self.isVisible():
            return
        if self._owns_window(window):
            return
        self.hide()

    def changeEvent(self, event) -> None:  # noqa: ANN001
        if (
            event.type() == QEvent.Type.WindowDeactivate
            and self.isVisible()
            and self._dismiss_armed
            and not self._owns_window(QGuiApplication.focusWindow())
        ):
            self.hide()
        super().changeEvent(event)

    def hideEvent(self, event) -> None:  # noqa: ANN001
        self._arm_timer.stop()
        self._dismiss_armed = False
        self.close_submenus()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self._outside_filter)
            try:
                app.focusWindowChanged.disconnect(self._on_focus_window_changed)
            except TypeError:
                pass
        super().hideEvent(event)


class TrayContextSubmenu(QFrame):
    """Flyout panel for nested context-menu choices (one or more levels deep)."""

    def __init__(
        self,
        host: TrayContextMenu | TrayContextSubmenu,
        *,
        root_menu: TrayContextMenu,
        actions: list[QAction] | None = None,
    ) -> None:
        super().__init__(None)
        self._host = host
        self._root_menu = root_menu
        self._child_flyouts: list[TrayContextSubmenu] = []
        self._rows: list[QWidget] = []
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumWidth(160)
        self.setStyleSheet(_CTX_MENU_STYLE)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(6, 6, 6, 6)
        self._layout.setSpacing(2)
        for action in actions or []:
            self.add_action(action)

    def add_action(self, action: QAction) -> QWidget:
        row = _CtxMenuRow(action, self)
        action.changed.connect(lambda r=row, a=action: TrayContextMenu._sync_row(r, a))
        action.triggered.connect(self._root_menu.hide)
        TrayContextMenu._sync_row(row, action)
        self._layout.addWidget(row)
        self._rows.append(row)
        return row

    def add_submenu(
        self,
        title: str,
        actions: list[QAction] | None = None,
    ) -> TrayContextSubmenu:
        child = TrayContextSubmenu(self, root_menu=self._root_menu, actions=actions or [])
        self._child_flyouts.append(child)
        row = _CtxSubmenuRow(title, child, host=self, root_menu=self._root_menu)
        self._layout.addWidget(row)
        self._rows.append(row)
        return child

    def clear_actions(self) -> None:
        """Remove action rows (keeps nested submenu rows). Used to rebuild browser lists."""
        kept: list[QWidget] = []
        for row in self._rows:
            if isinstance(row, _CtxSubmenuRow):
                kept.append(row)
                continue
            self._layout.removeWidget(row)
            row.deleteLater()
        self._rows = kept

    def close_child_flyouts(self) -> None:
        for child in self._child_flyouts:
            child.hide()

    def owns_window(self, window) -> bool:  # noqa: ANN001
        if not self.isVisible():
            return any(c.owns_window(window) for c in self._child_flyouts)
        if window is self.windowHandle():
            return True
        return any(c.owns_window(window) for c in self._child_flyouts)

    def contains_global(self, global_pos: QPoint) -> bool:
        if self.isVisible() and self.frameGeometry().contains(global_pos):
            return True
        return any(c.contains_global(global_pos) for c in self._child_flyouts)

    def popup_beside(self, anchor: QWidget) -> None:
        self.adjustSize()
        screen = QApplication.screenAt(anchor.mapToGlobal(QPoint(0, 0))) or QApplication.primaryScreen()
        bounds = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        w = max(self.sizeHint().width(), 160)
        h = max(self.sizeHint().height(), 1)
        top_left = anchor.mapToGlobal(QPoint(0, 0))
        host_width = self._host.width() if self._host.isVisible() else self._root_menu.width()
        # Prefer opening to the right of the host panel; flip left if it would clip.
        x = top_left.x() + host_width - 4
        if x + w > bounds.right() - 4:
            x = self._host.x() - w + 4
        y = top_left.y() - 6
        x = min(max(x, bounds.left() + 4), bounds.right() - w - 4)
        y = min(max(y, bounds.top() + 4), bounds.bottom() - h - 4)
        self.setFixedWidth(w)
        self.setGeometry(x, y, w, h)
        self.show()
        self.move(x, y)
        self.raise_()

    def hideEvent(self, event) -> None:  # noqa: ANN001
        self.close_child_flyouts()
        super().hideEvent(event)


class _CtxAccountCard(QFrame):
    """Single non-interactive field: circular avatar spanning email + subscription."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ctxAccount")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(56)
        self.setStyleSheet(_CTX_ACCOUNT_STYLE)
        self.setCursor(Qt.CursorShape.ArrowCursor)

        self._avatar = QLabel()
        self._avatar.setFixedSize(_ACCOUNT_AVATAR_PX, _ACCOUNT_AVATAR_PX)
        self._avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._email = QLabel("Authenticated as: —")
        email_font = self._email.font()
        email_font.setPointSize(10)
        self._email.setFont(email_font)
        self._email.setStyleSheet("color: #F2F2F2;")
        self._email.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self._email.setWordWrap(False)

        self._plan = QLabel("Subscription: —")
        plan_font = self._plan.font()
        plan_font.setPointSize(10)
        self._plan.setFont(plan_font)
        self._plan.setStyleSheet("color: #B0B0B0;")
        self._plan.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)
        text_col.addWidget(self._email)
        text_col.addWidget(self._plan)

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 8, 10, 8)
        row.setSpacing(10)
        row.addWidget(self._avatar, alignment=Qt.AlignmentFlag.AlignVCenter)
        row.addLayout(text_col, stretch=1)

    def set_account(
        self,
        *,
        email: str | None,
        subscription: str | None,
        avatar: QPixmap | None = None,
        authenticated: bool = True,
    ) -> None:
        if not authenticated:
            self._email.setText("Not authenticated")
            self._email.setStyleSheet("color: #F2F2F2;")
            self._plan.setText("Sign in to Cursor to track spending")
            self._plan.setStyleSheet("color: #B0B0B0;")
            self._avatar.clear()
            return
        if email:
            self._email.setText(f"Authenticated as: {email}")
        else:
            self._email.setText("Authenticated")
        self._email.setStyleSheet("color: #F2F2F2;")
        self._plan.setText(f"Subscription: {subscription or '—'}")
        self._plan.setStyleSheet("color: #B0B0B0;")
        if avatar is not None and not avatar.isNull():
            self._avatar.setPixmap(_circular_avatar(avatar))
            self._avatar.show()
        else:
            self._avatar.clear()

    def enterEvent(self, event) -> None:  # noqa: ANN001
        parent = self.parent()
        if isinstance(parent, (TrayContextMenu, TrayContextSubmenu)):
            parent.close_child_flyouts()
        super().enterEvent(event)


class _CtxMenuRow(QFrame):
    """Left-aligned menu row with optional trailing checkmark (Notion-style)."""

    def __init__(self, action: QAction, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._action = action
        self.setObjectName("ctxRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(34)
        self.setStyleSheet(_CTX_ROW_STYLE)

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

    def enterEvent(self, event) -> None:  # noqa: ANN001
        # Hovering another item should dismiss sibling flyouts at this level
        # (QMenu-style). Nested hosts close only their children.
        parent = self.parent()
        if isinstance(parent, (TrayContextMenu, TrayContextSubmenu)):
            parent.close_child_flyouts()
        super().enterEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.MouseButton.LeftButton and self._action.isEnabled():
            self._action.trigger()
        super().mouseReleaseEvent(event)


class _CtxSubmenuRow(QFrame):
    """Parent-menu row that opens a flyout submenu."""

    def __init__(
        self,
        title: str,
        submenu: TrayContextSubmenu,
        *,
        host: TrayContextMenu | TrayContextSubmenu,
        root_menu: TrayContextMenu,
    ) -> None:
        super().__init__(host)
        self._submenu = submenu
        self._host = host
        self._root_menu = root_menu
        self.setObjectName("ctxRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(34)
        self.setStyleSheet(_CTX_ROW_STYLE)

        self._label = QLabel(title)
        label_font = self._label.font()
        label_font.setPointSize(10)
        self._label.setFont(label_font)
        self._label.setStyleSheet("color: #F2F2F2; padding-left: 10px;")
        self._label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )

        trailing = QLabel("›")
        trailing.setFixedWidth(22)
        trailing.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        trailing.setStyleSheet("color: #A0A0A0; padding-right: 10px;")

        row = QHBoxLayout(self)
        row.setContentsMargins(2, 0, 2, 0)
        row.setSpacing(8)
        row.addWidget(self._label, stretch=1)
        row.addWidget(trailing)

    def enterEvent(self, event) -> None:  # noqa: ANN001
        self._host.close_child_flyouts()
        self._submenu.popup_beside(self)
        super().enterEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.MouseButton.LeftButton:
            if self._submenu.isVisible():
                self._submenu.hide()
            else:
                self._host.close_child_flyouts()
                self._submenu.popup_beside(self)
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
        if self._menu._contains_global(global_pos):
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
        self.tray.secondary_activated.connect(self._on_secondary_activated)
        self.tray.context_menu_requested.connect(self._on_context_menu)

        self._ctx = TrayContextMenu()
        self._refresh_action = QAction("Refresh now", self)
        self._refresh_action.triggered.connect(self._refresh_now)
        self._keep_open_action = QAction("Keep open", self)
        self._keep_open_action.setToolTip(
            "Open the tray popup and leave it open until you click the tray icon again."
        )
        self._keep_open_action.triggered.connect(self._on_keep_open)
        self._autostart_action = QAction("Launch at login", self)
        self._autostart_action.setCheckable(True)
        self._autostart_action.setChecked(autostart.is_enabled())
        self._autostart_action.toggled.connect(self._on_autostart_toggled)
        self._poll_actions: list[QAction] = []
        self._poll_group = QActionGroup(self)
        self._poll_group.setExclusive(True)
        for minutes in POLL_INTERVAL_MINUTES:
            action = QAction(poll_interval_label(minutes), self)
            action.setCheckable(True)
            action.setData(minutes * 60)
            action.triggered.connect(
                lambda checked=False, m=minutes: self._on_poll_interval_chosen(m)
            )
            self._poll_group.addAction(action)
            self._poll_actions.append(action)
        self._sync_poll_actions()

        self._browser_actions: list[QAction] = []
        self._browser_group = QActionGroup(self)
        self._browser_group.setExclusive(True)
        self._between_keep_action = QAction("Keep open", self)
        self._between_keep_action.setCheckable(True)
        self._between_keep_action.setToolTip(
            "Leave the automation browser running between scrapes."
        )
        self._between_keep_action.triggered.connect(
            lambda checked=False: self._on_between_scrapes_chosen("keep_open")
        )
        self._between_quit_action = QAction("Quit between scrapes", self)
        self._between_quit_action.setCheckable(True)
        self._between_quit_action.setToolTip(
            "Stop the automation browser after each scrape and relaunch before the next."
        )
        self._between_quit_action.triggered.connect(
            lambda checked=False: self._on_between_scrapes_chosen("quit")
        )
        self._between_group = QActionGroup(self)
        self._between_group.setExclusive(True)
        self._between_group.addAction(self._between_keep_action)
        self._between_group.addAction(self._between_quit_action)
        self._sync_between_scrapes_actions()

        self._view_browser_action = QAction("View Browser", self)
        self._view_browser_action.setToolTip(
            "Open the selected automation browser on the Cursor spending page."
        )
        self._view_browser_action.triggered.connect(self._on_view_browser)

        self._avatar_url_cached: str | None = None
        self._avatar_pixmap = QPixmap()

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(QApplication.instance().quit)
        self._account_card = self._ctx.add_account_card()
        self._ctx.add_separator()
        self._ctx.add_action(self._refresh_action)
        self._ctx.add_action(self._keep_open_action)
        self._ctx.add_separator()
        self._ctx.add_submenu("Refresh interval", self._poll_actions)
        browser_menu = self._ctx.add_submenu("Browser")
        browser_menu.add_action(self._view_browser_action)
        self._automation_menu = browser_menu.add_submenu("Automation on")
        self._rebuild_browser_actions()
        between_menu = browser_menu.add_submenu("Between Scrapes")
        between_menu.add_action(self._between_keep_action)
        between_menu.add_action(self._between_quit_action)
        self._ctx.add_action(self._autostart_action)
        self._ctx.add_separator()
        self._ctx.add_action(quit_action)

        # Spinner animation (used while waiting for the automation browser to start)
        self._spin_angle = 0.0
        self._spin_timer = QTimer(self)
        self._spin_timer.setInterval(_SPIN_INTERVAL_MS)
        self._spin_timer.timeout.connect(self._on_spin_tick)
        self._login_launch_pending = False
        self._headless_switch_pending = False
        self._viewing_browser = False
        # Set when network watch detects sign-in: headless first, then scrape.
        self._refresh_after_headless = False
        self._warmup_only = False
        self._pending_scrape_ready: Callable[[], None] | None = None

        self._apply_snapshot(self.snapshot)
        self.show()
        self.tray.show()

        self.scheduler = RefreshScheduler(
            config,
            self,
            ensure_ready=self._ensure_browser_ready,
        )
        self.scheduler.snapshot_updated.connect(self._on_snapshot)
        self.scheduler.seconds_changed.connect(self.popup.set_remaining)
        self.scheduler.status_changed.connect(self.popup.set_status)
        self.scheduler.refreshing_changed.connect(self._on_refreshing)
        self.scheduler.browser_warmup_requested.connect(self._warmup_browser_for_scrape)
        self._login_watch = LoginNetworkWatcher(config, self)
        self._login_watch.detected.connect(self._on_login_network_detected)
        self._login_watch.status.connect(self.popup.set_status)
        self._logout_watch = LogoutNetworkWatcher(config, self)
        self._logout_watch.detected.connect(self._on_logout_network_detected)
        # Early cookie presence check: brand-new / never-signed-in profiles open
        # headed login immediately; profiles with a session cookie scrape in background.
        if self._prompt_login_if_no_session_cookie():
            self.scheduler.start(refresh=False)
        else:
            self.scheduler.start(refresh=True)

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
        needs_login = session_logged_out(snap) and not disconnected
        self.popup.apply_snapshot(snap)
        self.popup.set_browser_inaccessible(
            disconnected, self.config.zen_launch_command()
        )
        self.popup.set_awaiting_login(needs_login)
        self._refresh_action.setVisible(not disconnected)
        self._sync_account_actions(snap)
        self.tray.set_icon(
            make_tray_icon(
                snap.cursor_models_pct,
                snap.other_models_pct,
                disconnected=disconnected,
            )
        )
        title, body = tray_tooltip(snap)
        self.tray.set_tooltip(body, title=title)

    def _sync_account_actions(self, snap: UsageSnapshot) -> None:
        # Email is preferred; subscription alone still means a signed-in scrape.
        authenticated = (
            (bool(snap.account_email) or bool(snap.subscription_level))
            and not session_logged_out(snap)
        )
        if not authenticated:
            self._refresh_account_avatar(None)
            self._account_card.set_account(
                email=None,
                subscription=None,
                avatar=None,
                authenticated=False,
            )
            return
        self._refresh_account_avatar(snap.account_avatar_url)
        avatar = self._avatar_pixmap if not self._avatar_pixmap.isNull() else None
        self._account_card.set_account(
            email=snap.account_email,
            subscription=snap.subscription_level,
            avatar=avatar,
            authenticated=True,
        )

    def _refresh_account_avatar(self, url: str | None) -> None:
        """Download/cache the sidebar avatar as a full-color pixmap (not a disabled QIcon)."""
        if not url:
            self._avatar_url_cached = None
            self._avatar_pixmap = QPixmap()
            cache = data_dir() / "account_avatar.bin"
            url_file = data_dir() / "account_avatar.url"
            for path in (cache, url_file):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            return
        if url == self._avatar_url_cached and not self._avatar_pixmap.isNull():
            return
        cache = data_dir() / "account_avatar.bin"
        url_file = data_dir() / "account_avatar.url"
        try:
            data_dir().mkdir(parents=True, exist_ok=True)
            cached_url = url_file.read_text(encoding="utf-8").strip() if url_file.is_file() else None
            raw: bytes | None = None
            if cached_url == url and cache.is_file():
                raw = cache.read_bytes()
            else:
                with httpx.Client(timeout=8.0, follow_redirects=True) as client:
                    resp = client.get(url)
                    resp.raise_for_status()
                    raw = resp.content
                cache.write_bytes(raw)
                url_file.write_text(url, encoding="utf-8")
            pix = QPixmap()
            if not pix.loadFromData(raw):
                return
            self._avatar_pixmap = pix
            self._avatar_url_cached = url
        except Exception as exc:  # noqa: BLE001 — avatar is decorative
            log.debug("Could not load account avatar: %s", exc)

    def _refresh_now(self) -> None:
        # Release the BiDi/CDP watch session before a scrape (Firefox allows one).
        self._stop_login_network_watch()
        self._stop_logout_network_watch()
        self.scheduler.refresh()

    def _on_login_network_detected(self, url: str) -> None:
        """After sign-in traffic: flip to headless, then scrape (no manual refresh)."""
        log.info(
            "Sign-in network activity detected (%s) — switching to headless then refresh",
            url,
        )
        self._stop_login_network_watch()
        self._stop_logout_network_watch()
        self._login_launch_pending = False
        self._viewing_browser = False
        self._refresh_after_headless = True
        self.popup.set_status("Sign-in detected — switching to headless…")
        QTimer.singleShot(0, self._switch_to_headless_after_login)

    def _on_logout_network_detected(self, url: str) -> None:
        """After sign-out traffic in View Browser: signed-out UI + headed re-auth."""
        log.info(
            "Sign-out network activity detected (%s) — switching to signed-out UI",
            url,
        )
        self._stop_logout_network_watch()
        self._viewing_browser = False
        # Drop cached identity so the menu/tray match poll-logout behaviour.
        self.snapshot.account_email = None
        self.snapshot.subscription_level = None
        self.snapshot.account_avatar_url = None
        self.snapshot.cursor_models_pct = None
        self.snapshot.other_models_pct = None
        self.snapshot.source = "logged_out"
        self.snapshot.error = (
            "Signed out — sign in to Cursor in the dedicated browser window."
        )
        self.snapshot.save()
        self._apply_snapshot(self.snapshot)
        self.scheduler.pause_for_login()
        self.popup.set_awaiting_login(True)
        self.popup.set_status(
            "Signed out — sign in to Cursor in the dedicated browser window "
            "(refresh runs automatically when the dashboard loads)."
        )
        self._ensure_login_browser()

    def _start_login_network_watch(self) -> None:
        """Watch headed-browser traffic only while awaiting Cursor sign-in."""
        if not getattr(self.popup, "_awaiting_login", False):
            return
        if self.scheduler.is_refreshing():
            return
        self._stop_logout_network_watch()
        self._login_watch.start()

    def _stop_login_network_watch(self) -> None:
        if hasattr(self, "_login_watch"):
            self._login_watch.stop()

    def _start_logout_network_watch(self) -> None:
        """Watch headed View Browser traffic for Cursor sign-out."""
        if not self._viewing_browser:
            return
        # Known headless means View Browser ended; None = still starting (thread retries).
        if self.config.browser_is_headless() is True:
            return
        if getattr(self.popup, "_awaiting_login", False):
            return
        if self.scheduler.is_refreshing():
            return
        self._stop_login_network_watch()
        self._logout_watch.start()

    def _stop_logout_network_watch(self) -> None:
        if hasattr(self, "_logout_watch"):
            self._logout_watch.stop()

    def _on_refreshing(self, refreshing: bool) -> None:
        self.popup.set_refreshing(refreshing)
        self.popup.set_remaining(self.scheduler.remaining_seconds())
        if refreshing:
            self._stop_login_network_watch()
            self._stop_logout_network_watch()
        elif self._viewing_browser:
            # Resume logout watch after a scrape while View Browser stays headed.
            QTimer.singleShot(500, self._start_logout_network_watch)

    def _on_snapshot(self, snap: object) -> None:
        assert isinstance(snap, UsageSnapshot)
        self.snapshot = snap
        self._apply_snapshot(snap)
        # Single entry for first launch, browser switch, and poll failures.
        if self._prompt_login_if_needed(snap):
            self._stop_logout_network_watch()
            return
        self._stop_login_network_watch()
        if snap.source in ("bidi", "cdp") and not snap.error:
            self._login_launch_pending = False
            if self.config.browser_is_headless() is False:
                # Manual "View Browser" stays headed; login windows flip back to headless.
                if self._viewing_browser:
                    self._headless_switch_pending = False
                    QTimer.singleShot(500, self._start_logout_network_watch)
                elif not self._headless_switch_pending:
                    self._stop_logout_network_watch()
                    QTimer.singleShot(0, self._switch_to_headless_after_login)
            else:
                self._headless_switch_pending = False
                self._stop_logout_network_watch()
                if self.config.between_scrapes == "quit":
                    QTimer.singleShot(0, self._quit_browser_between_scrapes)

    def _prompt_login_if_needed(self, snap: UsageSnapshot) -> bool:
        """Open headed sign-in when scrape hit auth/bot page instead of spending.

        Shared by first launch, browser switches, and scheduled polls.
        Returns True when login flow was started or is already in progress.
        """
        if not snapshot_needs_login(snap):
            return False
        self.scheduler.pause_for_login()
        self.popup.set_awaiting_login(True)
        self._ensure_login_browser()
        return True

    def _prompt_login_if_no_session_cookie(self) -> bool:
        """Fast path: no WorkosCursorSessionToken in the profile → headed login now.

        Used at startup and browser switch before any scrape settle delay.
        Returns True when login was prompted (caller should skip headless scrape).
        """
        if profile_has_cursor_session_cookie(self.config.browser, app_name=APP_NAME):
            log.info(
                "Cursor session cookie present in %s profile — background auth scrape OK",
                self.config.browser.display_name,
            )
            return False
        name = self.config.browser.display_name
        log.info(
            "No WorkosCursorSessionToken in %s profile — opening sign-in early",
            name,
        )
        self.popup.set_status(f"No Cursor session in {name} — opening sign-in…")
        # Drop stale identity from a previous session so the menu shows logged-out.
        self.snapshot.account_email = None
        self.snapshot.subscription_level = None
        self.snapshot.account_avatar_url = None
        self.snapshot.source = "logged_out"
        self.snapshot.save()
        self._sync_account_actions(self.snapshot)
        self.popup.set_awaiting_login(True)
        if hasattr(self, "scheduler"):
            self.scheduler.pause_for_login()
        self._ensure_login_browser()
        return True

    def _ensure_login_browser(self) -> None:
        """Open a headed dedicated-profile window so the user can sign into Cursor."""
        headless = self.config.browser_is_headless()
        if headless is False:
            self._login_launch_pending = False
            self.popup.set_status(
                f"Sign in to Cursor in the {self.config.browser.display_name} window "
                "(refresh runs automatically when the dashboard loads)."
            )
            self._start_login_network_watch()
            return
        if self._login_launch_pending:
            return
        self._login_launch_pending = True
        name = self.config.browser.display_name
        if headless is True:
            self.popup.set_status(f"Restarting {name} with a visible window for sign-in…")
            QTimer.singleShot(0, self._restart_headed_for_login)
        else:
            self.popup.set_status(f"Opening {name} for Cursor sign-in…")
            QTimer.singleShot(0, self._launch_headed_login)

    def _restart_headed_for_login(self) -> None:
        if self.config.browser_is_headless() is True:
            if not self.config.stop_browser(timeout=8.0):
                self._login_launch_pending = False
                self.popup.set_status(
                    f"Could not stop headless {self.config.browser.display_name} "
                    "to open a sign-in window."
                )
                return
        self._launch_headed_login()

    def _launch_headed_login(self) -> None:
        if self.config.browser_is_headless() is False:
            self._login_launch_pending = False
            self.popup.set_status(
                f"Sign in to Cursor in the {self.config.browser.display_name} window "
                "(refresh runs automatically when the dashboard loads)."
            )
            self._start_login_network_watch()
            return
        argv = self.config.browser_login_argv()
        try:
            subprocess.Popen(
                argv,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            log.exception("Failed to launch headed %s for login", self.config.browser.display_name)
            self._login_launch_pending = False
            self.popup.set_status(f"Could not open sign-in window: {exc}")
            return

        self._start_spinner()
        self.popup.set_status(
            f"Sign in to Cursor in the {self.config.browser.display_name} window…"
        )
        # Watch network once remote debugging is up; auto-refresh after dashboard API traffic.
        QTimer.singleShot(2_000, self._stop_spinner)
        QTimer.singleShot(2_500, self._start_login_network_watch)

    def _on_view_browser(self) -> None:
        """Open the selected automation browser headed on the Cursor spending page."""
        self._viewing_browser = True
        self._login_launch_pending = False
        self._headless_switch_pending = False
        name = self.config.browser.display_name
        if self.config.browser_is_headless() is False:
            self.popup.set_status(f"{name} is already open on the spending page.")
            QTimer.singleShot(500, self._start_logout_network_watch)
            return
        if self.config.browser_is_running():
            self.popup.set_status(f"Opening {name} on the spending page…")
            QTimer.singleShot(0, self._restart_headed_for_view)
            return
        self.popup.set_status(f"Opening {name} on the spending page…")
        QTimer.singleShot(0, self._launch_headed_view)

    def _restart_headed_for_view(self) -> None:
        if self.config.browser_is_headless() is True:
            if not self.config.stop_browser(timeout=8.0):
                self._viewing_browser = False
                self._stop_logout_network_watch()
                self.popup.set_status(
                    f"Could not stop headless {self.config.browser.display_name} "
                    "to open a visible window."
                )
                return
        self._launch_headed_view()

    def _launch_headed_view(self) -> None:
        if self.config.browser_is_headless() is False:
            self.popup.set_status(
                f"{self.config.browser.display_name} is already open on the spending page."
            )
            QTimer.singleShot(500, self._start_logout_network_watch)
            return
        argv = self.config.browser_login_argv()
        try:
            subprocess.Popen(
                argv,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            log.exception(
                "Failed to launch headed %s for View Browser",
                self.config.browser.display_name,
            )
            self._viewing_browser = False
            self._stop_logout_network_watch()
            self.popup.set_status(f"Could not open browser: {exc}")
            return

        self._start_spinner()
        self.popup.set_status(
            f"{self.config.browser.display_name} open on the Cursor spending page."
        )
        QTimer.singleShot(2_000, self._stop_spinner)
        # Watch for sign-out once remote debugging is up (headed View Browser only).
        QTimer.singleShot(2_500, self._start_logout_network_watch)

    def _switch_to_headless_after_login(self) -> None:
        """Close the headed login window and relaunch the dedicated profile headless.

        When ``_refresh_after_headless`` is set (network login detection), scrape
        after the headless browser is up. Otherwise this is the post-scrape flip
        from a manual/headed success path.
        """
        if self._headless_switch_pending:
            return
        if self._viewing_browser:
            self._refresh_after_headless = False
            return
        self._stop_logout_network_watch()
        refresh_after = self._refresh_after_headless
        self._refresh_after_headless = False

        headless = self.config.browser_is_headless()
        if headless is not False:
            # Already headless or not running — scrape if login detection asked for it.
            self._headless_switch_pending = False
            if refresh_after:
                self.scheduler.refresh()
            return

        self._headless_switch_pending = True
        name = self.config.browser.display_name
        self.popup.set_status(f"Sign-in complete — switching {name} to headless…")
        self._start_spinner()
        if not self.config.stop_browser(timeout=8.0):
            self._headless_switch_pending = False
            self._stop_spinner()
            self.popup.set_status(f"Could not restart {name} in headless mode.")
            if refresh_after:
                # Last resort: scrape while still headed so the user is not stuck.
                self.scheduler.refresh()
            return
        if self.config.between_scrapes == "quit":
            self._headless_switch_pending = False
            self._stop_spinner()
            if refresh_after:
                # ensure_ready will launch headless before this scrape.
                self.popup.set_status(
                    f"Sign-in complete — launching {name} headless for refresh…"
                )
                self.scheduler.refresh()
            else:
                self.popup.set_status(
                    f"Sign-in complete — {name} will relaunch on the next refresh."
                )
            return
        argv = self.config.browser_launch_argv(headless=True)
        try:
            subprocess.Popen(
                argv,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            log.exception("Failed to relaunch headless %s after login", name)
            self._headless_switch_pending = False
            self._stop_spinner()
            self.popup.set_status(f"Could not relaunch headless {name}: {exc}")
            if refresh_after:
                self.scheduler.refresh()
            return
        self._viewing_browser = False
        # _arm_launch_retry → probe → scheduler.refresh() once remote debugging is up.
        self._arm_launch_retry()

    def _on_activated(self, pos: QPoint) -> None:
        self._anchor_pos = QPoint(pos)
        if self._ctx.isVisible():
            self._ctx.hide()
            return
        # Tray click again while open → close (also closes keep-open / pinned).
        if self.popup.isVisible():
            self.popup.hide()
            return
        self.popup.show_at(self._popup_position())

    def _on_secondary_activated(self, pos: QPoint) -> None:
        """Middle-click: refresh usage and open the popup."""
        self._anchor_pos = QPoint(pos)
        if self._ctx.isVisible():
            self._ctx.hide()
        self._refresh_now()
        if not self.popup.isVisible():
            self.popup.show_at(self._popup_position())

    def _on_keep_open(self) -> None:
        """Open the popup pinned — outside clicks / focus loss do not dismiss it."""
        if self._ctx.isVisible():
            self._ctx.hide()
        self.popup.show_at(self._popup_position(), keep_open=True)

    def _on_context_menu(self, pos: QPoint) -> None:
        self._anchor_pos = QPoint(pos)
        if self.popup.isVisible():
            self.popup.hide()
        if self._ctx.isVisible():
            self._ctx.hide()
            return
        self._autostart_action.blockSignals(True)
        self._autostart_action.setChecked(autostart.is_enabled())
        self._autostart_action.blockSignals(False)
        self._sync_poll_actions()
        self._rebuild_browser_actions()
        self._sync_between_scrapes_actions()
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

    @staticmethod
    def _set_action_checked(action: QAction, checked: bool) -> None:
        """Update check state without re-entering triggered handlers; refresh row UI."""
        action.blockSignals(True)
        action.setChecked(checked)
        action.blockSignals(False)
        # blockSignals swallows QAction.changed, so custom menu rows need a nudge.
        action.changed.emit()

    def _sync_poll_actions(self) -> None:
        current = self.config.poll_seconds
        for action in self._poll_actions:
            self._set_action_checked(action, action.data() == current)

    def _rebuild_browser_actions(self) -> None:
        for action in self._browser_actions:
            self._browser_group.removeAction(action)
            action.deleteLater()
        self._browser_actions.clear()
        self._automation_menu.clear_actions()

        browsers = self.config.installed_browsers()
        current_key = self.config.browser.key
        if not browsers:
            empty = QAction("No supported browsers found", self)
            empty.setEnabled(False)
            self._browser_actions.append(empty)
            self._automation_menu.add_action(empty)
            return

        for info in browsers:
            action = QAction(info.display_name, self)
            action.setCheckable(True)
            action.setData(info.key)
            action.setToolTip(f"{info.family.value.title()}-family · {info.binary}")
            action.triggered.connect(
                lambda checked=False, key=info.key: self._on_browser_chosen(key)
            )
            self._browser_group.addAction(action)
            self._browser_actions.append(action)
            self._automation_menu.add_action(action)
            self._set_action_checked(action, info.key == current_key)

    def _sync_between_scrapes_actions(self) -> None:
        mode = self.config.between_scrapes
        self._set_action_checked(self._between_keep_action, mode == "keep_open")
        self._set_action_checked(self._between_quit_action, mode == "quit")

    def _on_browser_chosen(self, key: str) -> None:
        if self.config.browser.key == key:
            self._rebuild_browser_actions()
            return
        if self.config.browser_is_running():
            self.config.stop_browser(timeout=8.0)
        info = self.config.set_browser_key(key)
        self._rebuild_browser_actions()
        # New profile may have no Cursor session — cookie check first (instant),
        # otherwise scrape so auth/bot pages still open headed sign-in.
        self._login_launch_pending = False
        self._headless_switch_pending = False
        self._viewing_browser = False
        self._stop_logout_network_watch()
        if self._prompt_login_if_no_session_cookie():
            return
        self.popup.set_status(
            f"Automation browser: {info.display_name} — checking sign-in…"
        )
        self.scheduler.refresh()

    def _on_between_scrapes_chosen(self, mode: BetweenScrapesMode) -> None:
        if self.config.between_scrapes == mode:
            self._sync_between_scrapes_actions()
            return
        self.config.set_between_scrapes(mode)
        self._sync_between_scrapes_actions()
        if mode == "keep_open":
            self.popup.set_status("Browser will stay open between scrapes")
            if not self.config.browser_is_running():
                self._launch_browser()
            return
        self.popup.set_status("Browser will quit between scrapes")
        scraping = hasattr(self, "scheduler") and self.scheduler.is_refreshing()
        if (
            not scraping
            and self.config.browser_is_running()
            and self.config.browser_is_headless() is not False
        ):
            self._quit_browser_between_scrapes()

    def _quit_browser_between_scrapes(self) -> None:
        if self.config.between_scrapes != "quit":
            return
        if self.config.browser_is_headless() is False:
            return
        if not self.config.browser_is_running():
            return
        name = self.config.browser.display_name
        if self.config.stop_browser(timeout=8.0):
            log.info("Stopped dedicated %s between scrapes", name)
        else:
            log.warning("Could not stop dedicated %s between scrapes", name)

    def _ensure_browser_ready(self, on_ready: Callable[[], None]) -> None:
        """Launch the automation browser if needed, then invoke on_ready."""
        launch_pending = (
            hasattr(self, "_launch_retry_timer") and self._launch_retry_timer.isActive()
        )
        if self.config.browser_is_running():
            # Warmup may have the process up before Remote Agent is ready — wait.
            if launch_pending or self._warmup_only:
                self._pending_scrape_ready = on_ready
                self._warmup_only = False
                return
            on_ready()
            return
        already_waiting = self._pending_scrape_ready is not None
        self._pending_scrape_ready = on_ready
        self._warmup_only = False
        if already_waiting or launch_pending:
            return
        self._launch_browser()

    def _warmup_browser_for_scrape(self) -> None:
        """Pre-launch headless browser ~10s before the poll deadline (quit-between)."""
        if self.config.between_scrapes != "quit":
            return
        if self.config.browser_is_running():
            return
        if self._pending_scrape_ready is not None:
            return
        launch_pending = (
            hasattr(self, "_launch_retry_timer") and self._launch_retry_timer.isActive()
        )
        if launch_pending:
            return
        name = self.config.browser.display_name
        self._warmup_only = True
        self.popup.set_status(f"Starting {name} for upcoming scrape…")
        self._launch_browser()

    def _on_poll_interval_chosen(self, minutes: int) -> None:
        seconds = minutes * 60
        if self.config.poll_seconds == seconds:
            self._sync_poll_actions()
            return
        self.scheduler.set_poll_seconds(seconds)
        self._sync_poll_actions()
        self.popup.set_status(f"Refresh every {poll_interval_label(minutes)}")

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
        self.tray.set_icon(make_spinner_icon(self._spin_angle))
        self._spin_timer.start()

    def _stop_spinner(self) -> None:
        self._spin_timer.stop()

    def _on_spin_tick(self) -> None:
        self._spin_angle = (self._spin_angle + 18) % 360  # one full rotation per ~1.6s
        self.tray.set_icon(make_spinner_icon(self._spin_angle))

    def _launch_browser(self) -> None:
        """Start the dedicated automation browser (headless) and poll until reachable."""
        if self.config.browser_is_running():
            return

        self._viewing_browser = False
        self._stop_logout_network_watch()
        argv = self.config.browser_launch_argv()
        try:
            subprocess.Popen(
                argv,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            log.exception("Failed to launch %s", self.config.browser.display_name)
            self._pending_scrape_ready = None
            self._warmup_only = False
            self.scheduler.cancel_awaiting_ready()
            self.popup.set_status(
                f"Could not start {self.config.browser.display_name} "
                "(see popup for the launch command)."
            )
            return

        self._start_spinner()
        if not self._warmup_only:
            self.popup.set_status(f"Starting {self.config.browser.display_name}…")
        self._arm_launch_retry()

    def _arm_launch_retry(self) -> None:
        if not hasattr(self, "_launch_retry_timer"):
            self._launch_retry_timer = QTimer(self)
            self._launch_retry_timer.setSingleShot(True)
            self._launch_retry_timer.timeout.connect(self._refresh_after_launch)
        self._launch_retry_timer.start(_LAUNCH_SETTLE_MS)

    def _refresh_after_launch(self) -> None:
        """Probe remote debugging; if up scrape (or finish warmup), otherwise retry."""

        def on_available() -> None:
            pending = self._pending_scrape_ready
            self._pending_scrape_ready = None
            warmup = self._warmup_only
            self._warmup_only = False
            if pending is not None:
                pending()
            elif warmup:
                self._stop_spinner()
                self.popup.set_status("")
                log.info(
                    "Warmup complete — %s ready for scheduled scrape",
                    self.config.browser.display_name,
                )
            else:
                self.scheduler.refresh()

        self.scheduler.probe_or_refresh(
            on_unavailable=self._reschedule_launch_retry,
            on_available=on_available,
        )

    def _reschedule_launch_retry(self) -> None:
        self._launch_retry_timer.start(_LAUNCH_RETRY_MS)

    def shutdown(self) -> None:
        """Stop polling and tear down the dedicated automation browser on quit."""
        self._stop_login_network_watch()
        self._stop_logout_network_watch()
        if hasattr(self, "scheduler"):
            self.scheduler.stop()
        if hasattr(self, "_launch_retry_timer"):
            self._launch_retry_timer.stop()
        self._stop_spinner()
        if not self.config.browser_is_running():
            return
        name = self.config.browser.display_name
        if self.config.stop_browser(timeout=8.0):
            log.info("Stopped dedicated %s on quit", name)
        else:
            log.warning("Dedicated %s did not exit cleanly on quit", name)
