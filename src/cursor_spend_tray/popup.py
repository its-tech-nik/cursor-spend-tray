from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, QPoint, QRectF, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .config import UsageSnapshot


class CopyableCommand(QLabel):
    """Monospace command chip — click copies the command to the clipboard."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._command = ""
        self._reset = QTimer(self)
        self._reset.setSingleShot(True)
        self._reset.timeout.connect(self._show_command)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        font = QFont("monospace")
        font.setStyleHint(QFont.StyleHint.TypeWriter)
        font.setPointSize(9)
        self.setFont(font)
        self.setStyleSheet(
            """
            QLabel {
                color: #D8D8D8;
                background: #242424;
                border: 1px solid #3A3A3A;
                border-radius: 6px;
                padding: 8px 10px;
            }
            QLabel:hover {
                color: #FFFFFF;
                background: #2C2C2C;
                border-color: #4A4A4A;
            }
            """
        )
        self.setToolTip("Click to copy")
        self.hide()

    def set_command(self, command: str) -> None:
        self._command = command.strip()
        self._show_command()
        self.setVisible(bool(self._command))

    def _show_command(self) -> None:
        self.setText(self._command or "")

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.MouseButton.LeftButton and self._command:
            QGuiApplication.clipboard().setText(self._command)
            self.setText("Copied!")
            self._reset.start(1200)
        super().mouseReleaseEvent(event)


class BrowserHelpBanner(QFrame):
    """Shown when Zen Remote Agent is unreachable."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("browserHelp")
        self.setStyleSheet(
            """
            QFrame#browserHelp {
                background: #1F1A14;
                border: 1px solid #3D3428;
                border-radius: 10px;
            }
            """
        )

        self._title = QLabel("Browser inaccessible")
        title_font = QFont()
        title_font.setPointSize(11)
        title_font.setWeight(QFont.Weight.DemiBold)
        self._title.setFont(title_font)
        self._title.setStyleSheet("color: #E8DCC8; background: transparent; border: none;")

        self._hint = QLabel(
            "Start the dedicated Zen profile with remote debugging so the tray can read spending:"
        )
        hint_font = QFont()
        hint_font.setPointSize(9)
        self._hint.setFont(hint_font)
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet("color: #A89880; background: transparent; border: none;")

        self.command = CopyableCommand()
        self.command.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(self._title)
        layout.addWidget(self._hint)
        layout.addWidget(self.command)
        self.hide()

    def set_launch_command(self, command: str) -> None:
        self.command.set_command(command)


class UsageRing(QWidget):
    """Circular usage gauge with a short label underneath."""

    def __init__(
        self,
        title: str,
        fill: str,
        *,
        warn_at_70: bool = False,
        tooltip: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._ring = _RingGlyph(fill, warn_at_70=warn_at_70)
        self._title = QLabel(title)

        title_font = QFont()
        title_font.setPointSize(11)
        title_font.setWeight(QFont.Weight.DemiBold)
        title_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.5)
        self._title.setFont(title_font)
        self._title.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self._title.setFixedHeight(22)
        self._title.setStyleSheet("color: #ECECEC; background: transparent; border: none;")

        if tooltip:
            self.setToolTip(tooltip)
            self._ring.setToolTip(tooltip)

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(160)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self._ring, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self._title)

    def set_percent(self, value: int | None) -> None:
        self._ring.set_percent(value)


class _RingGlyph(QWidget):
    """Donut chart with the percentage in the center."""

    def __init__(
        self,
        fill: str,
        *,
        warn_at_70: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._fill = QColor(fill)
        self._warn_at_70 = warn_at_70
        self._pct: int | None = None
        self.setFixedSize(136, 136)

    def set_percent(self, value: int | None) -> None:
        self._pct = value
        self.update()

    def _arc_color(self) -> QColor:
        pct = self._pct
        if pct is None:
            return self._fill
        if pct >= 90:
            return QColor("#D9897A")
        if self._warn_at_70 and pct >= 70:
            return QColor("#D0B56C")
        return self._fill

    def paintEvent(self, event) -> None:  # noqa: ANN001
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        stroke = 12.0
        margin = stroke / 2.0 + 3.0
        rect = QRectF(margin, margin, self.width() - 2 * margin, self.height() - 2 * margin)

        track = QPen(
            QColor("#2A2A2A"),
            stroke,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
        )
        painter.setPen(track)
        painter.drawEllipse(rect)

        if self._pct is not None:
            clamped = max(0, min(100, self._pct))
            if clamped > 0:
                fill = QPen(
                    self._arc_color(),
                    stroke,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.FlatCap if clamped >= 97 else Qt.PenCapStyle.RoundCap,
                )
                painter.setPen(fill)
                if clamped >= 100:
                    painter.drawEllipse(rect)
                else:
                    painter.drawArc(rect, 90 * 16, int(-360 * 16 * clamped / 100))

        painter.setPen(QColor("#ECECEC"))
        pct_font = QFont()
        pct_font.setPointSize(18)
        pct_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(pct_font)
        label = "—" if self._pct is None else f"{max(0, min(100, self._pct))}%"
        text_rect = QRectF(0, self.height() * 0.28, self.width(), 36)
        painter.drawText(text_rect, int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter), label)

        caption = QFont()
        caption.setPointSize(8)
        painter.setFont(caption)
        painter.setPen(QColor("#8B8B8B"))
        used = "" if self._pct is None else "used"
        used_rect = QRectF(0, self.height() * 0.52, self.width(), 20)
        painter.drawText(used_rect, int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter), used)
        painter.end()


class CountdownLabel(QLabel):
    """Clickable countdown — click triggers an immediate refresh."""

    clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = QFont()
        font.setPointSize(10)
        self.setFont(font)
        self.setStyleSheet(
            """
            QLabel {
                color: #A0A0A0;
                padding: 6px 8px;
                border-radius: 6px;
            }
            QLabel:hover {
                color: #E8E8E8;
                background: #2C2C2C;
            }
            """
        )
        self.setToolTip("Click to refresh now")

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def set_remaining(self, seconds: int, refreshing: bool = False) -> None:
        if refreshing:
            self.setText("Refreshing… · click to retry")
            return
        if seconds < 0:
            self.setText("Updates paused · click to retry")
            return
        minutes, secs = divmod(max(0, seconds), 60)
        self.setText(f"Next update in {minutes:02d}:{secs:02d} · click to refresh")


class SpendPopup(QFrame):
    """Frameless tray panel. Closes when focus leaves or the user clicks outside."""

    refresh_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._refreshing = False
        self._dismiss_armed = False
        self._arm_timer = QTimer(self)
        self._arm_timer.setSingleShot(True)
        self._arm_timer.timeout.connect(self._arm_dismiss)
        self._outside_filter = _OutsideClickFilter(self)

        # Tool (not Popup): under Plasma/XWayland, Popup often never deactivates on
        # outside click. Tool + focus tracking matches normal tray-panel dismiss.
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedWidth(420)
        self.setStyleSheet(
            """
            SpendPopup {
                background: #141414;
                border: 1px solid #2A2A2A;
                border-radius: 12px;
            }
            """
        )

        self._card = QFrame()
        self._card.setStyleSheet(
            """
            QFrame {
                background: #1B1B1B;
                border: 1px solid #2A2A2A;
                border-radius: 10px;
            }
            """
        )

        self._heading = QLabel("Included in Pro")
        heading_font = QFont()
        heading_font.setPointSize(12)
        heading_font.setWeight(QFont.Weight.DemiBold)
        self._heading.setFont(heading_font)
        self._heading.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._heading.setStyleSheet("color: #F2F2F2; background: transparent; border: none;")

        self.cursor_ring = UsageRing(
            "AUTO",
            "#8BA4C7",
            tooltip="Cursor Models — includes Cursor Grok and Composer. Extra usage consumes API quota or on-demand spend.",
        )
        self.other_ring = UsageRing(
            "API",
            "#B0B0B0",
            warn_at_70=True,
            tooltip="Other Models — additional usage beyond limits consumes on-demand spend.",
        )

        rings = QHBoxLayout()
        rings.setContentsMargins(0, 8, 0, 4)
        rings.setSpacing(16)
        rings.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        rings.addWidget(self.cursor_ring, stretch=1)
        rings.addWidget(self.other_ring, stretch=1)

        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        card_layout.setSpacing(8)
        card_layout.addWidget(self._heading)
        card_layout.addLayout(rings)

        self.browser_help = BrowserHelpBanner()

        self.countdown = CountdownLabel()
        self.countdown.clicked.connect(self.refresh_requested.emit)

        self.status = QLabel("")
        status_font = QFont()
        status_font.setPointSize(9)
        self.status.setFont(status_font)
        self.status.setStyleSheet("color: #777; background: transparent; border: none;")
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        root.addWidget(self._card)
        root.addWidget(self.browser_help)
        root.addWidget(self.countdown)
        root.addWidget(self.status)

    def show_at(self, pos) -> None:  # noqa: ANN001
        """Show near tray; close when focus leaves or user clicks elsewhere."""
        self._dismiss_armed = False
        self._arm_timer.stop()
        self.adjustSize()
        target = pos if isinstance(pos, QPoint) else QPoint(pos.x(), pos.y())
        # Under XWayland a single pre- or post-show move() is often ignored.
        self.setGeometry(target.x(), target.y(), self.width(), self.height())
        self.show()
        self.move(target)
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        QTimer.singleShot(0, lambda p=target: self.move(p) if self.isVisible() else None)
        QTimer.singleShot(50, lambda p=target: self.move(p) if self.isVisible() else None)

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self._outside_filter)
            # focusWindowChanged is the reliable leave signal on Plasma/XWayland.
            try:
                app.focusWindowChanged.disconnect(self._on_focus_window_changed)
            except TypeError:
                pass
            app.focusWindowChanged.connect(self._on_focus_window_changed)

        # Grace period: the tray Activate that opened us must not instantly dismiss.
        self._arm_timer.start(300)

    def _arm_dismiss(self) -> None:
        self._dismiss_armed = True
        # If focus already left during the grace period, close now.
        if self.isVisible() and QGuiApplication.focusWindow() is not self.windowHandle():
            self.hide()

    def _on_focus_window_changed(self, window) -> None:  # noqa: ANN001
        if not self._dismiss_armed or not self.isVisible():
            return
        if window is self.windowHandle():
            return
        self.hide()

    def dismiss_if_outside(self, global_pos: QPoint) -> bool:
        """Hide when a press lands outside this panel. Returns True if dismissed."""
        if not self._dismiss_armed or not self.isVisible():
            return False
        if self.frameGeometry().contains(global_pos):
            return False
        self.hide()
        return True

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

    def apply_snapshot(self, snap: UsageSnapshot) -> None:
        self.cursor_ring.set_percent(snap.cursor_models_pct)
        self.other_ring.set_percent(snap.other_models_pct)

    def set_browser_inaccessible(self, inaccessible: bool, launch_command: str = "") -> None:
        """Show or hide the Browser inaccessible banner with a copyable launch command."""
        if inaccessible:
            self._card.hide()
            self.browser_help.set_launch_command(launch_command)
            self.browser_help.show()
            self.countdown.hide()
        else:
            self._card.show()
            self.browser_help.hide()
            self.countdown.show()
        self.adjustSize()

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    def set_remaining(self, seconds: int) -> None:
        self.countdown.set_remaining(seconds, refreshing=self._refreshing)

    def set_refreshing(self, refreshing: bool) -> None:
        self._refreshing = refreshing


class _OutsideClickFilter(QObject):
    """Dismiss the panel when a mouse press happens outside its geometry."""

    def __init__(self, popup: SpendPopup) -> None:
        super().__init__(popup)
        self._popup = popup

    def eventFilter(self, obj, event) -> bool:  # noqa: ANN001
        if event.type() not in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonDblClick,
        ):
            return False
        if not self._popup.isVisible() or not self._popup._dismiss_armed:
            return False
        # Global coordinates — works for presses on other top-levels too.
        try:
            global_pos = event.globalPosition().toPoint()
        except AttributeError:
            global_pos = event.globalPos()
        if self._popup.frameGeometry().contains(global_pos):
            return False
        self._popup.hide()
        return False


# Alias kept for older imports
SpendPanel = SpendPopup
