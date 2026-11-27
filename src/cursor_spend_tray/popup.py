from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from datetime import time as time_of_day
from time import monotonic

from PyQt6.QtCore import (
    QEasingCurve,
    QEvent,
    QMimeData,
    QObject,
    QParallelAnimationGroup,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    QThread,
    QTimer,
    QUrl,
    Qt,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QDesktopServices,
    QDrag,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMenu,
    QScrollArea,
    QScrollBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .config import (
    DEFAULT_GITHUB_PROJECT_SORT,
    DEFAULT_POPUP_OPEN_ANIMATION,
    DEFAULT_TRAY_METRIC,
    GITHUB_PROJECT_SORT_LABELS,
    GITHUB_PROJECT_SORTS,
    TRAY_METRICS,
    GithubProjectSort,
    PopupOpenAnimation,
    TrayMetric,
    UsageSnapshot,
    format_usage_reset_label,
    load_github_project_sort,
    load_popup_panel_order,
    normalize_github_project_sort,
    save_github_project_sort,
    save_popup_panel_order,
)
from .billing import period_label as billing_period_label
from .billing import short_period_label as billing_short_period_label
from .github_stats import (
    BRANCH_PREF_DEV,
    BRANCH_PREF_MAIN,
    GithubHabitsPreview,
    GithubProjectRow,
    collect_github_habits_preview,
    github_preview_for_day,
    peek_cached_github_habits_preview,
    refresh_github_project_branch,
    save_github_branch_pref,
)
from .qt_threads import stop_qthread
from .sdk_stats import SdkHabitsBatch, SdkHabitsPreview, collect_habits_preview
from .usage_csv import (
    UsageCsvPreview,
    load_period_daily_series,
    load_usage_preview,
)
from .vscdb_stats import (
    VscdbHabitsPreview,
    collect_vscdb_habits_preview,
    peek_cached_vscdb_habits_preview,
)

PANEL_MIME = "application/x-cursor-spend-tray-panel"


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
            "Start the dedicated browser profile with remote debugging so the tray can read spending:"
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

    clicked = pyqtSignal()

    # glyph + layout spacing + title + spacing + subtitle (margins are 0 vertically)
    _BODY_HEIGHT = 104 + 4 + 20 + 4 + 16

    def __init__(
        self,
        title: str,
        fill: str,
        *,
        subtitle: str = "",
        warn_at_70: bool = False,
        tooltip: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._ring = _RingGlyph(fill, warn_at_70=warn_at_70)
        self._title = QLabel(title)
        self._selected = False

        title_font = QFont()
        title_font.setPointSize(10)
        title_font.setWeight(QFont.Weight.DemiBold)
        title_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
        self._title.setFont(title_font)
        self._title.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self._title.setFixedHeight(20)
        self._title.setStyleSheet("color: #ECECEC; background: transparent; border: none;")

        self._subtitle = QLabel(subtitle)
        sub_font = QFont()
        sub_font.setPointSize(8)
        self._subtitle.setFont(sub_font)
        self._subtitle.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
        )
        self._subtitle.setFixedHeight(16)
        self._subtitle.setStyleSheet(
            "color: #888888; background: transparent; border: none;"
        )
        # Always reserve the subtitle row so ring height never jumps.

        if tooltip:
            self.setToolTip(tooltip)
            self._ring.setToolTip(tooltip)

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(108)
        self.setFixedHeight(self._BODY_HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        # Clicks hit this widget so selection works on the ring and labels.
        self._ring.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._subtitle.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(4)
        layout.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self._ring, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self._title)
        layout.addWidget(self._subtitle)
        self._apply_selection_opacity()

    def set_percent(self, value: int | None) -> None:
        self._ring.set_percent(value)

    def content_rect(self) -> QRect:
        """Donut + labels in local coords (ignores horizontal stretch empty space)."""
        return QRect(self._ring.x(), 0, self._ring.width(), self.height())

    def set_selected(self, selected: bool) -> None:
        if selected == self._selected:
            return
        self._selected = selected
        self._apply_selection_opacity()

    def is_selected(self) -> bool:
        return self._selected

    def _apply_selection_opacity(self) -> None:
        # Soft-dim unselected rings so the sliding window reads as the focus.
        opacity = 1.0 if self._selected else 0.55
        for child in (self._ring, self._title, self._subtitle):
            effect = QGraphicsOpacityEffect(child)
            effect.setOpacity(opacity)
            child.setGraphicsEffect(effect)

    def set_subtitle(self, text: str) -> None:
        # Keep the row visible (possibly blank) so the fixed ring height holds.
        self._subtitle.setText((text or "").strip())

    def set_tooltip(self, text: str) -> None:
        self.setToolTip(text)
        self._ring.setToolTip(text)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        ):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


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
        self.setFixedSize(104, 104)

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
        pct_font.setPointSize(15)
        pct_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(pct_font)
        label = "—" if self._pct is None else f"{max(0, min(100, self._pct))}%"
        text_rect = QRectF(0, 0, self.width(), self.height())
        painter.drawText(text_rect, int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter), label)
        painter.end()


class UsageRingSelector(QWidget):
    """AUTO / API / GROK rings with a sliding selection window."""

    selected_metric_changed = pyqtSignal(str)

    # Vertical outward pad; horizontal edges sit mid-gap between options.
    _PAD_Y = 8.0
    _SPACING = 6
    _RADIUS = 14.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cursor_ring = UsageRing(
            "AUTO",
            "#8BA4C7",
            subtitle="month",
            tooltip=(
                "Included-in-Pro AUTO usage for the current billing month.\n"
                "Resets on your subscription renewal day."
            ),
        )
        self.other_ring = UsageRing(
            "API",
            "#B0B0B0",
            subtitle="month",
            warn_at_70=True,
            tooltip=(
                "Included-in-Pro API (other models) usage for the current\n"
                "billing month. Resets on your subscription renewal day."
            ),
        )
        self.grok_ring = UsageRing(
            "GROK",
            "#9BB8D4",
            subtitle="weekly",
            tooltip=(
                "Grok Bot weekly allowance from the Cursor spending page.\n"
                "Resets every week; pauses at the limit until then."
            ),
        )
        self._rings: dict[TrayMetric, UsageRing] = {
            "auto": self.cursor_ring,
            "api": self.other_ring,
            "grok": self.grok_ring,
        }
        self._ordered = [self._rings[key] for key in TRAY_METRICS]
        self._selected_metric: TrayMetric = DEFAULT_TRAY_METRIC
        self._thumb = QRectF()
        self._anim_x = QPropertyAnimation(self, b"thumbX", self)
        self._anim_x.setDuration(200)
        self._anim_x.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim_w = QPropertyAnimation(self, b"thumbWidth", self)
        self._anim_w.setDuration(200)
        self._anim_w.setEasingCurve(QEasingCurve.Type.OutCubic)

        # Side margins ≈ half the visual gutter so the outer window edge can land mid-gap.
        pad_y = int(self._PAD_Y)
        side = 8
        top_m = 8 + pad_y
        bottom_m = 4 + pad_y
        layout = QHBoxLayout(self)
        layout.setContentsMargins(side, top_m, side, bottom_m)
        layout.setSpacing(self._SPACING)
        layout.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        for key in TRAY_METRICS:
            ring = self._rings[key]
            ring.clicked.connect(lambda m=key: self.set_selected_metric(m))
            layout.addWidget(ring, stretch=1)

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(top_m + UsageRing._BODY_HEIGHT + bottom_m)

        self.cursor_ring.set_selected(True)
        self.other_ring.set_selected(False)
        self.grok_ring.set_selected(False)
        QTimer.singleShot(0, lambda: self._snap_thumb(animate=False))

    def selected_metric(self) -> TrayMetric:
        return self._selected_metric

    def set_selected_metric(
        self, metric: str, *, animate: bool = True, emit: bool = True
    ) -> None:
        if metric not in self._rings:
            return
        typed: TrayMetric = metric  # type: ignore[assignment]
        changed = typed != self._selected_metric
        self._selected_metric = typed
        self._sync_selection_styles()
        self._snap_thumb(animate=animate and changed)
        if emit and changed:
            self.selected_metric_changed.emit(typed)

    def _sync_selection_styles(self) -> None:
        for key, ring in self._rings.items():
            ring.set_selected(key == self._selected_metric)

    def _mapped_content_rect(self, ring: UsageRing) -> QRectF:
        """Donut column in selector coordinates (excludes stretch padding)."""
        local = ring.content_rect()
        top_left = ring.mapTo(self, local.topLeft())
        return QRectF(
            float(top_left.x()),
            float(top_left.y()),
            float(local.width()),
            float(local.height()),
        )

    def _target_thumb(self) -> QRectF:
        """Tallest-option height; horizontal edges bisect the gap to neighbors."""
        contents = [self._mapped_content_rect(r) for r in self._ordered]
        if not contents or any(c.width() <= 0 or c.height() <= 0 for c in contents):
            return QRectF()
        idx = next(
            i
            for i, r in enumerate(self._ordered)
            if r is self._rings[self._selected_metric]
        )
        sel = contents[idx]
        # Typical content-to-content gutter (falls back to layout spacing).
        gaps = [
            contents[i + 1].left() - contents[i].right()
            for i in range(len(contents) - 1)
        ]
        half_gap = (sum(gaps) / len(gaps) / 2.0) if gaps else self._SPACING / 2.0

        if idx > 0:
            left = (contents[idx - 1].right() + sel.left()) / 2.0
        else:
            left = sel.left() - half_gap
        if idx < len(contents) - 1:
            right = (sel.right() + contents[idx + 1].left()) / 2.0
        else:
            right = sel.right() + half_gap

        top = min(c.top() for c in contents) - self._PAD_Y
        bottom = max(c.bottom() for c in contents) + self._PAD_Y
        return QRectF(left, top, max(0.0, right - left), max(0.0, bottom - top))

    def _snap_thumb(self, *, animate: bool) -> None:
        target = self._target_thumb()
        self._anim_x.stop()
        self._anim_w.stop()
        if not animate or self._thumb.isEmpty():
            self._thumb = target
            self.update()
            return
        self._anim_x.setStartValue(self._thumb.x())
        self._anim_x.setEndValue(target.x())
        self._anim_w.setStartValue(self._thumb.width())
        self._anim_w.setEndValue(target.width())
        # Keep y/height in sync immediately — rings share one row height.
        self._thumb.setY(target.y())
        self._thumb.setHeight(target.height())
        self._anim_x.start()
        self._anim_w.start()

    def _get_thumb_x(self) -> float:
        return self._thumb.x()

    def _set_thumb_x(self, value: float) -> None:
        self._thumb.moveLeft(float(value))
        self.update()

    def _get_thumb_width(self) -> float:
        return self._thumb.width()

    def _set_thumb_width(self, value: float) -> None:
        self._thumb.setWidth(float(value))
        self.update()

    thumbX = pyqtProperty(float, _get_thumb_x, _set_thumb_x)
    thumbWidth = pyqtProperty(float, _get_thumb_width, _set_thumb_width)

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        # Layout may still be settling; snap without animation after geometry lands.
        QTimer.singleShot(0, lambda: self._snap_thumb(animate=False))

    def paintEvent(self, event) -> None:  # noqa: ANN001, ARG002
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self._thumb.isEmpty():
            painter.setPen(QPen(QColor("#3A465A"), 1))
            painter.setBrush(QColor("#222830"))
            painter.drawRoundedRect(
                self._thumb.adjusted(0.5, 0.5, -0.5, -0.5),
                self._RADIUS,
                self._RADIUS,
            )
        painter.end()


def _habits_chip(text: str, *, fg: str, bg: str, border: str) -> QLabel:
    """Small status/metric pill used in the habits preview."""
    chip = QLabel(text)
    font = QFont()
    font.setPointSize(8)
    font.setWeight(QFont.Weight.DemiBold)
    chip.setFont(font)
    chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
    chip.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    chip.setStyleSheet(
        f"""
        QLabel {{
            color: {fg};
            background: {bg};
            border: 1px solid {border};
            border-radius: 7px;
            padding: 3px 8px;
        }}
        """
    )
    return chip


class BubbleTip(QFrame):
    """Solid speech-bubble tip with an arrow pointing at the hovered chip."""

    _instance: BubbleTip | None = None
    _ARROW = 8
    _RADIUS = 10
    _PAD_X = 12
    _PAD_Y = 10
    _FILL = QColor("#1E2430")
    _BORDER = QColor("#3A465A")
    _TEXT = "#E6EAF0"

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet("background: transparent; border: none;")

        self._arrow_below = True  # tip above target → arrow on bottom edge
        self._arrow_x = 0.5  # 0..1 across the bubble width

        self._label = QLabel("")
        tip_font = QFont()
        tip_font.setPointSize(9)
        self._label.setFont(tip_font)
        self._label.setWordWrap(True)
        self._label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self._label.setStyleSheet(
            f"color: {self._TEXT}; background: transparent; border: none;"
        )
        self._label.setMaximumWidth(200)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(
            self._PAD_X,
            self._PAD_Y,
            self._PAD_X,
            self._PAD_Y + self._ARROW,
        )
        lay.setSpacing(0)
        lay.addWidget(self._label)

    def _set_arrow_margins(self, *, arrow_below: bool) -> None:
        lay = self.layout()
        if lay is None:
            return
        if arrow_below:
            lay.setContentsMargins(
                self._PAD_X,
                self._PAD_Y,
                self._PAD_X,
                self._PAD_Y + self._ARROW,
            )
        else:
            lay.setContentsMargins(
                self._PAD_X,
                self._PAD_Y + self._ARROW,
                self._PAD_X,
                self._PAD_Y,
            )

    def paintEvent(self, event) -> None:  # noqa: ANN001, ARG002
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        arrow = float(self._ARROW)
        # Body sits opposite the arrow so the tip can point at the chip.
        if self._arrow_below:
            body = QRectF(
                1.0, 1.0, self.width() - 2.0, self.height() - arrow - 2.0
            )
        else:
            body = QRectF(
                1.0, arrow + 1.0, self.width() - 2.0, self.height() - arrow - 2.0
            )

        path = QPainterPath()
        path.setFillRule(Qt.FillRule.WindingFill)
        path.addRoundedRect(body, float(self._RADIUS), float(self._RADIUS))

        tip_x = body.left() + max(
            self._RADIUS + 4.0,
            min(body.width() - self._RADIUS - 4.0, body.width() * self._arrow_x),
        )
        half = 7.0
        if self._arrow_below:
            path.moveTo(tip_x - half, body.bottom())
            path.lineTo(tip_x, body.bottom() + arrow - 1.0)
            path.lineTo(tip_x + half, body.bottom())
            path.closeSubpath()
        else:
            path.moveTo(tip_x - half, body.top())
            path.lineTo(tip_x, body.top() - arrow + 1.0)
            path.lineTo(tip_x + half, body.top())
            path.closeSubpath()

        painter.setPen(QPen(self._BORDER, 1.0))
        painter.setBrush(self._FILL)
        painter.drawPath(path)
        painter.end()

    def show_for(self, widget: QWidget, text: str) -> None:
        clean = text.strip()
        if not clean:
            self.hide()
            return
        self._label.setText(clean)
        self._label.adjustSize()
        self.adjustSize()

        origin = widget.mapToGlobal(QPoint(0, 0))
        tip_w, tip_h = self.width(), self.height()
        chip_cx = origin.x() + widget.width() / 2.0

        # Prefer above the chip; flip below if the screen clips the top.
        place_above = True
        x = int(chip_cx - tip_w / 2.0)
        y = origin.y() - tip_h + 2
        screen = QGuiApplication.screenAt(origin) or QGuiApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            x = max(geo.left() + 4, min(x, geo.right() - tip_w - 4))
            if y < geo.top() + 4:
                place_above = False
                y = origin.y() + widget.height() - 2
            y = max(geo.top() + 4, min(y, geo.bottom() - tip_h - 4))

        self._arrow_below = place_above
        # Aim the arrow at the chip center even if the bubble was shifted.
        self._arrow_x = (chip_cx - x) / tip_w if tip_w else 0.5
        self._arrow_x = max(0.12, min(0.88, self._arrow_x))
        self._set_arrow_margins(arrow_below=self._arrow_below)
        self.adjustSize()
        tip_w, tip_h = self.width(), self.height()
        if place_above:
            y = origin.y() - tip_h + 2
        else:
            y = origin.y() + widget.height() - 2
        if screen is not None:
            geo = screen.availableGeometry()
            y = max(geo.top() + 4, min(y, geo.bottom() - tip_h - 4))
        self.move(x, y)
        self.update()
        self.show()
        self.raise_()

    @classmethod
    def shared(cls) -> BubbleTip:
        if cls._instance is None:
            cls._instance = BubbleTip()
        return cls._instance

    @classmethod
    def hide_shared(cls) -> None:
        if cls._instance is not None and cls._instance.isVisible():
            cls._instance.hide()


class SeriesToggleChip(QLabel):
    """Colored metric pill — shows latest period value; click toggles chart series."""

    toggled = pyqtSignal(str)  # series name

    def __init__(
        self,
        series_name: str,
        *,
        fg: str,
        bg: str,
        border: str,
        tooltip: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("—", parent)
        self.series_name = series_name
        self._fg = fg
        self._bg = bg
        self._border = border
        self._series_on = True
        self._bubble_tip = tooltip.strip()
        self._tip_timer = QTimer(self)
        self._tip_timer.setSingleShot(True)
        self._tip_timer.setInterval(280)
        self._tip_timer.timeout.connect(self._show_bubble_tip)
        font = QFont()
        font.setPointSize(8)
        font.setWeight(QFont.Weight.DemiBold)
        self.setFont(font)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Custom bubble tip — avoid the flat system QToolTip strip.
        self.setToolTip("")
        self._apply_style()

    def set_series_on(self, on: bool) -> None:
        if on == self._series_on:
            return
        self._series_on = on
        self._apply_style()

    def is_series_on(self) -> bool:
        return self._series_on

    def _apply_style(self) -> None:
        if self._series_on:
            fg, bg, border = self._fg, self._bg, self._border
        else:
            fg, bg, border = "#5E6775", "#1A1E24", "#2A313C"
        self.setStyleSheet(
            f"""
            QLabel {{
                color: {fg};
                background: {bg};
                border: 1px solid {border};
                border-radius: 7px;
                padding: 3px 8px;
            }}
            QLabel:hover {{
                border-color: {self._border if self._series_on else "#3A465A"};
            }}
            """
        )

    def _show_bubble_tip(self) -> None:
        if not self._bubble_tip or not self.underMouse():
            return
        BubbleTip.shared().show_for(self, self._bubble_tip)

    def enterEvent(self, event) -> None:  # noqa: ANN001
        if self._bubble_tip:
            self._tip_timer.start()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: ANN001
        self._tip_timer.stop()
        BubbleTip.hide_shared()
        super().leaveEvent(event)

    def hideEvent(self, event) -> None:  # noqa: ANN001
        self._tip_timer.stop()
        BubbleTip.hide_shared()
        super().hideEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.MouseButton.LeftButton:
            self._tip_timer.stop()
            BubbleTip.hide_shared()
            self.toggled.emit(self.series_name)
        super().mouseReleaseEvent(event)


def _habits_metric_row(label: str) -> tuple[QWidget, QLabel]:
    """Label + value row for a single habit metric."""
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)

    key = QLabel(label)
    key_font = QFont()
    key_font.setPointSize(9)
    key.setFont(key_font)
    key.setStyleSheet("color: #7E8694; background: transparent; border: none;")

    value = QLabel("—")
    value_font = QFont()
    value_font.setPointSize(9)
    value_font.setWeight(QFont.Weight.Medium)
    value.setFont(value_font)
    value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    value.setStyleSheet("color: #E4E7EC; background: transparent; border: none;")
    value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

    layout.addWidget(key, stretch=0)
    layout.addStretch(1)
    layout.addWidget(value, stretch=0)
    return row, value


@dataclass
class ChartSeries:
    """One metric line for MultiSeriesHistoryChart."""

    name: str
    color: str
    values: list[float | None]
    unit: str = ""
    area: bool = False  # filled under the line; always painted behind other series
    point_labels: bool = False  # draw raw values above non-zero points


class ChartValueCard(QWidget):
    """Hover readout drawn in its own window so the chart cannot clip it."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._font = QFont()
        self._font.setPointSize(8)
        self._lines: list[tuple[str, QColor]] = []

    def set_lines(self, lines: list[tuple[str, str]]) -> None:
        self._lines = [(text, QColor(color)) for text, color in lines]
        metrics = QFontMetrics(self._font)
        text_w = max((metrics.horizontalAdvance(text) for text, _ in self._lines), default=40)
        width = text_w + 16
        height = metrics.height() * max(1, len(self._lines)) + 10
        self.setFixedSize(width, height)
        self.update()

    def place(self, anchor: QPoint) -> None:
        """Put the card beside a global point, kept on screen, not inside the chart."""
        screen = QGuiApplication.screenAt(anchor) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry() if screen is not None else QRect(0, 0, 1920, 1080)
        width, height = self.width(), self.height()
        x = anchor.x() + 10
        if x + width > geo.right() - 4:
            x = anchor.x() - width - 10
        x = max(geo.left() + 4, min(x, geo.right() - width - 4))
        y = anchor.y() + 4
        if y + height > geo.bottom() - 4:
            y = geo.bottom() - 4 - height
        y = max(geo.top() + 4, y)
        self.move(x, y)
        self.show()
        self.raise_()

    def paintEvent(self, event) -> None:  # noqa: ANN001
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(self._font)
        card = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        painter.setBrush(QColor(20, 24, 32, 230))
        painter.setPen(QPen(QColor("#3A465A"), 1))
        painter.drawRoundedRect(card, 6, 6)
        metrics = painter.fontMetrics()
        ty = 5.0
        for text, color in self._lines:
            painter.setPen(color)
            painter.drawText(
                QRectF(8, ty, self.width() - 12, metrics.height()),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                text,
            )
            ty += metrics.height()
        painter.end()


def _format_chart_value(value: float | None, unit: str = "") -> str:
    if value is None:
        return "—"
    if unit == "%":
        return f"{value:.0f}%"
    if abs(value) >= 1000:
        return f"{value:,.0f}{unit}"
    if abs(value - round(value)) < 1e-6:
        return f"{value:.0f}{unit}"
    return f"{value:g}{unit}"


class MultiSeriesHistoryChart(QWidget):
    """Single multi-line chart; series are min–max normalized (``%`` series use 0–100); hover shows raw values."""

    # Fixed plot body. Legend lives in the Habits chips above — not inside the chart.
    # Total height is constant so chip/series changes cannot stretch or shrink this widget.
    _PLOT_BODY = 160
    _AXIS_RESERVE = 0  # period axis label hidden; keep constant for height math
    _PLOT_TOP = 10
    # Pixel clearance inside the plot so peaks/floor dots aren't edge-clipped.
    _CEILING_CLEARANCE = 12.0
    # Keep 0% / min values clearly above the plot floor (dots + stroke + AA).
    _FLOOR_CLEARANCE = 28.0

    # Selected period index, or None when the selection is cleared.
    period_selected = pyqtSignal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._labels: list[str] = []
        self._weights: list[int] = []
        self._series: list[ChartSeries] = []
        self._hidden: set[str] = set()
        self._hover_index: int | None = None
        self._selected_index: int | None = None
        # Year-chart index where a prior plan ends and a new plan begins.
        self._plan_change_index: int | None = None
        self._footer_caption = ""
        self._footer_nav_visible = False
        self._plot = QRectF()
        self._value_card = ChartValueCard(self)
        self._nav_prev = PeriodNavButton("‹", self)
        self._nav_next = PeriodNavButton("›", self)
        self._nav_prev.setToolTip("Previous billing period")
        self._nav_next.setToolTip("Next billing period")
        self._nav_prev.hide()
        self._nav_next.hide()
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(self._fixed_height())
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _fixed_height(self) -> int:
        return int(self._PLOT_TOP + self._PLOT_BODY + self._AXIS_RESERVE)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        del width
        return self._fixed_height()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(360, self._fixed_height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(200, self._fixed_height())

    def set_data(
        self,
        labels: list[str],
        series: list[ChartSeries],
        *,
        weights: list[int] | None = None,
        caption: str = "",
        plan_change_index: int | None = None,
    ) -> None:
        selected_label = self.selected_label()
        self._labels = list(labels)
        self._series = list(series)
        self._weights = list(weights or [])
        self._footer_caption = caption.strip()
        if plan_change_index is not None and 0 <= plan_change_index < len(self._labels):
            self._plan_change_index = plan_change_index
        else:
            self._plan_change_index = None
        names = {s.name for s in self._series}
        self._hidden &= names
        self._hover_index = None
        self._selected_index = (
            self._labels.index(selected_label)
            if selected_label in self._labels
            else None
        )
        self._value_card.hide()
        self.setFixedHeight(self._fixed_height())
        self.updateGeometry()
        self.update()

    @property
    def nav_prev(self) -> PeriodNavButton:
        return self._nav_prev

    @property
    def nav_next(self) -> PeriodNavButton:
        return self._nav_next

    def set_footer_nav_visible(self, visible: bool) -> None:
        self._footer_nav_visible = bool(visible)
        self._nav_prev.setVisible(self._footer_nav_visible)
        self._nav_next.setVisible(self._footer_nav_visible)
        self._position_footer_nav()
        self.update()

    def set_footer_nav_enabled(self, *, prev: bool, next: bool) -> None:
        self._nav_prev.set_nav_enabled(prev)
        self._nav_next.set_nav_enabled(next)

    def _footer_caption_text_width(self) -> int:
        """Usable width for the footer caption between optional nav buttons."""
        inset = 0
        if self._footer_nav_visible:
            inset = self._nav_prev.width() + self._nav_next.width() + 16
        return max(0, int(self._plot.width()) - inset)

    def _position_footer_nav(self) -> None:
        if not self._footer_nav_visible or self._plot.height() <= 0:
            return
        floor_y = self._draw_rect().bottom()
        footer_top = floor_y + 2.0
        footer_h = max(0.0, self._plot.bottom() - footer_top)
        btn_h = self._nav_prev.height()
        y = int(round(footer_top + max(0.0, (footer_h - btn_h) / 2.0)))
        left = int(round(self._plot.left()))
        right = int(round(self._plot.right() - self._nav_next.width()))
        self._nav_prev.move(left, y)
        self._nav_next.move(right, y)
        self._nav_prev.raise_()
        self._nav_next.raise_()

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        self._position_footer_nav()

    def selected_index(self) -> int | None:
        return self._selected_index

    def clear_selection(self) -> None:
        self._selected_index = None
        self.update()

    def select_index(self, index: int | None) -> None:
        if index is None or not (0 <= index < len(self._labels)):
            self._selected_index = None
        else:
            self._selected_index = index
        self.update()

    def selected_label(self) -> str | None:
        idx = self._selected_index
        if idx is None or not (0 <= idx < len(self._labels)):
            return None
        return self._labels[idx]

    def _index_at(self, x: float) -> int:
        n = len(self._labels)
        rel = (x - self._plot.left()) / max(1.0, self._plot.width())
        return max(0, min(n - 1, int(round(rel * (n - 1)))))

    def is_series_visible(self, name: str) -> bool:
        return name not in self._hidden

    def toggle_series(self, name: str) -> bool:
        """Toggle series visibility. Returns True if the series is now visible."""
        if name in self._hidden:
            self._hidden.discard(name)
            visible = True
        else:
            self._hidden.add(name)
            visible = False
        self.update()
        self._sync_value_card()
        return visible

    def _visible_series(self) -> list[ChartSeries]:
        return [s for s in self._series if s.name not in self._hidden]

    def _draw_rect(self) -> QRectF:
        """Viewing area for series — fixed pixel insets so floor/ceiling stay visible."""
        top = self._CEILING_CLEARANCE
        # Point-value labels sit above dots; keep extra headroom so they aren't clipped.
        if any(s.point_labels for s in self._visible_series()):
            top = max(top, 18.0)
        bottom = self._FLOOR_CLEARANCE
        r = self._plot
        if r.height() <= top + bottom + 4:
            return r
        return QRectF(
            r.left(),
            r.top() + top,
            r.width(),
            r.height() - top - bottom,
        )

    def leaveEvent(self, event) -> None:  # noqa: ANN001
        self._hover_index = None
        self._value_card.hide()
        self.update()
        super().leaveEvent(event)

    def hideEvent(self, event) -> None:  # noqa: ANN001
        self._value_card.hide()
        super().hideEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001
        if len(self._labels) < 2 or self._plot.width() <= 0:
            return
        x = event.position().x() if hasattr(event, "position") else event.x()
        idx = self._index_at(x)
        if idx != self._hover_index:
            self._hover_index = idx
            self.update()
        self._sync_value_card()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        if (
            event.button() == Qt.MouseButton.LeftButton
            and len(self._labels) >= 2
            and self._plot.width() > 0
        ):
            x = event.position().x() if hasattr(event, "position") else event.x()
            idx = self._index_at(x)
            self._selected_index = None if idx == self._selected_index else idx
            self.update()
            self.period_selected.emit(self._selected_index)
        super().mousePressEvent(event)

    def _sync_value_card(self) -> None:
        idx = self._hover_index
        if (
            idx is None
            or not (0 <= idx < len(self._labels))
            or self._plot.width() <= 0
            or not self.isVisible()
        ):
            self._value_card.hide()
            return
        n = len(self._labels)
        x = (
            self._plot.left()
            if n == 1
            else self._plot.left() + (self._plot.width() * idx / (n - 1))
        )
        header = self._labels[idx]
        if idx < len(self._weights):
            header += f" · {self._weights[idx]}"
        if self._plan_change_index is not None and idx == self._plan_change_index:
            header += " · plan change"
        lines = [(header, "#D5DEEA")]
        value_lines: list[tuple[str, str]] = []
        for series in self._visible_series():
            raw = series.values[idx] if idx < len(series.values) else None
            if raw is None:
                continue
            value_lines.append(
                (f"{series.name}: {_format_chart_value(raw, series.unit)}", series.color)
            )
        # Only annotate the change slot when both eras actually contribute values.
        if (
            self._plan_change_index is not None
            and idx == self._plan_change_index
            and len(value_lines) >= 2
        ):
            lines.append(("prior plan ends · new plan starts", "#8B8496"))
        lines.extend(value_lines)
        self._value_card.set_lines(lines)
        anchor = self.mapToGlobal(QPoint(int(round(x)), int(round(self._plot.top()))))
        self._value_card.place(anchor)

    def _normalized_points(self, series: ChartSeries) -> list[QPointF | None]:
        values = series.values
        numeric = [v for v in values if v is not None]
        if not numeric:
            return [None] * len(values)
        if series.unit == "%":
            lo = 0.0
            hi = max(100.0, max(numeric))
        else:
            lo = min(numeric)
            hi = max(numeric)
            if hi <= lo:
                hi = lo + 1.0
            span = hi - lo
            # Peak headroom in value space; floor clearance is pixel-based via _draw_rect.
            headroom = 0.22 if series.point_labels else 0.14
            hi += span * headroom
        draw = self._draw_rect()
        n = len(values)
        out: list[QPointF | None] = []
        for i, value in enumerate(values):
            if value is None:
                out.append(None)
                continue
            x = (
                draw.left()
                if n == 1
                else draw.left() + (draw.width() * i / (n - 1))
            )
            # Min maps to draw.bottom() (= plot.bottom - _FLOOR_CLEARANCE).
            y = draw.bottom() - ((value - lo) / (hi - lo)) * draw.height()
            out.append(QPointF(x, y))
        return out

    def paintEvent(self, event) -> None:  # noqa: ANN001
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        painter.fillRect(self.rect(), QColor("#141820"))
        painter.setPen(QPen(QColor("#243041"), 1))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 8, 8)

        # Series identity is shown by the Habits chips above the chart.
        plot_top = float(self._PLOT_TOP)
        available = self.height() - plot_top - self._AXIS_RESERVE
        self._plot = QRectF(
            12,
            plot_top,
            self.width() - 24,
            # Never force a plot taller than the widget — that clipped the axis.
            max(0.0, available),
        )

        visible_series = self._visible_series()
        if len(self._labels) < 2 or not self._series:
            painter.setPen(QColor("#5E6775"))
            note = QFont()
            note.setPointSize(8)
            painter.setFont(note)
            msg = self._footer_caption or "Need 2+ data points"
            painter.drawText(
                self._plot,
                int(Qt.AlignmentFlag.AlignCenter),
                msg,
            )
            painter.end()
            self._position_footer_nav()
            return

        if not visible_series:
            painter.setPen(QColor("#5E6775"))
            note = QFont()
            note.setPointSize(8)
            painter.setFont(note)
            painter.drawText(
                self._plot,
                int(Qt.AlignmentFlag.AlignCenter),
                "All series hidden — click chips to show",
            )
            self._paint_footer_caption(painter, self._draw_rect().bottom())
            painter.end()
            self._position_footer_nav()
            return

        # Grid
        painter.setPen(QPen(QColor("#1E2633"), 1, Qt.PenStyle.DotLine))
        for frac in (0.25, 0.5, 0.75):
            y = self._plot.top() + self._plot.height() * frac
            painter.drawLine(
                QPointF(self._plot.left(), y), QPointF(self._plot.right(), y)
            )
        # Floor baseline — makes 0%/min values readable above the axis strip.
        floor_y = self._draw_rect().bottom()
        painter.setPen(QPen(QColor("#2A3545"), 1, Qt.PenStyle.SolidLine))
        painter.drawLine(
            QPointF(self._plot.left(), floor_y),
            QPointF(self._plot.right(), floor_y),
        )

        # Plan-change marker behind series so dots still read on top.
        self._paint_plan_change_marker(painter)

        # Area fills first (behind), then every line/dot on top.
        area_series = [s for s in visible_series if s.area]
        line_series = [s for s in visible_series if not s.area]
        for series in area_series:
            self._paint_area_series(painter, series)
        for series in [*area_series, *line_series]:
            self._paint_line_series(painter, series)

        n = len(self._labels)
        if self._selected_index is not None and 0 <= self._selected_index < n:
            x = self._plot.left() + (self._plot.width() * self._selected_index / (n - 1))
            painter.setPen(QPen(QColor("#9FB3CC"), 1.4, Qt.PenStyle.SolidLine))
            painter.drawLine(
                QPointF(x, self._plot.top()), QPointF(x, self._plot.bottom())
            )

        # Hover crosshair + value card
        if self._hover_index is not None and 0 <= self._hover_index < len(self._labels):
            idx = self._hover_index
            n = len(self._labels)
            x = (
                self._plot.left()
                if n == 1
                else self._plot.left() + (self._plot.width() * idx / (n - 1))
            )
            painter.setPen(QPen(QColor("#6B7C93"), 1, Qt.PenStyle.DashLine))
            painter.drawLine(
                QPointF(x, self._plot.top()), QPointF(x, self._plot.bottom())
            )

        self._paint_footer_caption(painter, floor_y)
        painter.end()
        self._position_footer_nav()

    def _paint_plan_change_marker(self, painter: QPainter) -> None:
        """Dotted rule spanning prior-end / new-start points, with red/green washes."""
        idx = self._plan_change_index
        n = len(self._labels)
        if idx is None or n < 2 or not (0 <= idx < n):
            return
        draw = self._draw_rect()
        x = self._plot.left() + (self._plot.width() * idx / (n - 1))
        # Soft horizontal washes across ~one period width on each side.
        period_w = self._plot.width() / (n - 1)
        wash_w = max(18.0, min(period_w * 0.85, self._plot.width() * 0.18))

        # Height = vertical span of the end-of-plan and start-of-plan dots.
        ys: list[float] = []
        for series in self._visible_series():
            points = self._normalized_points(series)
            if idx >= len(points):
                continue
            pt = points[idx]
            if pt is not None:
                ys.append(pt.y())
        if len(ys) >= 2:
            band_top = min(ys)
            band_bottom = max(ys)
        elif len(ys) == 1:
            # Single visible point — short stub around it.
            band_top = ys[0] - 8.0
            band_bottom = ys[0] + 8.0
        else:
            mid_y = (draw.top() + draw.bottom()) / 2.0
            band_top = mid_y - 12.0
            band_bottom = mid_y + 12.0
        # Keep a readable minimum span and stay inside the draw rect.
        if band_bottom - band_top < 12.0:
            mid = (band_top + band_bottom) / 2.0
            band_top = mid - 6.0
            band_bottom = mid + 6.0
        band_top = max(draw.top(), band_top)
        band_bottom = min(draw.bottom(), band_bottom)

        left = max(self._plot.left(), x - wash_w)
        right = min(self._plot.right(), x + wash_w)

        # Prior plan ends — red fade into the change from the left.
        if x > left + 1.0:
            red = QLinearGradient(left, 0.0, x, 0.0)
            red.setColorAt(0.0, QColor(220, 80, 80, 0))
            red.setColorAt(0.55, QColor(220, 80, 80, 28))
            red.setColorAt(1.0, QColor(220, 80, 80, 70))
            painter.fillRect(QRectF(left, band_top, x - left, band_bottom - band_top), red)

        # New plan starts — green fade out to the right.
        if right > x + 1.0:
            green = QLinearGradient(x, 0.0, right, 0.0)
            green.setColorAt(0.0, QColor(80, 180, 110, 70))
            green.setColorAt(0.45, QColor(80, 180, 110, 28))
            green.setColorAt(1.0, QColor(80, 180, 110, 0))
            painter.fillRect(
                QRectF(x, band_top, right - x, band_bottom - band_top), green
            )

        pen = QPen(QColor("#E8E8E8"), 1.15, Qt.PenStyle.DotLine)
        painter.setPen(pen)
        painter.drawLine(QPointF(x, band_top), QPointF(x, band_bottom))

        label = "plan change"
        font = QFont()
        font.setPointSize(7)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        tw = float(metrics.horizontalAdvance(label))
        th = float(metrics.height())
        text_x = x + 4.0
        if text_x + tw > self._plot.right() - 2.0:
            text_x = x - tw - 4.0
        text_x = max(self._plot.left() + 2.0, text_x)
        painter.setPen(QColor("#B8C0CC"))
        painter.drawText(
            QRectF(text_x, band_top - th - 1.0, tw + 2.0, th),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            label,
        )

    def _paint_footer_caption(self, painter: QPainter, floor_y: float) -> None:
        """Draw period summary in the thin strip below the graph floor line."""
        if not self._footer_caption:
            return
        footer_top = floor_y + 3.0
        footer_h = max(0.0, self._plot.bottom() - footer_top - 2.0)
        if footer_h < 10.0:
            return
        side = 0.0
        if self._footer_nav_visible:
            side = float(self._nav_prev.width() + 8)
        text_left = self._plot.left() + side
        text_w = max(0.0, self._plot.width() - 2 * side)
        cap_font = QFont()
        cap_font.setPointSize(8)
        painter.setFont(cap_font)
        metrics = painter.fontMetrics()
        elided = metrics.elidedText(
            self._footer_caption,
            Qt.TextElideMode.ElideRight,
            int(text_w),
        )
        painter.setPen(QColor("#6F7887"))
        painter.drawText(
            QRectF(text_left, footer_top, text_w, footer_h),
            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
            elided,
        )

    def _paint_area_series(self, painter: QPainter, series: ChartSeries) -> None:
        """Fill under the series curve down to the plot baseline (behind lines)."""
        points = self._normalized_points(series)
        usable = [p for p in points if p is not None]
        if len(usable) < 2:
            return
        fill = QColor(series.color)
        fill.setAlpha(55)
        stroke = QColor(series.color)
        stroke.setAlpha(160)
        # Align fill to the same floor as line series (not the axis strip).
        base_y = self._draw_rect().bottom()

        run: list[QPointF] = []
        def flush() -> None:
            nonlocal run
            if len(run) < 2:
                run = []
                return
            path = QPainterPath()
            path.moveTo(QPointF(run[0].x(), base_y))
            for pt in run:
                path.lineTo(pt)
            path.lineTo(QPointF(run[-1].x(), base_y))
            path.closeSubpath()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawPath(path)
            # Soft top edge so the area reads as a band, not a hard polygon.
            edge = QPainterPath()
            edge.moveTo(run[0])
            for pt in run[1:]:
                edge.lineTo(pt)
            pen = QPen(stroke, 1.2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(pen)
            painter.drawPath(edge)
            run = []

        for pt in points:
            if pt is None:
                flush()
                continue
            run.append(pt)
        flush()

    def _paint_point_labels(
        self, painter: QPainter, series: ChartSeries, points: list[QPointF | None]
    ) -> None:
        """Draw compact numeric labels above non-zero points (e.g. daily commits)."""
        if not series.point_labels:
            return
        font = QFont()
        font.setPointSize(7)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        for i, pt in enumerate(points):
            if pt is None or i >= len(series.values):
                continue
            raw = series.values[i]
            if raw is None or raw == 0:
                continue
            text = _format_chart_value(raw, series.unit)
            tw = float(metrics.horizontalAdvance(text))
            th = float(metrics.height())
            painter.setPen(QColor(series.color))
            painter.drawText(
                QRectF(pt.x() - tw / 2.0, pt.y() - th - 2.0, tw, th),
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                text,
            )

    def _paint_line_series(self, painter: QPainter, series: ChartSeries) -> None:
        """Stroke + dots for a series (drawn after area fills)."""
        if series.area:
            # Area series: dots only on top of the fill; stroke already drawn softly.
            points = self._normalized_points(series)
            painter.setBrush(QColor(series.color))
            painter.setPen(Qt.PenStyle.NoPen)
            for pt in points:
                if pt is not None:
                    painter.drawEllipse(pt, 2.0, 2.0)
            self._paint_point_labels(painter, series, points)
            return

        points = self._normalized_points(series)
        usable = [p for p in points if p is not None]
        if not usable:
            return
        pen = QPen(QColor(series.color), 1.9)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(pen)
        if len(usable) == 1:
            # Lone sample (e.g. only the current period has AUTO/API yet).
            painter.setBrush(QColor(series.color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(usable[0], 2.6, 2.6)
            self._paint_point_labels(painter, series, points)
            return
        path = QPainterPath()
        started = False
        for pt in points:
            if pt is None:
                started = False
                continue
            if not started:
                path.moveTo(pt)
                started = True
            else:
                path.lineTo(pt)
        painter.drawPath(path)
        painter.setBrush(QColor(series.color))
        painter.setPen(Qt.PenStyle.NoPen)
        for pt in points:
            if pt is not None:
                painter.drawEllipse(pt, 2.2, 2.2)
        self._paint_point_labels(painter, series, points)
        painter.setBrush(Qt.BrushStyle.NoBrush)


# Temporarily hide the whole Agent habits panel in the popup.
# Keep AgentHabitsPreview / HabitsHistoryCharts until we decide to delete them.
_SHOW_AGENT_HABITS_PANEL = False


class HabitsHistoryCharts(QWidget):
    """SDK habit trends for subscription billing periods."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        caption = QLabel("Trends by billing period · older → newer")
        cap_font = QFont()
        cap_font.setPointSize(8)
        caption.setFont(cap_font)
        caption.setStyleSheet("color: #6F7887; background: transparent; border: none;")
        self._caption = caption
        self._chart = MultiSeriesHistoryChart()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(caption)
        layout.addWidget(self._chart)

    def apply_history(self, history: tuple[SdkHabitsBatch, ...]) -> None:
        if not history:
            self.hide()
            return
        self.show()
        total_runs = sum(b.runs for b in history)
        self._caption.setText(
            f"Trends · {len(history)} billing periods · {total_runs} runs · older → newer"
        )
        labels = [b.label for b in history]
        self._chart.set_data(
            labels,
            [
                ChartSeries(
                    "Friction %",
                    "#8BA4C7",
                    [
                        float(b.friction_rate_pct)
                        if b.friction_rate_pct is not None
                        else None
                        for b in history
                    ],
                    unit="%",
                ),
                ChartSeries(
                    "Shell fail %",
                    "#D9897A",
                    [
                        float(b.shell_fail_pct)
                        if b.shell_fail_pct is not None
                        else None
                        for b in history
                    ],
                    unit="%",
                ),
                ChartSeries(
                    "Median tokens",
                    "#A8C3A4",
                    [
                        float(b.median_total_tokens)
                        if b.median_total_tokens is not None
                        else None
                        for b in history
                    ],
                ),
                ChartSeries(
                    "Median tools",
                    "#D0B56C",
                    [
                        float(b.median_tools_per_run)
                        if b.median_tools_per_run is not None
                        else None
                        for b in history
                    ],
                ),
            ],
            weights=[b.runs for b in history],
            caption="Hover a period for exact values (lines are normalized per metric)",
        )


class AgentHabitsPreview(QFrame):
    """Compact local SDK habit stats — review preview, separate from spend gauges."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("agentHabits")
        self.setStyleSheet(
            """
            QFrame#agentHabits {
                background: #16191E;
                border: 1px solid #2A313C;
                border-radius: 10px;
            }
            """
        )

        title = QLabel("Agent habits")
        title_font = QFont()
        title_font.setPointSize(11)
        title_font.setWeight(QFont.Weight.DemiBold)
        title.setFont(title_font)
        title.setStyleSheet("color: #E8ECF2; background: transparent; border: none;")

        self._badge = _habits_chip(
            "preview",
            fg="#9BB0C9",
            bg="#1E2530",
            border="#334155",
        )

        self._window = QLabel("")
        window_font = QFont()
        window_font.setPointSize(8)
        self._window.setFont(window_font)
        self._window.setStyleSheet(
            "color: #6F7887; background: transparent; border: none;"
        )
        self._window.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        header.addWidget(title, stretch=0)
        header.addWidget(self._badge, stretch=0)
        header.addStretch(1)
        header.addWidget(self._window, stretch=0)

        self._chip_ok = _habits_chip("— ok", fg="#A8C3A4", bg="#1A2420", border="#2F4638")
        self._chip_cancel = _habits_chip(
            "— cancel", fg="#D0B56C", bg="#242018", border="#4A4028"
        )
        self._chip_err = _habits_chip(
            "— err", fg="#D9897A", bg="#261C1A", border="#4A322E"
        )
        self._chip_friction = _habits_chip(
            "— friction", fg="#8BA4C7", bg="#1A2030", border="#2F3B52"
        )

        chips = QHBoxLayout()
        chips.setContentsMargins(0, 0, 0, 0)
        chips.setSpacing(6)
        chips.addWidget(self._chip_ok)
        chips.addWidget(self._chip_cancel)
        chips.addWidget(self._chip_err)
        chips.addWidget(self._chip_friction)
        chips.addStretch(1)

        self._tokens_row, self._tokens_value = _habits_metric_row("Median tokens")
        self._tools_row, self._tools_value = _habits_metric_row("Median tools / run")
        self._shell_row, self._shell_value = _habits_metric_row("Shell ≠ 0")
        self._incomplete_row, self._incomplete_value = _habits_metric_row(
            "Incomplete tools"
        )

        metrics = QVBoxLayout()
        metrics.setContentsMargins(0, 0, 0, 0)
        metrics.setSpacing(4)
        metrics.addWidget(self._tokens_row)
        metrics.addWidget(self._tools_row)
        metrics.addWidget(self._shell_row)
        metrics.addWidget(self._incomplete_row)

        self._history = HabitsHistoryCharts()
        self._history.hide()

        self._insight = QLabel("")
        insight_font = QFont()
        insight_font.setPointSize(9)
        self._insight.setFont(insight_font)
        self._insight.setWordWrap(True)
        self._insight.setStyleSheet(
            """
            QLabel {
                color: #B7C0CE;
                background: #1B2230;
                border: 1px solid #2C3648;
                border-left: 3px solid #8BA4C7;
                border-radius: 8px;
                padding: 7px 10px;
            }
            """
        )
        self._insight.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._insight.hide()

        self._fallback = QLabel("")
        fallback_font = QFont()
        fallback_font.setPointSize(9)
        self._fallback.setFont(fallback_font)
        self._fallback.setWordWrap(True)
        self._fallback.setStyleSheet(
            "color: #8F8F8F; background: transparent; border: none;"
        )
        self._fallback.hide()

        self._content = QWidget()
        content_layout = QVBoxLayout(self._content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(8)
        content_layout.addLayout(chips)
        content_layout.addLayout(metrics)
        content_layout.addWidget(self._history)
        content_layout.addWidget(self._insight)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 11, 12, 11)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addWidget(self._content)
        layout.addWidget(self._fallback)

    def apply_preview(self, preview: SdkHabitsPreview) -> None:
        if not preview.available or preview.runs_considered == 0:
            message = (
                preview.error_message
                or (preview.lines[0] if preview.lines else "Unavailable")
            )
            self._content.hide()
            self._window.setText("")
            self._fallback.setText(message)
            self._fallback.show()
            return

        self._fallback.hide()
        self._content.show()
        self._window.setText(preview.window_label)

        self._chip_ok.setText(f"{preview.finished} ok")
        self._chip_cancel.setText(f"{preview.cancelled} cancel")
        self._chip_err.setText(f"{preview.error} err")
        if preview.friction_rate_pct is None:
            self._chip_friction.setText("— friction")
        else:
            self._chip_friction.setText(f"{preview.friction_rate_pct}% friction")

        if preview.median_total_tokens is None:
            self._tokens_value.setText("—")
        else:
            self._tokens_value.setText(f"{preview.median_total_tokens:,}")

        if preview.median_tools_per_run is None:
            self._tools_value.setText("—")
        else:
            self._tools_value.setText(f"{preview.median_tools_per_run:g}")

        if preview.shell_total:
            rate = round(100 * preview.shell_nonzero / preview.shell_total)
            self._shell_value.setText(
                f"{preview.shell_nonzero}/{preview.shell_total} ({rate}%)"
            )
            shell_color = "#D9897A" if rate >= 15 else (
                "#D0B56C" if rate >= 8 else "#A8C3A4"
            )
            self._shell_value.setStyleSheet(
                f"color: {shell_color}; background: transparent; border: none;"
            )
        else:
            self._shell_value.setText("0")
            self._shell_value.setStyleSheet(
                "color: #A8C3A4; background: transparent; border: none;"
            )

        self._incomplete_value.setText(str(preview.incomplete_tools))
        incomplete_color = (
            "#D0B56C" if preview.incomplete_tools else "#E4E7EC"
        )
        self._incomplete_value.setStyleSheet(
            f"color: {incomplete_color}; background: transparent; border: none;"
        )

        self._history.apply_history(preview.history)

        if preview.insight:
            self._insight.setText(preview.insight)
            self._insight.show()
        else:
            self._insight.hide()


def _format_token_chip(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B tokens"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M tokens"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k tokens"
    return f"{n} tokens"


def _parse_bucket_renewal_time(raw: str) -> time_of_day | None:
    if not raw:
        return None
    try:
        parts = [int(x) for x in raw.split(":")]
        while len(parts) < 3:
            parts.append(0)
        return time_of_day(parts[0], parts[1], parts[2])
    except ValueError:
        return None


def _merge_composer_usage_series(
    usage: UsageCsvPreview,
) -> tuple[
    list[str],
    list[str],
    list[float | None],
    list[float | None],
    list[float | None],
    list[float | None],
    list[float | None],
    list[float | None],
    list[float | None],
    list[float | None],
    list[int],
    list[time_of_day | None],
    list[str],
]:
    """Align billing-period labels with dual-era tokens + AUTO/API/Grok %.

    When a legacy period ends on the same day the current era begins (plan
    change), both are collapsed onto one x-slot so the new-plan points sit in
    the same horizontal space as free-floating, differently colored dots.
    """
    empty = (
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
    )
    if not usage.available:
        return empty

    rows: list[dict[str, object]] = []
    for u in sorted(
        usage.periods,
        key=lambda p: (p.period_start, 0 if p.era == "legacy" else 1),
    ):
        tok = int(getattr(u, "total_tokens", 0) or 0)
        value = float(tok) if tok or getattr(u, "event_count", 0) else None
        cp = getattr(u, "cursor_models_pct", None)
        op = getattr(u, "other_models_pct", None)
        gp = getattr(u, "grok_bot_pct", None)
        rows.append(
            {
                "label": str(getattr(u, "label", None) or u.period_start),
                "period_start": u.period_start,
                "period_end": str(getattr(u, "period_end", "") or ""),
                "era": getattr(u, "era", "current") or "current",
                "renewal_day": int(getattr(u, "renewal_day", 0) or 0),
                "renewal_time": _parse_bucket_renewal_time(
                    str(getattr(u, "renewal_time", "") or "")
                ),
                "tokens": value,
                "auto": float(cp) if cp is not None else None,
                "api": float(op) if op is not None else None,
                "grok": float(gp) if gp is not None else None,
            }
        )

    # Collapse plan-change pair onto one x index (legacy end == current start).
    collapsed: list[dict[str, object]] = []
    i = 0
    while i < len(rows):
        cur = rows[i]
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        if (
            nxt is not None
            and cur["era"] == "legacy"
            and nxt["era"] == "current"
            and cur["period_end"]
            and cur["period_end"] == nxt["period_start"]
        ):
            change = str(nxt["period_start"])
            try:
                d = date.fromisoformat(change)
                change_label = f"{d.strftime('%b')} {d.day}, {d.year}"
            except ValueError:
                change_label = change
            collapsed.append(
                {
                    "label": change_label,
                    # Month drill-down uses the active (new) plan period.
                    "period_start": nxt["period_start"],
                    "period_end": nxt["period_end"],
                    "renewal_day": nxt["renewal_day"],
                    "renewal_time": nxt["renewal_time"],
                    "tokens_legacy": cur["tokens"],
                    "tokens_current": nxt["tokens"],
                    "auto_legacy": cur["auto"],
                    "auto_current": nxt["auto"],
                    "api_legacy": cur["api"],
                    "api_current": nxt["api"],
                    "grok_legacy": cur["grok"],
                    "grok_current": nxt["grok"],
                }
            )
            i += 2
            continue
        era = cur["era"]
        collapsed.append(
            {
                "label": cur["label"],
                "period_start": cur["period_start"],
                "period_end": cur["period_end"],
                "renewal_day": cur["renewal_day"],
                "renewal_time": cur["renewal_time"],
                "tokens_legacy": cur["tokens"] if era == "legacy" else None,
                "tokens_current": cur["tokens"] if era != "legacy" else None,
                "auto_legacy": cur["auto"] if era == "legacy" else None,
                "auto_current": cur["auto"] if era != "legacy" else None,
                "api_legacy": cur["api"] if era == "legacy" else None,
                "api_current": cur["api"] if era != "legacy" else None,
                "grok_legacy": cur["grok"] if era == "legacy" else None,
                "grok_current": cur["grok"] if era != "legacy" else None,
            }
        )
        i += 1

    labels: list[str] = []
    period_starts: list[str] = []
    tokens_legacy: list[float | None] = []
    tokens_current: list[float | None] = []
    auto_legacy: list[float | None] = []
    auto_current: list[float | None] = []
    api_legacy: list[float | None] = []
    api_current: list[float | None] = []
    grok_legacy: list[float | None] = []
    grok_current: list[float | None] = []
    renewal_days: list[int] = []
    renewal_times: list[time_of_day | None] = []
    period_ends: list[str] = []

    for row in collapsed:
        labels.append(str(row["label"]))
        period_starts.append(str(row["period_start"]))
        period_ends.append(str(row["period_end"] or ""))
        renewal_days.append(int(row["renewal_day"] or 0))
        renewal_times.append(row["renewal_time"])  # type: ignore[arg-type]
        tokens_legacy.append(row["tokens_legacy"])  # type: ignore[arg-type]
        tokens_current.append(row["tokens_current"])  # type: ignore[arg-type]
        auto_legacy.append(row["auto_legacy"])  # type: ignore[arg-type]
        auto_current.append(row["auto_current"])  # type: ignore[arg-type]
        api_legacy.append(row["api_legacy"])  # type: ignore[arg-type]
        api_current.append(row["api_current"])  # type: ignore[arg-type]
        grok_legacy.append(row["grok_legacy"])  # type: ignore[arg-type]
        grok_current.append(row["grok_current"])  # type: ignore[arg-type]

    series_cols = (
        tokens_legacy,
        tokens_current,
        auto_legacy,
        auto_current,
        api_legacy,
        api_current,
        grok_legacy,
        grok_current,
    )

    def _row_has_data(idx: int) -> bool:
        return any(col[idx] is not None for col in series_cols)

    keep = [idx for idx in range(len(labels)) if _row_has_data(idx)]
    if keep:
        lo, hi = keep[0], keep[-1] + 1
        labels = labels[lo:hi]
        period_starts = period_starts[lo:hi]
        tokens_legacy = tokens_legacy[lo:hi]
        tokens_current = tokens_current[lo:hi]
        auto_legacy = auto_legacy[lo:hi]
        auto_current = auto_current[lo:hi]
        api_legacy = api_legacy[lo:hi]
        api_current = api_current[lo:hi]
        grok_legacy = grok_legacy[lo:hi]
        grok_current = grok_current[lo:hi]
        renewal_days = renewal_days[lo:hi]
        renewal_times = renewal_times[lo:hi]
        period_ends = period_ends[lo:hi]

    return (
        labels,
        period_starts,
        tokens_legacy,
        tokens_current,
        auto_legacy,
        auto_current,
        api_legacy,
        api_current,
        grok_legacy,
        grok_current,
        renewal_days,
        renewal_times,
        period_ends,
    )


class SegmentedToggle(QWidget):
    """Pill track with a sliding selection thumb (Year / Month)."""

    changed = pyqtSignal(str)

    _PAD = 2
    _H = 26

    def __init__(
        self,
        options: list[tuple[str, str]],
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if len(options) < 2:
            raise ValueError("SegmentedToggle needs at least 2 options")
        self._options = list(options)  # (mode_id, label)
        self._index = 0
        self._thumb_x = float(self._PAD)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(self._H)

        font = QFont()
        font.setPointSize(8)
        font.setWeight(QFont.Weight.DemiBold)
        self._font = font
        metrics = QFontMetrics(font)
        seg_w = max(metrics.horizontalAdvance(label) for _, label in options) + 20
        self._seg_w = max(48, seg_w)
        self.setFixedWidth(self._PAD * 2 + self._seg_w * len(options))

        self._anim = QPropertyAnimation(self, b"thumbX", self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def mode(self) -> str:
        return self._options[self._index][0]

    def set_mode(self, mode: str, *, animate: bool = True) -> None:
        idx = next((i for i, (mid, _) in enumerate(self._options) if mid == mode), None)
        if idx is None or idx == self._index:
            return
        self._index = idx
        self._snap_thumb(animate=animate)

    def _thumb_target_x(self) -> float:
        return float(self._PAD + self._index * self._seg_w)

    def _snap_thumb(self, *, animate: bool) -> None:
        target = self._thumb_target_x()
        self._anim.stop()
        if not animate:
            self._thumb_x = target
            self.update()
            return
        self._anim.setStartValue(self._thumb_x)
        self._anim.setEndValue(target)
        self._anim.start()

    def _get_thumb_x(self) -> float:
        return self._thumb_x

    def _set_thumb_x(self, value: float) -> None:
        self._thumb_x = float(value)
        self.update()

    thumbX = pyqtProperty(float, _get_thumb_x, _set_thumb_x)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.width(), self._H)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        x = event.position().x()
        idx = int((x - self._PAD) // self._seg_w)
        idx = max(0, min(len(self._options) - 1, idx))
        mode = self._options[idx][0]
        if idx != self._index:
            self._index = idx
            self._snap_thumb(animate=True)
            self.changed.emit(mode)
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:  # noqa: ANN001, ARG002
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        track = QRectF(0, 0, self.width(), self.height())
        radius = track.height() / 2
        painter.setPen(QPen(QColor("#3A3348"), 1))
        painter.setBrush(QColor("#1A1620"))
        painter.drawRoundedRect(track.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

        thumb_h = self.height() - self._PAD * 2
        thumb = QRectF(self._thumb_x, self._PAD, self._seg_w, thumb_h)
        thumb_r = thumb_h / 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#0C0C0E"))
        painter.drawRoundedRect(thumb, thumb_r, thumb_r)
        # Subtle raised edge so the thumb reads as a sliding plate.
        painter.setPen(QPen(QColor("#2E2A38"), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(thumb.adjusted(0.5, 0.5, -0.5, -0.5), thumb_r, thumb_r)

        painter.setFont(self._font)
        for i, (_, label) in enumerate(self._options):
            seg = QRectF(
                self._PAD + i * self._seg_w,
                0,
                self._seg_w,
                self.height(),
            )
            active = i == self._index
            painter.setPen(QColor("#F2F2F4" if active else "#8B8496"))
            painter.drawText(seg, int(Qt.AlignmentFlag.AlignCenter), label)

        painter.end()


class PeriodNavButton(QLabel):
    """Compact ‹ / › control for stepping billing periods in month view."""

    clicked = pyqtSignal()

    def __init__(self, glyph: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._glyph = glyph
        self._enabled = True
        self.setFixedSize(20, 18)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._apply_style()

    def set_nav_enabled(self, enabled: bool) -> None:
        if enabled == self._enabled:
            return
        self._enabled = enabled
        self.setCursor(
            Qt.CursorShape.PointingHandCursor
            if enabled
            else Qt.CursorShape.ArrowCursor
        )
        self._apply_style()
        self.update()

    def _apply_style(self) -> None:
        if self._enabled:
            bg, border = "#1E2430", "#3A465A"
        else:
            bg, border = "#171A20", "#2A313C"
        self.setStyleSheet(
            f"""
            QLabel {{
                background: {bg};
                border: 1px solid {border};
                border-radius: 5px;
            }}
            QLabel:hover {{
                border-color: {"#5B7A9E" if self._enabled else border};
            }}
            """
        )

    def paintEvent(self, event) -> None:  # noqa: ANN001
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont()
        font.setPointSize(9)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        if self._enabled:
            painter.setPen(QColor("#D8DEE8"))
        else:
            painter.setPen(QColor("#4A5564"))
        # Center the glyph’s ink box (‹ › sit off-center with AlignCenter).
        ink = QFontMetrics(font).tightBoundingRect(self._glyph)
        x = (self.width() - ink.width()) * 0.5 - ink.x()
        y = (self.height() - ink.height()) * 0.5 - ink.y()
        painter.drawText(QPointF(x, y), self._glyph)
        painter.end()

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._enabled
            and self.rect().contains(event.position().toPoint())
        ):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class ComposerHistoryPreview(QFrame):
    """Usage trends — Year (billing periods) or Month (days in period)."""

    # period_start ISO, renewal_day, renewal_time (time | None)
    focused_period_changed = pyqtSignal(str, int, object)
    # ISO calendar day when a Month-view point is selected; None = whole period
    focused_day_changed = pyqtSignal(object)

    # Single horizontal chip row. Thin overlay scrollbar appears only while scrolling.
    _CHIP_ROW_H = 24
    _CHIPS_AREA_H = _CHIP_ROW_H
    _CHIPS_BAR_H = 3
    _CHIPS_BAR_HIDE_MS = 900
    _SECTION_GAP = 8

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("composerHistory")
        # No outer chrome — chart + chips own the visual edges.
        self.setStyleSheet(
            """
            QFrame#composerHistory {
                background: transparent;
                border: none;
            }
            """
        )

        self._title = QLabel("Habits")
        title_font = QFont()
        title_font.setPointSize(11)
        title_font.setWeight(QFont.Weight.DemiBold)
        self._title.setFont(title_font)
        self._title.setStyleSheet(
            "color: #E8ECF2; background: transparent; border: none;"
        )
        self._title.setToolTip("Drag to reorder panels")

        self._mode = "year"
        self._view_toggle = SegmentedToggle(
            [("year", "Year"), ("month", "Month")]
        )
        self._view_toggle.changed.connect(self._on_view_mode)

        header = QHBoxLayout()
        header.setContentsMargins(5, 0, 0, 0)
        header.setSpacing(8)
        header.addWidget(self._title, stretch=0)
        header.addStretch(1)
        header.addWidget(self._view_toggle, stretch=0)

        self._chip_tokens = SeriesToggleChip(
            "Tokens",
            fg="#E0C090",
            bg="#242016",
            border="#4A3C28",
            tooltip=(
                "Total tokens from Cursor\n"
                "usage-events CSV this period.\n"
                "\n"
                "Prior subscription renewal day\n"
                "when a plan change split the chart."
            ),
        )
        self._chip_tokens_new = SeriesToggleChip(
            "Tokens · new",
            fg="#7EB8A0",
            bg="#1A2420",
            border="#2F4638",
            tooltip=(
                "Tokens under the current\n"
                "subscription renewal day\n"
                "(after the plan change)."
            ),
        )
        self._chip_auto = SeriesToggleChip(
            "AUTO %",
            fg="#6FA8DC",
            bg="#1A2030",
            border="#2F3B52",
            tooltip=(
                "Included-in-Pro AUTO usage %\n"
                "from the Cursor spending page\n"
                "for this billing period."
            ),
        )
        self._chip_auto_new = SeriesToggleChip(
            "AUTO · new",
            fg="#8EC0E8",
            bg="#1A2030",
            border="#3A4F6A",
            tooltip=(
                "AUTO % under the current\n"
                "subscription renewal day\n"
                "(after the plan change)."
            ),
        )
        self._chip_api = SeriesToggleChip(
            "API %",
            fg="#B0B0B0",
            bg="#1E1E1E",
            border="#3A3A3A",
            tooltip=(
                "Included-in-Pro API usage %\n"
                "from the Cursor spending page\n"
                "for this billing period."
            ),
        )
        self._chip_api_new = SeriesToggleChip(
            "API · new",
            fg="#D0D0D0",
            bg="#1E1E1E",
            border="#4A4A4A",
            tooltip=(
                "API % under the current\n"
                "subscription renewal day\n"
                "(after the plan change)."
            ),
        )
        self._chip_grok = SeriesToggleChip(
            "Grok %",
            fg="#C9A8D4",
            bg="#241A28",
            border="#46304A",
            tooltip=(
                "Average of Grok Bot weekly peak %\n"
                "in this billing period.\n"
                "\n"
                "Weeks that cross a renewal boundary\n"
                "are prorated by overlapping days."
            ),
        )
        self._chip_grok_new = SeriesToggleChip(
            "Grok · new",
            fg="#D4B8E0",
            bg="#241A28",
            border="#5A4060",
            tooltip=(
                "Grok % under the current\n"
                "subscription renewal day\n"
                "(after the plan change)."
            ),
        )
        self._chip_daily = SeriesToggleChip(
            "Daily",
            fg="#E0C090",
            bg="#242016",
            border="#4A3C28",
            tooltip=(
                "Tokens used on this calendar day\n"
                "inside the selected billing period."
            ),
        )
        self._chip_cumulative = SeriesToggleChip(
            "Cumulative",
            fg="#6FA8DC",
            bg="#1A2030",
            border="#2F3B52",
            tooltip=(
                "Running token total from the\n"
                "period reset through this day."
            ),
        )
        self._chip_commits = SeriesToggleChip(
            "Commits",
            fg="#A8C3A4",
            bg="#1A2420",
            border="#2F4638",
            tooltip=(
                "Authored GitHub commits on this\n"
                "calendar day (tracked branches)."
            ),
        )
        self._chip_tokens_new.hide()
        self._chip_auto.hide()
        self._chip_auto_new.hide()
        self._chip_api.hide()
        self._chip_api_new.hide()
        self._chip_grok.hide()
        self._chip_grok_new.hide()
        self._chip_daily.hide()
        self._chip_cumulative.hide()
        self._chip_commits.hide()

        self._year_chips: list[SeriesToggleChip] = [
            self._chip_tokens,
            self._chip_tokens_new,
            self._chip_auto,
            self._chip_auto_new,
            self._chip_api,
            self._chip_api_new,
            self._chip_grok,
            self._chip_grok_new,
        ]
        self._month_chips: list[SeriesToggleChip] = [
            self._chip_daily,
            self._chip_cumulative,
            self._chip_commits,
        ]
        self._series_chips: list[SeriesToggleChip] = (
            self._year_chips + self._month_chips
        )
        for chip in self._series_chips:
            chip.toggled.connect(self._on_series_chip_toggled)

        chips_host = QWidget()
        chips_host.setObjectName("habitsChipsHost")
        chips_host.setStyleSheet(
            "QWidget#habitsChipsHost { background: transparent; }"
        )
        chips_row = QHBoxLayout(chips_host)
        chips_row.setContentsMargins(0, 0, 0, 0)
        chips_row.setSpacing(4)
        chips_row.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        for chip in self._series_chips:
            chips_row.addWidget(chip, stretch=0)
        chips_row.addStretch(1)
        self._chips_host = chips_host
        self._chips_row = chips_row

        self._chips_scroll = QScrollArea()
        self._chips_scroll.setObjectName("habitsChipsScroll")
        self._chips_scroll.setWidgetResizable(False)
        self._chips_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._chips_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._chips_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._chips_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._chips_scroll.setFixedHeight(self._CHIPS_AREA_H)
        self._chips_scroll.setWidget(chips_host)
        self._chips_scroll.setStyleSheet(
            """
            QScrollArea#habitsChipsScroll {
                background: transparent;
                border: none;
            }
            QScrollArea#habitsChipsScroll > QWidget > QWidget {
                background: transparent;
            }
            """
        )
        # Thin overlay bar — shown briefly on scroll, never reserves layout space.
        self._chips_bar = QScrollBar(
            Qt.Orientation.Horizontal, self._chips_scroll
        )
        self._chips_bar.setObjectName("habitsChipsBar")
        self._chips_bar.setFixedHeight(self._CHIPS_BAR_H)
        self._chips_bar.setCursor(Qt.CursorShape.ArrowCursor)
        self._chips_bar.setStyleSheet(
            f"""
            QScrollBar#habitsChipsBar {{
                background: transparent;
                height: {self._CHIPS_BAR_H}px;
                margin: 0px;
                border: none;
            }}
            QScrollBar#habitsChipsBar::handle:horizontal {{
                background: #5A677A;
                border-radius: 1px;
                min-width: 20px;
            }}
            QScrollBar#habitsChipsBar::handle:horizontal:hover {{
                background: #7A8AA0;
            }}
            QScrollBar#habitsChipsBar::add-line:horizontal,
            QScrollBar#habitsChipsBar::sub-line:horizontal {{
                width: 0px;
                height: 0px;
            }}
            QScrollBar#habitsChipsBar::add-page:horizontal,
            QScrollBar#habitsChipsBar::sub-page:horizontal {{
                background: transparent;
            }}
            """
        )
        self._chips_bar.hide()
        self._chips_bar_syncing = False
        self._chips_bar_hide = QTimer(self)
        self._chips_bar_hide.setSingleShot(True)
        self._chips_bar_hide.setInterval(self._CHIPS_BAR_HIDE_MS)
        self._chips_bar_hide.timeout.connect(self._chips_bar.hide)
        hbar = self._chips_scroll.horizontalScrollBar()
        hbar.valueChanged.connect(self._on_chips_hbar_value)
        hbar.rangeChanged.connect(self._on_chips_hbar_range)
        self._chips_bar.valueChanged.connect(self._on_chips_bar_value)
        self._chips_scroll.installEventFilter(self)
        self._chips_scroll.viewport().installEventFilter(self)
        self._sync_chips_scroll_width()

        self._chart = MultiSeriesHistoryChart()
        self._chart.period_selected.connect(self._on_point_selected)
        self._chart.nav_prev.clicked.connect(lambda: self._step_month_period(-1))
        self._chart.nav_next.clicked.connect(lambda: self._step_month_period(1))

        self._renewal_day = 19
        self._renewal_time: time_of_day | None = None
        self._year_labels: list[str] = []
        self._period_starts: list[str] = []
        self._period_renewal_days: list[int] = []
        self._period_renewal_times: list[time_of_day | None] = []
        self._period_ends: list[str] = []
        self._tokens_legacy: list[float | None] = []
        self._tokens_current: list[float | None] = []
        self._auto_legacy: list[float | None] = []
        self._auto_current: list[float | None] = []
        self._api_legacy: list[float | None] = []
        self._api_current: list[float | None] = []
        self._grok_legacy: list[float | None] = []
        self._grok_current: list[float | None] = []
        self._month_period_start: str = ""
        self._daily: list[float | None] = []
        self._cumulative: list[float | None] = []
        self._commits: list[float | None] = []
        # period_start → (day_labels, daily_commits) for Month-view overlay
        self._github_commits_by_period: dict[str, tuple[list[str], list[float]]] = {}
        self._selected_year_index: int | None = None
        self._last_focused_period_key: tuple[str, int, str] | None = None
        self._last_focused_day: str | None = None
        self._month_day_count = 0
        self._emit_day_focus = True

        self._fallback = QLabel("")
        fallback_font = QFont()
        fallback_font.setPointSize(9)
        self._fallback.setFont(fallback_font)
        self._fallback.setWordWrap(True)
        self._fallback.setStyleSheet(
            "color: #8F8F8F; background: transparent; border: none;"
        )
        self._fallback.hide()

        self._content = QWidget()
        content_layout = QVBoxLayout(self._content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(self._SECTION_GAP)
        content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        content_layout.addWidget(self._chips_scroll)
        content_layout.addWidget(self._chart)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(self._SECTION_GAP)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)
        layout.addWidget(self._content)
        layout.addWidget(self._fallback)

        header_h = max(self._title.sizeHint().height(), SegmentedToggle._H)
        panel_h = (
            header_h
            + self._SECTION_GAP
            + self._CHIPS_AREA_H
            + self._SECTION_GAP
            + self._chart.height()
        )
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(panel_h)

    def drag_handle(self) -> QWidget:
        """Title label — sole drag affordance for reordering this panel."""
        return self._title

    def _sync_chips_scroll_width(self) -> None:
        """Size the chip row to its content so the scroll area can overflow."""
        spacing = self._chips_row.spacing()
        widths: list[int] = []
        for chip in self._series_chips:
            if chip.isHidden():
                continue
            chip.ensurePolished()
            w = max(chip.sizeHint().width(), chip.minimumSizeHint().width())
            if w > 0:
                widths.append(w)
        if not widths:
            content_w = self._chips_scroll.viewport().width()
        else:
            content_w = sum(widths) + spacing * max(0, len(widths) - 1)
        content_w = max(content_w, self._chips_scroll.viewport().width())
        self._chips_host.setFixedHeight(self._CHIP_ROW_H)
        self._chips_host.setFixedWidth(content_w)
        self._chips_host.updateGeometry()
        self._sync_chips_bar_from_hbar()
        if self._chips_bar.isVisible():
            self._place_chips_bar()

    def _chips_can_scroll(self) -> bool:
        hbar = self._chips_scroll.horizontalScrollBar()
        return hbar.maximum() > hbar.minimum()

    def _sync_chips_bar_from_hbar(self) -> None:
        hbar = self._chips_scroll.horizontalScrollBar()
        self._chips_bar_syncing = True
        self._chips_bar.setRange(hbar.minimum(), hbar.maximum())
        self._chips_bar.setPageStep(hbar.pageStep())
        self._chips_bar.setSingleStep(hbar.singleStep())
        self._chips_bar.setValue(hbar.value())
        self._chips_bar_syncing = False
        if not self._chips_can_scroll():
            self._chips_bar_hide.stop()
            self._chips_bar.hide()

    def _place_chips_bar(self) -> None:
        margin = 4
        area_w = self._chips_scroll.width()
        area_h = self._chips_scroll.height()
        self._chips_bar.setGeometry(
            margin,
            max(0, area_h - self._CHIPS_BAR_H - 1),
            max(0, area_w - margin * 2),
            self._CHIPS_BAR_H,
        )
        self._chips_bar.raise_()

    def _flash_chips_bar(self) -> None:
        if not self._chips_can_scroll():
            self._chips_bar.hide()
            return
        self._sync_chips_bar_from_hbar()
        self._place_chips_bar()
        self._chips_bar.show()
        self._chips_bar.raise_()
        self._chips_bar_hide.start()

    def _on_chips_hbar_value(self, value: int) -> None:
        if self._chips_bar_syncing:
            return
        self._chips_bar_syncing = True
        self._chips_bar.setValue(value)
        self._chips_bar_syncing = False
        self._flash_chips_bar()

    def _on_chips_hbar_range(self, _minimum: int, _maximum: int) -> None:
        self._sync_chips_bar_from_hbar()

    def _on_chips_bar_value(self, value: int) -> None:
        if self._chips_bar_syncing:
            return
        self._chips_bar_syncing = True
        self._chips_scroll.horizontalScrollBar().setValue(value)
        self._chips_bar_syncing = False
        self._flash_chips_bar()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: ANN001
        if obj is self._chips_scroll and event.type() == QEvent.Type.Resize:
            if self._chips_bar.isVisible():
                self._place_chips_bar()
        if obj is self._chips_scroll.viewport():
            et = event.type()
            if et == QEvent.Type.Resize:
                self._sync_chips_scroll_width()
            elif et == QEvent.Type.Wheel:
                # Map vertical wheel to horizontal chip scrolling.
                delta = event.angleDelta().y() or event.angleDelta().x()  # type: ignore[attr-defined]
                if delta:
                    bar = self._chips_scroll.horizontalScrollBar()
                    bar.setValue(bar.value() - int(delta))
                    return True
        return super().eventFilter(obj, event)

    def apply_github_commits(self, preview: GithubHabitsPreview | None) -> None:
        """Store authored daily commits for a billing period (Month view overlay)."""
        if preview is None or not preview.available or not preview.period_start:
            return
        self._github_commits_by_period[preview.period_start] = (
            list(preview.day_labels),
            list(preview.daily_commits),
        )
        if (
            self._mode == "month"
            and self._content.isVisible()
            and self._month_period_start == preview.period_start
        ):
            self._render_month_view()

    def focused_period_start(self) -> str:
        """ISO start of the billing period the chart is focused on."""
        meta = self.focused_period_meta()
        return meta[0] if meta is not None else ""

    def focused_period_meta(
        self,
    ) -> tuple[str, int, time_of_day | None] | None:
        """``(period_start, renewal_day, renewal_time)`` for the focused slot."""
        if not self._period_starts:
            return None
        idx = self._year_period_index()
        if not (0 <= idx < len(self._period_starts)):
            return None
        return self._period_focus_meta(idx)

    def _period_focus_meta(self, idx: int) -> tuple[str, int, time_of_day | None]:
        period_start = self._period_starts[idx]
        period_day = (
            self._period_renewal_days[idx]
            if 0 <= idx < len(self._period_renewal_days)
            and self._period_renewal_days[idx]
            else self._renewal_day
        )
        period_time = (
            self._period_renewal_times[idx]
            if 0 <= idx < len(self._period_renewal_times)
            else self._renewal_time
        )
        if period_time is None:
            period_time = self._renewal_time
        return period_start, period_day, period_time

    def _notify_focused_period(self) -> None:
        if not self._period_starts:
            return
        idx = self._year_period_index()
        if not (0 <= idx < len(self._period_starts)):
            return
        period_start, period_day, period_time = self._period_focus_meta(idx)
        tod = period_time or time_of_day(0, 0)
        key = (period_start, period_day, tod.strftime("%H:%M:%S"))
        period_changed = key != self._last_focused_period_key
        if period_changed:
            self._last_focused_period_key = key
            # New billing month → drop any day drill-down from the prior month.
            self._clear_focused_day()
            self.focused_period_changed.emit(period_start, period_day, period_time)

    def _day_iso_for_index(self, index: int | None) -> str | None:
        if self._mode != "month" or index is None or not self._month_period_start:
            return None
        if not (0 <= index < self._month_day_count):
            return None
        try:
            start = date.fromisoformat(self._month_period_start)
        except ValueError:
            return None
        # Axis is contiguous from period start (same as usage daily series).
        return (start + timedelta(days=index)).isoformat()

    def _clear_focused_day(self) -> None:
        if self._last_focused_day is None:
            return
        self._last_focused_day = None
        if self._emit_day_focus:
            self.focused_day_changed.emit(None)

    def _notify_focused_day(self, index: int | None) -> None:
        if not self._emit_day_focus:
            return
        day = self._day_iso_for_index(index)
        if day == self._last_focused_day:
            return
        self._last_focused_day = day
        self.focused_day_changed.emit(day)

    def _commits_aligned_to_days(self, days: list[str]) -> list[float | None] | None:
        """Map cached GitHub daily commits onto the token day axis, or None if N/A."""
        entry = self._github_commits_by_period.get(self._month_period_start)
        if entry is None:
            return None
        day_labels, daily_commits = entry
        if not day_labels or len(day_labels) != len(daily_commits):
            return None
        by_label = {
            label: float(count)
            for label, count in zip(day_labels, daily_commits, strict=True)
        }
        return [by_label.get(day, 0.0) for day in days]

    def apply_preview(
        self,
        preview: VscdbHabitsPreview,
        usage: UsageCsvPreview | None = None,
        sdk: SdkHabitsPreview | None = None,
        github: GithubHabitsPreview | None = None,
        *,
        renewal_day: int = 19,
        renewal_time: time_of_day | None = None,
    ) -> None:
        usage = usage or UsageCsvPreview.unavailable("")
        sdk = sdk or SdkHabitsPreview.unavailable("")
        self._renewal_day = renewal_day
        self._renewal_time = renewal_time
        if github is not None and github.available and github.period_start:
            self._github_commits_by_period[github.period_start] = (
                list(github.day_labels),
                list(github.daily_commits),
            )

        (
            labels,
            period_starts,
            tokens_legacy,
            tokens_current,
            auto_legacy,
            auto_current,
            api_legacy,
            api_current,
            grok_legacy,
            grok_current,
            renewal_days,
            renewal_times,
            period_ends,
        ) = _merge_composer_usage_series(usage)

        if len(labels) < 2:
            message = (
                usage.error_message
                or preview.error_message
                or sdk.error_message
                or "Not enough billing-period history yet"
            )
            self._content.hide()
            self._fallback.setText(message)
            self._fallback.show()
            return

        self._fallback.hide()
        self._content.show()

        self._year_labels = labels
        self._period_starts = period_starts
        self._period_renewal_days = renewal_days
        self._period_renewal_times = renewal_times
        self._period_ends = period_ends
        self._tokens_legacy = tokens_legacy
        self._tokens_current = tokens_current
        self._auto_legacy = auto_legacy
        self._auto_current = auto_current
        self._api_legacy = api_legacy
        self._api_current = api_current
        self._grok_legacy = grok_legacy
        self._grok_current = grok_current
        if self._selected_year_index is not None and not (
            0 <= self._selected_year_index < len(labels)
        ):
            self._selected_year_index = None

        self._render_active_view()
        self._notify_focused_period()

    def _on_view_mode(self, mode: str) -> None:
        if mode == self._mode:
            return
        if mode == "month" and self._mode == "year":
            self._selected_year_index = self._chart.selected_index()
        if mode == "year" and self._mode == "month":
            self._clear_focused_day()
        self._mode = mode
        self._view_toggle.set_mode(mode, animate=True)
        self._render_active_view()
        self._notify_focused_period()

    def _render_active_view(self) -> None:
        if self._mode == "month":
            self._render_month_view()
        else:
            self._render_year_view()

    def _year_period_index(self) -> int:
        if (
            self._selected_year_index is not None
            and 0 <= self._selected_year_index < len(self._period_starts)
        ):
            return self._selected_year_index
        return max(0, len(self._period_starts) - 1)

    def _sync_month_nav(self) -> None:
        n = len(self._period_starts)
        if self._mode != "month" or n == 0:
            self._chart.set_footer_nav_visible(False)
            return
        idx = self._year_period_index()
        self._chart.set_footer_nav_visible(True)
        self._chart.set_footer_nav_enabled(prev=idx > 0, next=idx < n - 1)

    def _step_month_period(self, delta: int) -> None:
        if self._mode != "month" or not self._period_starts:
            return
        idx = self._year_period_index() + delta
        if not (0 <= idx < len(self._period_starts)):
            return
        self._selected_year_index = idx
        self._render_month_view()
        self._notify_focused_period()

    def _render_year_view(self) -> None:
        labels = self._year_labels
        tokens_legacy = self._tokens_legacy
        tokens_current = self._tokens_current
        auto_legacy = self._auto_legacy
        auto_current = self._auto_current
        api_legacy = self._api_legacy
        api_current = self._api_current
        grok_legacy = self._grok_legacy
        grok_current = self._grok_current

        token_total = sum(
            int(v)
            for col in (tokens_legacy, tokens_current)
            for v in col
            if v is not None
        )
        caption_bits = [f"Billing periods · {len(labels)}"]
        if token_total:
            caption_bits.append(f"{token_total:,} tokens")
        footer = " · ".join(caption_bits)
        self._chart.set_footer_nav_visible(False)

        has_tok_legacy = any(v is not None for v in tokens_legacy)
        has_tok_current = any(v is not None for v in tokens_current)
        dual = has_tok_legacy and has_tok_current
        has_auto_legacy = any(v is not None for v in auto_legacy)
        has_auto_current = any(v is not None for v in auto_current)
        has_api_legacy = any(v is not None for v in api_legacy)
        has_api_current = any(v is not None for v in api_current)
        has_grok_legacy = any(v is not None for v in grok_legacy)
        has_grok_current = any(v is not None for v in grok_current)
        for chip in self._month_chips:
            chip.hide()
        # Chip visibility is period-focused in `_update_year_chip_values`.

        series: list[ChartSeries] = []
        if dual:
            series.append(
                ChartSeries("Tokens", "#E0C090", tokens_legacy, area=True)
            )
            series.append(
                ChartSeries("Tokens · new", "#7EB8A0", tokens_current, area=True)
            )
        elif has_tok_legacy:
            series.append(
                ChartSeries("Tokens", "#E0C090", tokens_legacy, area=True)
            )
        else:
            series.append(
                ChartSeries("Tokens", "#E0C090", tokens_current, area=True)
            )
        if dual:
            if has_auto_legacy:
                series.append(
                    ChartSeries("AUTO %", "#6FA8DC", auto_legacy, unit="%")
                )
            if has_auto_current:
                series.append(
                    ChartSeries("AUTO · new", "#8EC0E8", auto_current, unit="%")
                )
            if has_api_legacy:
                series.append(
                    ChartSeries("API %", "#B0B0B0", api_legacy, unit="%")
                )
            if has_api_current:
                series.append(
                    ChartSeries("API · new", "#D0D0D0", api_current, unit="%")
                )
            if has_grok_legacy:
                series.append(
                    ChartSeries("Grok %", "#C9A8D4", grok_legacy, unit="%")
                )
            if has_grok_current:
                series.append(
                    ChartSeries("Grok · new", "#D4B8E0", grok_current, unit="%")
                )
        else:
            auto = auto_legacy if has_auto_legacy else auto_current
            api = api_legacy if has_api_legacy else api_current
            grok = grok_legacy if has_grok_legacy else grok_current
            if any(v is not None for v in auto):
                series.append(ChartSeries("AUTO %", "#6FA8DC", auto, unit="%"))
            if any(v is not None for v in api):
                series.append(ChartSeries("API %", "#B0B0B0", api, unit="%"))
            if any(v is not None for v in grok):
                series.append(ChartSeries("Grok %", "#C9A8D4", grok, unit="%"))

        plan_change_index = next(
            (
                i
                for i, (legacy, current) in enumerate(
                    zip(tokens_legacy, tokens_current, strict=True)
                )
                if legacy is not None and current is not None
            ),
            None,
        )
        self._chart.set_data(
            labels,
            series,
            caption=footer,
            plan_change_index=plan_change_index,
        )
        if self._selected_year_index is not None:
            self._chart.select_index(self._selected_year_index)
        self._update_year_chip_values(self._chart.selected_index())
        self._sync_series_chip_states()
        self._notify_focused_period()

    def _render_month_view(self) -> None:
        if not self._period_starts:
            self._content.hide()
            self._fallback.setText("No billing period selected")
            self._fallback.show()
            self._month_day_count = 0
            self._sync_month_nav()
            return

        # Pin an explicit index so ‹ › have a stable position.
        if self._selected_year_index is None:
            self._selected_year_index = self._year_period_index()

        idx = self._year_period_index()
        period_start = self._period_starts[idx]
        period_day = (
            self._period_renewal_days[idx]
            if 0 <= idx < len(self._period_renewal_days)
            and self._period_renewal_days[idx]
            else self._renewal_day
        )
        period_time = (
            self._period_renewal_times[idx]
            if 0 <= idx < len(self._period_renewal_times)
            else self._renewal_time
        )
        if period_time is None:
            period_time = self._renewal_time
        inclusive_end = (
            self._period_ends[idx]
            if 0 <= idx < len(self._period_ends) and self._period_ends[idx]
            else None
        )
        daily = load_period_daily_series(
            period_start,
            renewal_day=period_day,
            renewal_time=period_time,
            inclusive_end=inclusive_end,
        )
        if not daily.available:
            for chip in self._year_chips:
                chip.hide()
            for chip in self._month_chips:
                chip.hide()
            self._sync_chips_scroll_width()
            label = self._year_labels[idx] if idx < len(self._year_labels) else period_start
            footer = f"{label} · {daily.error_message or 'No daily data'}"
            self._chart.set_data([], [], caption=footer)
            self._daily = []
            self._cumulative = []
            self._commits = []
            self._month_day_count = 0
            self._month_period_start = period_start
            self._sync_month_nav()
            self._notify_focused_period()
            return

        self._fallback.hide()
        self._content.show()
        self._month_period_start = daily.period_start
        self._daily = list(daily.daily_tokens)
        self._cumulative = list(daily.cumulative_tokens)
        day_labels = list(daily.days)
        self._month_day_count = len(day_labels)
        commits = self._commits_aligned_to_days(day_labels)
        if commits is not None:
            # Match token series: no commit dots on future (None) days.
            self._commits = [
                None if tok is None else count
                for tok, count in zip(self._daily, commits, strict=True)
            ]
        else:
            self._commits = []

        for chip in self._year_chips:
            chip.hide()
        self._chip_daily.show()
        self._chip_cumulative.show()
        self._chip_commits.setVisible(bool(self._commits))

        caption_bits = [
            daily.label,
            f"{len(daily.days)} days",
        ]
        if daily.total_tokens:
            caption_bits.append(f"{daily.total_tokens:,} tokens")
        if self._commits:
            commit_total = int(
                sum(v for v in self._commits if v is not None)
            )
            if commit_total:
                caption_bits.append(f"{commit_total} commits")
        footer = " · ".join(caption_bits)
        self._sync_month_nav()

        series = [
            ChartSeries(
                "Daily", "#E0C090", list(daily.daily_tokens), area=True
            ),
            ChartSeries(
                "Cumulative", "#6FA8DC", list(daily.cumulative_tokens)
            ),
        ]
        if self._commits:
            series.append(
                ChartSeries(
                    "Commits",
                    "#A8C3A4",
                    list(self._commits),
                )
            )

        self._chart.set_data(day_labels, series, caption=footer)
        selected = self._chart.selected_index()
        self._update_month_chip_values(selected)
        self._sync_series_chip_states()
        self._notify_focused_period()
        self._notify_focused_day(selected)

    def _on_point_selected(self, index: int | None) -> None:
        if self._mode == "month":
            self._update_month_chip_values(index)
            self._notify_focused_day(index)
            return
        self._selected_year_index = index
        self._update_year_chip_values(index)
        self._notify_focused_period()

    def clear_period_selection(self) -> None:
        self._chart.clear_selection()
        if self._mode == "month":
            self._update_month_chip_values(None)
            self._notify_focused_day(None)
        else:
            self._selected_year_index = None
            self._update_year_chip_values(None)
            self._notify_focused_period()

    def _update_year_chip_values(self, index: int | None) -> None:
        """Show only chips with values for the focused period (selected or latest)."""

        # Focus the selected period; otherwise the latest axis slot.
        if index is not None and 0 <= index < len(self._year_labels):
            focus = index
        elif self._year_labels:
            focus = len(self._year_labels) - 1
        else:
            focus = None

        def _at(values: list[float | None]) -> float | None:
            if focus is None or not (0 <= focus < len(values)):
                return None
            return values[focus]

        dual_history = any(v is not None for v in self._tokens_legacy) and any(
            v is not None for v in self._tokens_current
        )

        tok_l = _at(self._tokens_legacy)
        tok_c = _at(self._tokens_current)
        auto_l = _at(self._auto_legacy)
        auto_c = _at(self._auto_current)
        api_l = _at(self._api_legacy)
        api_c = _at(self._api_current)
        grok_l = _at(self._grok_legacy)
        grok_c = _at(self._grok_current)

        def _set_chip(
            chip: SeriesToggleChip,
            *,
            visible: bool,
            text: str,
        ) -> None:
            chip.setVisible(visible)
            if visible:
                chip.setText(text)

        if dual_history:
            _set_chip(
                self._chip_tokens,
                visible=tok_l is not None,
                text=_format_token_chip(int(tok_l)) if tok_l is not None else "",
            )
            _set_chip(
                self._chip_tokens_new,
                visible=tok_c is not None,
                text=_format_token_chip(int(tok_c)) if tok_c is not None else "",
            )
            _set_chip(
                self._chip_auto,
                visible=auto_l is not None,
                text=f"{auto_l:.0f}% AUTO" if auto_l is not None else "",
            )
            _set_chip(
                self._chip_auto_new,
                visible=auto_c is not None,
                text=f"{auto_c:.0f}% AUTO" if auto_c is not None else "",
            )
            _set_chip(
                self._chip_api,
                visible=api_l is not None,
                text=f"{api_l:.0f}% API" if api_l is not None else "",
            )
            _set_chip(
                self._chip_api_new,
                visible=api_c is not None,
                text=f"{api_c:.0f}% API" if api_c is not None else "",
            )
            _set_chip(
                self._chip_grok,
                visible=grok_l is not None,
                text=f"{grok_l:.0f}% Grok" if grok_l is not None else "",
            )
            _set_chip(
                self._chip_grok_new,
                visible=grok_c is not None,
                text=f"{grok_c:.0f}% Grok" if grok_c is not None else "",
            )
        else:
            tokens = tok_c if tok_c is not None else tok_l
            auto = auto_c if auto_c is not None else auto_l
            api = api_c if api_c is not None else api_l
            grok = grok_c if grok_c is not None else grok_l
            self._chip_tokens_new.hide()
            self._chip_auto_new.hide()
            self._chip_api_new.hide()
            self._chip_grok_new.hide()
            _set_chip(
                self._chip_tokens,
                visible=tokens is not None,
                text=_format_token_chip(int(tokens)) if tokens is not None else "",
            )
            _set_chip(
                self._chip_auto,
                visible=auto is not None,
                text=f"{auto:.0f}% AUTO" if auto is not None else "",
            )
            _set_chip(
                self._chip_api,
                visible=api is not None,
                text=f"{api:.0f}% API" if api is not None else "",
            )
            _set_chip(
                self._chip_grok,
                visible=grok is not None,
                text=f"{grok:.0f}% Grok" if grok is not None else "",
            )

        self._sync_chips_scroll_width()
        self._sync_series_chip_states()

    def _update_month_chip_values(self, index: int | None) -> None:
        def _pick(values: list[float | None]) -> float | None:
            if index is not None:
                return values[index] if 0 <= index < len(values) else None
            return next((v for v in reversed(values) if v is not None), None)

        daily = _pick(self._daily)
        self._chip_daily.setText(
            "— day" if daily is None else _format_token_chip(int(daily)).replace(
                " tokens", " day"
            )
        )
        cum = _pick(self._cumulative)
        self._chip_cumulative.setText(
            "— total"
            if cum is None
            else _format_token_chip(int(cum)).replace(" tokens", " total")
        )
        commits = _pick(self._commits)
        if commits is None:
            self._chip_commits.setText("— commits")
        else:
            n = int(commits)
            self._chip_commits.setText(
                f"{n} commit" if n == 1 else f"{n} commits"
            )
        self._sync_chips_scroll_width()

    def _on_series_chip_toggled(self, series_name: str) -> None:
        visible = self._chart.toggle_series(series_name)
        for chip in self._series_chips:
            if chip.series_name == series_name:
                chip.set_series_on(visible)
                break

    def _sync_series_chip_states(self) -> None:
        for chip in self._series_chips:
            if chip.isHidden():
                continue
            chip.set_series_on(self._chart.is_series_visible(chip.series_name))


class CountdownLabel(QLabel):
    """Clickable countdown — click triggers an immediate refresh."""

    clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Fixed height so leftover popup space cannot stretch this into a fat bar.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        font = QFont()
        font.setPointSize(9)
        self.setFont(font)
        self.setStyleSheet(
            """
            QLabel {
                color: #A0A0A0;
                padding: 8px 12px;
                border-radius: 8px;
            }
            QLabel:hover {
                color: #E8E8E8;
                background: #2C2C2C;
            }
            """
        )
        self.setFixedHeight(self.sizeHint().height())

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def set_remaining(self, seconds: int, refreshing: bool = False) -> None:
        if refreshing:
            self.setText("Refreshing… · click to retry")
        elif seconds < 0:
            self.setText("Updates paused · click to retry")
        else:
            minutes, secs = divmod(max(0, seconds), 60)
            self.setText(f"Next update in {minutes:02d}:{secs:02d} · click to refresh")
        hint_h = self.sizeHint().height()
        if self.height() != hint_h:
            self.setFixedHeight(hint_h)


class PinButton(QWidget):
    """Toggle that pins the popup open and above other windows."""

    toggled = pyqtSignal(bool)

    _SIZE = 26

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._checked = False
        self._hover = False
        self.setFixedSize(self._SIZE, self._SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._sync_tooltip()

    def is_checked(self) -> bool:
        return self._checked

    def set_checked(self, checked: bool) -> None:
        if checked == self._checked:
            return
        self._checked = checked
        self._sync_tooltip()
        self.update()

    def _sync_tooltip(self) -> None:
        self.setToolTip(
            "Unpin — close when clicking elsewhere"
            if self._checked
            else "Pin — keep open and on top of other windows"
        )

    def enterEvent(self, event) -> None:  # noqa: ANN001
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: ANN001
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.set_checked(not self._checked)
            self.toggled.emit(self._checked)
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:  # noqa: ANN001
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._checked:
            painter.setBrush(QColor("#22314A"))
            painter.setPen(QPen(QColor("#3A5378"), 1))
        elif self._hover:
            painter.setBrush(QColor("#2C2C2C"))
            painter.setPen(Qt.PenStyle.NoPen)
        else:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0), 8, 8)

        if self._checked:
            color = QColor("#8BA4C7")
        elif self._hover:
            color = QColor("#E8E8E8")
        else:
            color = QColor("#A0A0A0")
        painter.translate(self.width() / 2.0, self.height() / 2.0)
        # Unpinned pins lean over; pinned ones stand upright.
        painter.rotate(0 if self._checked else 45)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(QRectF(-3.5, -8.0, 7.0, 7.5), 1.5, 1.5)
        painter.drawRoundedRect(QRectF(-6.0, -1.5, 12.0, 2.5), 1.2, 1.2)
        pen = QPen(color, 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(QPointF(0, 1.0), QPointF(0, 8.0))
        painter.end()


def _format_signed_lines(n: int, *, positive: bool) -> str:
    """Compact +1.2k / -350 style line-change label."""
    sign = "+" if positive else "−"
    abs_n = abs(int(n))
    if abs_n >= 1_000_000:
        body = f"{abs_n / 1_000_000:.1f}M"
    elif abs_n >= 10_000:
        body = f"{abs_n / 1_000:.0f}k"
    elif abs_n >= 1_000:
        body = f"{abs_n / 1_000:.1f}k"
    else:
        body = str(abs_n)
    return f"{sign}{body}"


class _ElidedLabel(QLabel):
    """Single-line label that shrinks with an ellipsis instead of painting over neighbors."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full = text
        self.setWordWrap(False)
        self.setMinimumWidth(0)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        if text:
            self.setToolTip(text)

    def setFullText(self, text: str) -> None:
        self._full = text
        self.setToolTip(text)
        self._apply_elide()

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, QFontMetrics(self.font()).height())

    def sizeHint(self) -> QSize:  # noqa: N802
        # Don't force the row wider than the panel for long project names.
        return QSize(80, QFontMetrics(self.font()).height())

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self) -> None:
        width = self.width()
        if width <= 1:
            self.setText(self._full)
            return
        metrics = QFontMetrics(self.font())
        self.setText(
            metrics.elidedText(self._full, Qt.TextElideMode.ElideRight, width)
        )


class GithubBranchDropdown(QLabel):
    """Minimal branch picker — shows the active branch; click opens main/dev menu."""

    changed = pyqtSignal(str)

    _OPTIONS: tuple[tuple[str, str], ...] = (
        (BRANCH_PREF_MAIN, "main"),
        (BRANCH_PREF_DEV, "dev"),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._branch = BRANCH_PREF_MAIN
        font = QFont()
        font.setPointSize(8)
        font.setWeight(QFont.Weight.DemiBold)
        self.setFont(font)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.setToolTip(
            "Branch to count — your authored commits on this branch\n"
            "in the billing period (sum of per-commit +/- lines)."
        )
        self.setStyleSheet(
            """
            QLabel {
                color: #C5CEDA;
                background: transparent;
                border: none;
                padding: 0px;
            }
            QLabel:hover {
                color: #F2F4F8;
                text-decoration: underline;
            }
            """
        )
        self._apply()

    def branch(self) -> str:
        return self._branch

    def set_branch(self, branch: str) -> None:
        self._branch = BRANCH_PREF_DEV if branch == BRANCH_PREF_DEV else BRANCH_PREF_MAIN
        self._apply()

    def _apply(self) -> None:
        self.setText(f"{self._branch} ▾")
        metrics = QFontMetrics(self.font())
        self.setFixedWidth(metrics.horizontalAdvance(self.text()) + 2)

    def enterEvent(self, event) -> None:  # noqa: ANN001
        font = QFont(self.font())
        font.setUnderline(True)
        self.setFont(font)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: ANN001
        font = QFont(self.font())
        font.setUnderline(False)
        self.setFont(font)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        ):
            self._show_menu()
        super().mouseReleaseEvent(event)

    def _parent_of_type(self, cls: type[QWidget]) -> QWidget | None:
        current = self.parent()
        while current is not None:
            if isinstance(current, cls):
                return current
            current = current.parent()
        return None

    def _show_menu(self) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(
            """
            QMenu {
                background: #1A1E24;
                border: 1px solid #3A465A;
                border-radius: 6px;
                padding: 4px;
            }
            QMenu::item {
                color: #C5CEDA;
                padding: 4px 14px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background: #2A3548;
                color: #F2F4F8;
            }
            """
        )
        group = QActionGroup(menu)
        group.setExclusive(True)
        for value, label in self._OPTIONS:
            action = QAction(label, menu)
            action.setCheckable(True)
            action.setChecked(value == self._branch)
            action.setData(value)
            group.addAction(action)
            menu.addAction(action)

        card = self._parent_of_type(GithubProjectCard)
        popup = self._parent_of_type(SpendPopup)
        if isinstance(card, GithubProjectCard):
            card.suppress_repo_open()
        if isinstance(popup, SpendPopup):
            popup.begin_nested_ui()
        try:
            chosen = menu.exec(self.mapToGlobal(self.rect().bottomRight()))
        finally:
            if isinstance(popup, SpendPopup):
                popup.end_nested_ui()
            if isinstance(card, GithubProjectCard):
                card.release_repo_open_suppress()
        if chosen is None:
            return
        value = str(chosen.data() or "")
        if value not in (BRANCH_PREF_MAIN, BRANCH_PREF_DEV) or value == self._branch:
            return
        self.set_branch(value)
        self.changed.emit(value)


def _github_owner_repo(full_name: str, fallback_name: str = "") -> tuple[str, str]:
    """Split ``owner/repo``; fall back to a bare repo name when owner is missing."""
    full = (full_name or "").strip()
    if "/" in full:
        owner, repo = full.split("/", 1)
        owner, repo = owner.strip(), repo.strip()
        if owner and repo:
            return owner, repo
    name = (fallback_name or full or "project").strip()
    return "", name


def _sort_github_projects(
    projects: tuple[GithubProjectRow, ...] | list[GithubProjectRow],
    sort: GithubProjectSort | str,
) -> list[GithubProjectRow]:
    """Order project cards by commits, PRs, or lines changed (desc)."""
    mode = normalize_github_project_sort(sort)

    def key(row: GithubProjectRow) -> tuple:
        name = row.name.lower()
        if mode == "prs":
            return (-row.pr_count, -row.commit_count, -row.additions, name)
        if mode == "lines":
            churn = row.additions + row.deletions
            return (-churn, -row.commit_count, -row.pr_count, name)
        return (-row.commit_count, -row.pr_count, -row.additions, name)

    return sorted(projects, key=key)


class GithubProjectSortDropdown(QLabel):
    """Sort control for GitHub project cards — click opens commits/PRs/lines menu."""

    changed = pyqtSignal(str)

    _OPTIONS: tuple[tuple[GithubProjectSort, str], ...] = tuple(
        (key, GITHUB_PROJECT_SORT_LABELS[key]) for key in GITHUB_PROJECT_SORTS
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._sort: GithubProjectSort = DEFAULT_GITHUB_PROJECT_SORT
        font = QFont()
        font.setPointSize(8)
        font.setWeight(QFont.Weight.DemiBold)
        self.setFont(font)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.setToolTip("Sort projects by commits, PRs, or lines changed")
        self.setStyleSheet(
            """
            QLabel {
                color: #C5CEDA;
                background: transparent;
                border: none;
                padding: 0px;
            }
            QLabel:hover {
                color: #F2F4F8;
                text-decoration: underline;
            }
            """
        )
        self._apply()

    def sort_mode(self) -> GithubProjectSort:
        return self._sort

    def set_sort_mode(self, sort: GithubProjectSort | str) -> None:
        self._sort = normalize_github_project_sort(sort)
        self._apply()

    def _apply(self) -> None:
        label = GITHUB_PROJECT_SORT_LABELS[self._sort]
        self.setText(f"{label} ▾")
        metrics = QFontMetrics(self.font())
        self.setFixedWidth(metrics.horizontalAdvance(self.text()) + 2)

    def enterEvent(self, event) -> None:  # noqa: ANN001
        font = QFont(self.font())
        font.setUnderline(True)
        self.setFont(font)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: ANN001
        font = QFont(self.font())
        font.setUnderline(False)
        self.setFont(font)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        ):
            self._show_menu()
        super().mouseReleaseEvent(event)

    def _parent_of_type(self, cls: type[QWidget]) -> QWidget | None:
        current = self.parent()
        while current is not None:
            if isinstance(current, cls):
                return current
            current = current.parent()
        return None

    def _show_menu(self) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(
            """
            QMenu {
                background: #1A1E24;
                border: 1px solid #3A465A;
                border-radius: 6px;
                padding: 4px;
            }
            QMenu::item {
                color: #C5CEDA;
                padding: 4px 14px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background: #2A3548;
                color: #F2F4F8;
            }
            """
        )
        group = QActionGroup(menu)
        group.setExclusive(True)
        for value, label in self._OPTIONS:
            action = QAction(label, menu)
            action.setCheckable(True)
            action.setChecked(value == self._sort)
            action.setData(value)
            group.addAction(action)
            menu.addAction(action)

        popup = self._parent_of_type(SpendPopup)
        if isinstance(popup, SpendPopup):
            popup.begin_nested_ui()
        try:
            chosen = menu.exec(self.mapToGlobal(self.rect().bottomRight()))
        finally:
            if isinstance(popup, SpendPopup):
                popup.end_nested_ui()
        if chosen is None:
            return
        value = normalize_github_project_sort(chosen.data())
        if value == self._sort:
            return
        self.set_sort_mode(value)
        self.changed.emit(value)


def _github_metric_label(text: str, *, color: str) -> QLabel:
    """Plain colored metric text — no chip/bubble chrome."""
    lbl = QLabel(text)
    font = QFont()
    font.setPointSize(8)
    font.setWeight(QFont.Weight.DemiBold)
    lbl.setFont(font)
    lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    lbl.setStyleSheet(
        f"color: {color}; background: transparent; border: none;"
    )
    return lbl


class GithubProjectCard(QFrame):
    """One repository’s billing-period GitHub activity."""

    branch_changed = pyqtSignal(str, str)  # full_name, preference

    def __init__(
        self,
        project: GithubProjectRow,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._full_name = (project.full_name or "").strip()
        self._press_pos: QPoint | None = None
        self._suppress_repo_open = False
        self._loading = False
        self.setObjectName("githubProjectCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            """
            QFrame#githubProjectCard {
                background: #1A1E24;
                border: 1px solid #2F3642;
                border-radius: 8px;
            }
            QFrame#githubProjectCard:hover {
                border-color: #4A5A72;
                background: #1C222A;
            }
            """
        )
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        tip = (
            f"Open {self._full_name} on GitHub"
            if self._full_name
            else "Open repository on GitHub"
        )
        self.setToolTip(tip)

        owner, repo = _github_owner_repo(project.full_name, project.name)

        org_lbl = QLabel(owner or "personal")
        org_font = QFont()
        org_font.setPointSize(8)
        org_font.setWeight(QFont.Weight.DemiBold)
        org_lbl.setFont(org_font)
        org_lbl.setStyleSheet(
            "color: #8B95A5; background: transparent; border: none;"
        )
        org_lbl.setToolTip(tip)

        name_lbl = _ElidedLabel(repo)
        name_font = QFont()
        name_font.setPointSize(10)
        name_font.setWeight(QFont.Weight.DemiBold)
        name_lbl.setFont(name_font)
        name_lbl.setFullText(repo)
        name_lbl.setStyleSheet(
            "color: #E8ECF2; background: transparent; border: none;"
        )
        name_lbl.setToolTip(tip)

        title_col = QVBoxLayout()
        title_col.setContentsMargins(0, 0, 0, 0)
        title_col.setSpacing(1)
        title_col.addWidget(org_lbl)
        title_col.addWidget(name_lbl)

        self._branch_toggle = GithubBranchDropdown()
        self._branch_toggle.set_branch(project.branch_preference or BRANCH_PREF_MAIN)
        full_name = project.full_name

        def _on_branch(branch: str, full: str = full_name) -> None:
            if self._loading:
                return
            save_github_branch_pref(full, branch)
            self.branch_changed.emit(full, branch)

        self._branch_toggle.changed.connect(_on_branch)

        self._metric_commits = _github_metric_label(
            f"{project.commit_count} commits", color="#E0C090"
        )
        self._metric_prs = _github_metric_label(
            f"{project.pr_count} PRs", color="#9BB0C9"
        )
        self._metric_added = _github_metric_label(
            _format_signed_lines(project.additions, positive=True),
            color="#A8C3A4",
        )
        self._metric_removed = _github_metric_label(
            _format_signed_lines(project.deletions, positive=False),
            color="#D9897A",
        )
        self._fetching = QLabel("Fetching…")
        fetch_font = QFont()
        fetch_font.setPointSize(8)
        fetch_font.setWeight(QFont.Weight.DemiBold)
        self._fetching.setFont(fetch_font)
        self._fetching.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._fetching.setStyleSheet(
            "color: #7E8694; background: transparent; border: none;"
        )
        self._fetching.hide()

        metrics = QHBoxLayout()
        metrics.setContentsMargins(0, 0, 0, 0)
        metrics.setSpacing(8)
        metrics.addWidget(self._metric_commits)
        metrics.addWidget(self._metric_prs)
        metrics.addWidget(self._metric_added)
        metrics.addWidget(self._metric_removed)
        metrics.addWidget(self._fetching)

        right_col = QVBoxLayout()
        right_col.setContentsMargins(0, 0, 0, 0)
        right_col.setSpacing(4)
        right_col.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        right_col.addWidget(
            self._branch_toggle,
            stretch=0,
            alignment=Qt.AlignmentFlag.AlignRight,
        )
        right_col.addLayout(metrics)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addLayout(title_col, stretch=1)
        row.addLayout(right_col, stretch=0)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(0)
        layout.addLayout(row)

        self.installEventFilter(self)
        for child in self.findChildren(QWidget):
            if child is not self._branch_toggle:
                child.installEventFilter(self)

    @property
    def full_name(self) -> str:
        return self._full_name

    def set_loading(self, loading: bool, *, branch: str | None = None) -> None:
        """Show a per-card fetch state without removing sibling projects."""
        self._loading = bool(loading)
        if branch is not None:
            self._branch_toggle.set_branch(branch)
        self._branch_toggle.setEnabled(not self._loading)
        self._fetching.setVisible(self._loading)
        self._metric_commits.setVisible(not self._loading)
        self._metric_prs.setVisible(not self._loading)
        self._metric_added.setVisible(not self._loading)
        self._metric_removed.setVisible(not self._loading)
        if self._loading:
            pref = branch or self._branch_toggle.branch()
            self._fetching.setText(f"Fetching details from {pref}…")
            self.setCursor(Qt.CursorShape.ArrowCursor)
        else:
            self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _is_branch_toggle(self, widget: QObject | None) -> bool:
        current = widget
        while isinstance(current, QObject):
            if current is self._branch_toggle:
                return True
            current = current.parent()
        return False

    def suppress_repo_open(self) -> None:
        """Block card→browser opens while the branch menu is up (avoids ghost clicks)."""
        self._suppress_repo_open = True
        self._press_pos = None

    def release_repo_open_suppress(self) -> None:
        """Clear the block after the menu-dismiss click has been processed."""
        self._press_pos = None
        QTimer.singleShot(0, self._clear_repo_open_suppress)

    def _clear_repo_open_suppress(self) -> None:
        self._suppress_repo_open = False
        self._press_pos = None

    def _branch_toggle_global_rect(self) -> QRect:
        origin = self._branch_toggle.mapToGlobal(QPoint(0, 0))
        return QRect(origin, self._branch_toggle.size())

    def _open_repo(self) -> None:
        if self._suppress_repo_open:
            return
        if not self._full_name or "/" not in self._full_name:
            return
        QDesktopServices.openUrl(QUrl(f"https://github.com/{self._full_name}"))

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: ANN001
        if self._is_branch_toggle(obj):
            return False
        et = event.type()
        if et == QEvent.Type.MouseButtonPress:
            if self._suppress_repo_open:
                self._press_pos = None
                return False
            if getattr(event, "button", lambda: None)() == Qt.MouseButton.LeftButton:
                try:
                    self._press_pos = event.globalPosition().toPoint()
                except AttributeError:
                    self._press_pos = event.globalPos()
            return False
        if et == QEvent.Type.MouseButtonRelease:
            if (
                getattr(event, "button", lambda: None)() == Qt.MouseButton.LeftButton
                and self._press_pos is not None
                and not self._suppress_repo_open
            ):
                try:
                    release = event.globalPosition().toPoint()
                except AttributeError:
                    release = event.globalPos()
                if (
                    release - self._press_pos
                ).manhattanLength() < QApplication.startDragDistance() and (
                    not self._branch_toggle_global_rect().contains(release)
                ):
                    self._open_repo()
            self._press_pos = None
            return False
        return False


# How many project cards fit in the GitHub list viewport before scrolling.
_GITHUB_CARDS_VIEWPORT = 3
# Natural GithubProjectCard height (org + name + metrics, 9px vertical padding).
# Must match a real card — a taller guess makes the empty/loading viewport jump.
_GITHUB_CARD_PLACEHOLDER_H = 54


def _probe_github_card_height() -> int:
    """Measure one off-screen project card so empty/loading match a full row."""
    probe = GithubProjectCard(
        GithubProjectRow(
            name="repo",
            full_name="owner/repo",
            commit_count=0,
            pr_count=0,
            additions=0,
            deletions=0,
            branch_preference=BRANCH_PREF_MAIN,
        )
    )
    probe.ensurePolished()
    probe.adjustSize()
    h = max(probe.sizeHint().height(), probe.minimumSizeHint().height())
    probe.deleteLater()
    if h <= 0:
        return _GITHUB_CARD_PLACEHOLDER_H
    return min(h, _GITHUB_CARD_PLACEHOLDER_H * 2)


class GithubSummaryPreview(QFrame):
    """Authored commits + PRs by project for the active Cursor billing period."""

    branch_preference_changed = pyqtSignal(str, str)  # full_name, preference

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("githubSummary")
        # No outer chrome — repo cards own the visual edges.
        self.setStyleSheet(
            """
            QFrame#githubSummary {
                background: transparent;
                border: none;
            }
            """
        )
        # Slot height locked to a real 3-card row (probe once; refresh when cards appear).
        self._card_slot_h = _probe_github_card_height()

        self._title = QLabel("GitHub activity")
        title_font = QFont()
        title_font.setPointSize(11)
        title_font.setWeight(QFont.Weight.DemiBold)
        self._title.setFont(title_font)
        self._title.setStyleSheet(
            "color: #E8ECF2; background: transparent; border: none;"
        )
        self._title.setToolTip("Drag to reorder panels")

        self._window = QLabel("")
        win_font = QFont()
        win_font.setPointSize(8)
        self._window.setFont(win_font)
        self._window.setStyleSheet(
            "color: #6F7887; background: transparent; border: none;"
        )
        self._window.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

        header = QHBoxLayout()
        header.setContentsMargins(5, 0, 0, 0)
        header.setSpacing(8)
        header.addWidget(self._title, stretch=0)
        header.addStretch(1)
        header.addWidget(self._window, stretch=0)

        self._chip_commits = _habits_chip(
            "— commits", fg="#E0C090", bg="#242016", border="#4A3C28"
        )
        self._chip_prs = _habits_chip(
            "— PRs", fg="#9BB0C9", bg="#1E2530", border="#334155"
        )
        self._chip_added = _habits_chip(
            "+—", fg="#A8C3A4", bg="#1A2420", border="#2F4638"
        )
        self._chip_removed = _habits_chip(
            "−—", fg="#D9897A", bg="#241A1A", border="#46302F"
        )
        self._sort_mode: GithubProjectSort = load_github_project_sort()
        self._preview: GithubHabitsPreview | None = None
        self._sort_dropdown = GithubProjectSortDropdown()
        self._sort_dropdown.set_sort_mode(self._sort_mode)
        self._sort_dropdown.changed.connect(self._on_sort_changed)
        chips = QHBoxLayout()
        chips.setContentsMargins(0, 0, 0, 0)
        chips.setSpacing(4)
        chips.addWidget(self._chip_commits, stretch=0)
        chips.addWidget(self._chip_prs, stretch=0)
        chips.addWidget(self._chip_added, stretch=0)
        chips.addWidget(self._chip_removed, stretch=0)
        chips.addStretch(1)
        chips.addWidget(self._sort_dropdown, stretch=0)

        self._cards_host = QWidget()
        self._cards_host.setObjectName("githubCardsHost")
        self._cards_host.setStyleSheet(
            "QWidget#githubCardsHost { background: transparent; }"
        )
        self._cards_layout = QVBoxLayout(self._cards_host)
        self._cards_layout.setContentsMargins(0, 0, 0, 0)
        self._cards_layout.setSpacing(8)
        # Keep cards packed at the top — never distribute extra scroll-area space.
        self._cards_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._cards_scroll_needed = False
        self._pointer_inside = False
        self._reserved_cards_h = 0
        self._cards_scroll = QScrollArea()
        self._cards_scroll.setObjectName("githubCardsScroll")
        self._cards_scroll.setWidgetResizable(True)
        self._cards_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._cards_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._cards_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._cards_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._cards_scroll.setWidget(self._cards_host)
        # Start at a full 3-card viewport so the panel never boots collapsed.
        self._reserved_cards_h = self._viewport_height_for_card(self._card_slot_h)
        self._cards_scroll.setFixedHeight(self._reserved_cards_h)
        self._apply_cards_scroll_chrome()

        self._fallback = QLabel("Loading GitHub…")
        fb_font = QFont()
        fb_font.setPointSize(9)
        self._fallback.setFont(fb_font)
        self._fallback.setWordWrap(True)
        self._fallback.setStyleSheet(
            "color: #7E8694; background: transparent; border: none;"
        )
        self._fallback.hide()

        self._content = QWidget()
        content_layout = QVBoxLayout(self._content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(8)
        content_layout.addLayout(chips)
        content_layout.addWidget(self._cards_scroll)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addWidget(self._content)
        layout.addWidget(self._fallback)

    def drag_handle(self) -> QWidget:
        """Title label — sole drag affordance for reordering this panel."""
        return self._title

    def _apply_cards_scroll_chrome(self) -> None:
        """Reserve a scrollbar lane when needed; show the thumb only on hover."""
        if self._cards_scroll_needed:
            self._cards_scroll.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOn
            )
        else:
            self._cards_scroll.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
        show_thumb = self._cards_scroll_needed and self._pointer_inside
        handle = "#3A4454" if show_thumb else "transparent"
        handle_hover = "#4A5A72" if show_thumb else "transparent"
        self._cards_scroll.setStyleSheet(
            f"""
            QScrollArea#githubCardsScroll {{
                background: transparent;
                border: none;
            }}
            QScrollArea#githubCardsScroll > QWidget > QWidget {{
                background: transparent;
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 8px;
                margin: 0px 0px 0px 6px;
            }}
            QScrollBar::handle:vertical {{
                background: {handle};
                border-radius: 4px;
                min-height: 24px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {handle_hover};
            }}
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {{
                background: transparent;
            }}
            """
        )

    def enterEvent(self, event) -> None:  # noqa: ANN001
        self._pointer_inside = True
        self._apply_cards_scroll_chrome()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: ANN001
        self._pointer_inside = False
        self._apply_cards_scroll_chrome()
        super().leaveEvent(event)

    def _clear_cards(self) -> None:
        while self._cards_layout.count():
            item = self._cards_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _viewport_height_for_card(self, card_h: int) -> int:
        """Always reserve room for ``_GITHUB_CARDS_VIEWPORT`` project rows."""
        spacing = self._cards_layout.spacing()
        n = _GITHUB_CARDS_VIEWPORT
        return n * max(1, card_h) + max(0, n - 1) * spacing

    def _measure_project_card_height(self) -> int:
        """Natural project-card height from size hints (never the stretched paint height)."""
        card_h = 0
        for i in range(self._cards_layout.count()):
            item = self._cards_layout.itemAt(i)
            w = item.widget() if item is not None else None
            if not isinstance(w, GithubProjectCard):
                continue
            w.ensurePolished()
            # Do not use w.height() — inside a tall scroll host that value is
            # already stretched and would feed back into an ever-growing viewport.
            h = max(w.sizeHint().height(), w.minimumSizeHint().height())
            if h > card_h:
                card_h = h
        if card_h <= 0:
            # Keep the last known real slot height so empty/loading don't jump.
            return self._card_slot_h or _GITHUB_CARD_PLACEHOLDER_H
        # Guard against a pathological sizeHint blowing up the 3-slot viewport.
        card_h = min(card_h, _GITHUB_CARD_PLACEHOLDER_H * 2)
        self._card_slot_h = card_h
        return card_h

    def _apply_fixed_cards_viewport(
        self, *, card_count: int = 0, relayout_popup: bool = False
    ) -> None:
        """Lock the scroll area to three card slots (scroll when there are more)."""
        card_h = self._measure_project_card_height()
        viewport_h = self._viewport_height_for_card(card_h)
        self._reserved_cards_h = viewport_h
        self._cards_scroll.setFixedHeight(viewport_h)
        self._cards_scroll_needed = card_count > _GITHUB_CARDS_VIEWPORT
        self._apply_cards_scroll_chrome()
        if relayout_popup:
            host = self.parentWidget()
            while host is not None:
                if host.__class__.__name__ == "SpendPopup":
                    host.adjustSize()
                    break
                host = host.parentWidget()

    def _sync_cards_viewport(self, *, relayout_popup: bool = False) -> None:
        """Keep a fixed 3-card viewport; scroll when there are more projects."""
        card_count = 0
        for i in range(self._cards_layout.count()):
            item = self._cards_layout.itemAt(i)
            w = item.widget() if item is not None else None
            if isinstance(w, GithubProjectCard):
                card_count += 1
        self._apply_fixed_cards_viewport(
            card_count=card_count, relayout_popup=relayout_popup
        )

    def show_collecting(self, *, period_label: str = "") -> None:
        """Show a loader while commits are collected, keeping the 3-card height."""
        label = (period_label or "").strip()
        self._window.setText(label)
        message = (
            f"Collecting commits for {label}…"
            if label
            else "Collecting commits for this billing period…"
        )
        self._preview = None
        self._clear_cards()
        self._fallback.hide()
        self._content.show()

        placeholder = QLabel(message)
        ph_font = QFont()
        ph_font.setPointSize(9)
        placeholder.setFont(ph_font)
        placeholder.setWordWrap(True)
        placeholder.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        placeholder.setStyleSheet(
            "color: #7E8694; background: transparent; border: none;"
        )
        placeholder.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._cards_layout.addWidget(placeholder)
        self._apply_fixed_cards_viewport(card_count=0)

    def _on_sort_changed(self, sort: str) -> None:
        self._sort_mode = normalize_github_project_sort(sort)
        save_github_project_sort(self._sort_mode)
        if self._preview is not None and self._preview.available:
            self.apply_preview(self._preview)

    def set_project_loading(
        self, full_name: str, loading: bool, *, branch: str | None = None
    ) -> None:
        """Mark one project card as fetching; leave the rest of the list alone."""
        target = (full_name or "").strip()
        if not target:
            return
        for i in range(self._cards_layout.count()):
            item = self._cards_layout.itemAt(i)
            w = item.widget() if item is not None else None
            if isinstance(w, GithubProjectCard) and w.full_name == target:
                w.set_loading(loading, branch=branch)
                return

    def apply_preview(self, preview: GithubHabitsPreview) -> None:
        self._preview = preview
        self._clear_cards()
        if not preview.available:
            self._content.hide()
            self._window.setText("")
            self._fallback.setText(preview.error_message or "Unavailable")
            self._fallback.show()
            return

        self._fallback.hide()
        self._content.show()
        period = preview.period_label or "this period"
        login = f" · @{preview.login}" if preview.login else ""
        self._window.setText(f"{period}{login}")

        # Day drill-down labels are short ("Oct 20"); period labels contain an en-dash.
        day_scoped = bool(preview.period_label) and "–" not in preview.period_label

        self._chip_commits.setText(
            f"{preview.total_commits} commit"
            if preview.total_commits == 1
            else f"{preview.total_commits} commits"
        )
        self._chip_prs.setVisible(not day_scoped)
        if not day_scoped:
            self._chip_prs.setText(f"{preview.total_prs} PRs")
        self._chip_added.setText(
            _format_signed_lines(preview.total_additions, positive=True)
        )
        self._chip_removed.setText(
            _format_signed_lines(preview.total_deletions, positive=False)
        )

        if not preview.projects:
            empty_text = (
                f"No authored commits on {preview.period_label}"
                if day_scoped
                else "No authored commits or PRs in this billing period"
            )
            empty = QLabel(empty_text)
            empty_font = QFont()
            empty_font.setPointSize(9)
            empty.setFont(empty_font)
            empty.setStyleSheet(
                "color: #7E8694; background: transparent; border: none;"
            )
            self._cards_layout.addWidget(empty)
            self._sync_cards_viewport()
            QTimer.singleShot(
                0, lambda: self._sync_cards_viewport(relayout_popup=True)
            )
            return

        for project in _sort_github_projects(preview.projects, self._sort_mode):
            card = GithubProjectCard(project)
            card.branch_changed.connect(self.branch_preference_changed.emit)
            self._cards_layout.addWidget(card)
        self._sync_cards_viewport()
        # Re-measure after Qt finishes polishing card layouts.
        QTimer.singleShot(
            0, lambda: self._sync_cards_viewport(relayout_popup=True)
        )


class DraggablePanel(QFrame):
    """Content card that can be reordered by dragging.

    When ``drag_handle`` is set, only that widget (and its children) start a
    reorder — the rest of the panel keeps normal cursors/clicks.
    """

    def __init__(
        self,
        panel_id: str,
        body: QWidget,
        parent: QWidget | None = None,
        *,
        drag_handle: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.panel_id = panel_id
        self.body = body
        self._drag_handle = drag_handle
        self._press_global: QPoint | None = None
        self.setObjectName("draggablePanel")
        self.setAcceptDrops(False)
        if drag_handle is not None:
            self.setCursor(Qt.CursorShape.ArrowCursor)
            drag_handle.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(body)
        self.installEventFilter(self)
        body.installEventFilter(self)
        for child in body.findChildren(QWidget):
            child.installEventFilter(self)

    def _is_on_drag_handle(self, widget: QObject | None) -> bool:
        """True when drag may start from this widget (whole panel if no handle)."""
        if self._drag_handle is None:
            return True
        current = widget
        while isinstance(current, QObject):
            if current is self._drag_handle:
                return True
            current = current.parent()
        return False

    def _widget_blocks_drag(self, widget: QObject | None) -> bool:
        """Clicks on these controls should not start a panel reorder."""
        if not self._is_on_drag_handle(widget):
            return True
        current = widget
        while isinstance(current, QObject):
            if isinstance(
                current,
                (
                    CopyableCommand,
                    CountdownLabel,
                    SeriesToggleChip,
                    SegmentedToggle,
                    PeriodNavButton,
                    GithubBranchDropdown,
                    GithubProjectSortDropdown,
                    QScrollBar,
                ),
            ):
                return True
            current = current.parent()
        return False

    def _restore_cursors(self) -> None:
        if self._drag_handle is not None:
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self._drag_handle.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.setCursor(Qt.CursorShape.OpenHandCursor)

    def _start_panel_drag(self) -> None:
        self._press_global = None
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(PANEL_MIME, self.panel_id.encode("utf-8"))
        drag.setMimeData(mime)
        pix = self.grab()
        if not pix.isNull():
            scaled = pix.scaledToWidth(
                min(280, pix.width()), Qt.TransformationMode.SmoothTransformation
            )
            drag.setPixmap(scaled)
            drag.setHotSpot(QPoint(16, 16))
        handle = self._drag_handle or self
        handle.setCursor(Qt.CursorShape.ClosedHandCursor)
        drag.exec(Qt.DropAction.MoveAction)
        self._restore_cursors()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: ANN001
        et = event.type()
        if et == QEvent.Type.ChildAdded:
            child = event.child()  # type: ignore[attr-defined]
            if isinstance(child, QWidget):
                child.installEventFilter(self)
                for nested in child.findChildren(QWidget):
                    nested.installEventFilter(self)
            return False

        if et == QEvent.Type.MouseButtonPress:
            if (
                getattr(event, "button", lambda: None)() == Qt.MouseButton.LeftButton
                and not self._widget_blocks_drag(obj)
            ):
                try:
                    self._press_global = event.globalPosition().toPoint()
                except AttributeError:
                    self._press_global = event.globalPos()
            return False

        if et == QEvent.Type.MouseButtonRelease:
            self._press_global = None
            return False

        if et == QEvent.Type.MouseMove and self._press_global is not None:
            buttons = getattr(event, "buttons", lambda: Qt.MouseButton.NoButton)()
            if not (buttons & Qt.MouseButton.LeftButton):
                self._press_global = None
                return False
            try:
                current = event.globalPosition().toPoint()
            except AttributeError:
                current = event.globalPos()
            if (current - self._press_global).manhattanLength() < QApplication.startDragDistance():
                return False
            self._start_panel_drag()
            return True

        return False


class ReorderablePanelHost(QWidget):
    """Vertical stack of DraggablePanel widgets with drag-and-drop reordering."""

    order_changed = pyqtSignal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._panels: dict[str, DraggablePanel] = {}
        self._order: list[str] = []
        self._drop_index: int | None = None
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(10)

    def add_panel(self, panel: DraggablePanel) -> None:
        self._panels[panel.panel_id] = panel
        if panel.panel_id not in self._order:
            self._order.append(panel.panel_id)
        self._relayout()

    def set_order(self, order: list[str]) -> None:
        cleaned = [p for p in order if p in self._panels]
        for key in self._panels:
            if key not in cleaned:
                cleaned.append(key)
        self._order = cleaned
        self._relayout()

    def order(self) -> list[str]:
        return list(self._order)

    def panel(self, panel_id: str) -> DraggablePanel | None:
        return self._panels.get(panel_id)

    def _relayout(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(self)
        for panel_id in self._order:
            panel = self._panels.get(panel_id)
            if panel is not None:
                self._layout.addWidget(panel)

    def _index_for_y(self, y: int) -> int:
        if not self._order:
            return 0
        for i, panel_id in enumerate(self._order):
            panel = self._panels[panel_id]
            if not panel.isVisible():
                continue
            mid = panel.geometry().center().y()
            if y < mid:
                return i
        return len(self._order)

    def dragEnterEvent(self, event) -> None:  # noqa: ANN001
        if event.mimeData().hasFormat(PANEL_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: ANN001
        if not event.mimeData().hasFormat(PANEL_MIME):
            event.ignore()
            return
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        self._drop_index = self._index_for_y(pos.y())
        event.acceptProposedAction()
        self.update()

    def dragLeaveEvent(self, event) -> None:  # noqa: ANN001
        self._drop_index = None
        self.update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: ANN001
        if not event.mimeData().hasFormat(PANEL_MIME):
            event.ignore()
            return
        raw = bytes(event.mimeData().data(PANEL_MIME)).decode("utf-8")
        if raw not in self._order:
            event.ignore()
            return
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        target = self._index_for_y(pos.y())
        current = self._order.index(raw)
        order = list(self._order)
        order.pop(current)
        if target > current:
            target -= 1
        target = max(0, min(len(order), target))
        order.insert(target, raw)
        self._drop_index = None
        if order != self._order:
            self._order = order
            self._relayout()
            self.order_changed.emit(self.order())
        event.acceptProposedAction()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: ANN001
        super().paintEvent(event)
        if self._drop_index is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        y = 0
        visible = [pid for pid in self._order if self._panels[pid].isVisible()]
        if self._drop_index <= 0:
            y = 2
        elif self._drop_index >= len(visible):
            last = self._panels[visible[-1]] if visible else None
            y = (last.geometry().bottom() - 2) if last is not None else self.height() - 2
        else:
            # Map drop index onto visible panels
            count = 0
            for panel_id in self._order:
                panel = self._panels[panel_id]
                if not panel.isVisible():
                    continue
                if count == self._drop_index:
                    y = panel.geometry().top() - 4
                    break
                count += 1
            else:
                y = self.height() - 2
        painter.setPen(QPen(QColor("#8BA4C7"), 2))
        painter.drawLine(8, y, self.width() - 8, y)
        painter.end()


class _HabitsLoadWorker(QThread):
    """Load composer/usage/SDK/GitHub billing-period stats off the UI thread."""

    finished_ok = pyqtSignal(object, object, object, object)
    finished_err = pyqtSignal(str)

    def __init__(
        self,
        renewal_day: int,
        renewal_time: time_of_day | None,
        *,
        use_cache: bool = True,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._renewal_day = renewal_day
        self._renewal_time = renewal_time
        self._use_cache = use_cache

    def run(self) -> None:
        try:
            # Disk / cache first (fast), then local SDK scan, then gh (slowest).
            usage = load_usage_preview(
                renewal_day=self._renewal_day,
                renewal_time=self._renewal_time,
            )
            vscdb = collect_vscdb_habits_preview(
                renewal_day=self._renewal_day,
                renewal_time=self._renewal_time,
                use_cache=self._use_cache,
            )
            sdk = collect_habits_preview(
                renewal_day=self._renewal_day,
                renewal_time=self._renewal_time,
            )
            github = collect_github_habits_preview(
                renewal_day=self._renewal_day,
                renewal_time=self._renewal_time,
                use_cache=self._use_cache,
            )
            self.finished_ok.emit(vscdb, usage, sdk, github)
        except Exception as exc:  # noqa: BLE001 — surfaced to popup
            self.finished_err.emit(str(exc))


class _GithubPeriodLoadWorker(QThread):
    """Collect GitHub activity for one billing period (cache miss / nav)."""

    finished_ok = pyqtSignal(object)
    finished_err = pyqtSignal(str)

    def __init__(
        self,
        *,
        renewal_day: int,
        renewal_time: time_of_day | None,
        period_start: str,
        use_cache: bool = True,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._renewal_day = renewal_day
        self._renewal_time = renewal_time
        self._period_start = period_start
        self._use_cache = use_cache

    def run(self) -> None:
        try:
            github = collect_github_habits_preview(
                renewal_day=self._renewal_day,
                renewal_time=self._renewal_time,
                period_start=self._period_start,
                use_cache=self._use_cache,
            )
            self.finished_ok.emit(github)
        except Exception as exc:  # noqa: BLE001 — surfaced to popup
            self.finished_err.emit(str(exc))


class _GithubProjectBranchWorker(QThread):
    """Re-count a single repo after the user flips main/dev."""

    finished_ok = pyqtSignal(object)
    finished_err = pyqtSignal(str)

    def __init__(
        self,
        *,
        base: GithubHabitsPreview,
        full_name: str,
        preference: str,
        renewal_day: int,
        renewal_time: time_of_day | None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._base = base
        self._full_name = full_name
        self._preference = preference
        self._renewal_day = renewal_day
        self._renewal_time = renewal_time

    def run(self) -> None:
        try:
            github = refresh_github_project_branch(
                base=self._base,
                full_name=self._full_name,
                preference=self._preference,
                renewal_day=self._renewal_day,
                renewal_time=self._renewal_time,
            )
            self.finished_ok.emit(github)
        except Exception as exc:  # noqa: BLE001 — surfaced to popup
            self.finished_err.emit(str(exc))


def _renewal_boundary_key(
    renewal_day: int, renewal_time: time_of_day | None
) -> tuple[int, str]:
    tod = renewal_time or time_of_day(0, 0)
    return renewal_day, tod.strftime("%H:%M:%S")


class SpendPopup(QFrame):
    """Frameless tray panel. Closes when focus leaves or the user clicks outside.

    When pinned (pin button, or keep_open=True from the context menu), outside
    clicks and focus loss do not dismiss it — tray click (or hide()) still closes it.
    """

    refresh_requested = pyqtSignal()
    tray_metric_changed = pyqtSignal(str)
    open_animation_finished = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._refreshing = False
        self._habits_worker: _HabitsLoadWorker | None = None
        self._habits_gen = 0
        self._habits_status_when_done: str | None = None
        self._habits_running_key: tuple[int, str] | None = None
        self._habits_pending: tuple[int, time_of_day | None, bool] | None = None
        self._habits_renewal_day = 19
        self._habits_renewal_time: time_of_day | None = None
        self._github_by_period: dict[str, GithubHabitsPreview] = {}
        self._focused_github_period = ""
        self._focused_github_day: str | None = None
        self._github_period_worker: _GithubPeriodLoadWorker | None = None
        self._github_period_gen = 0
        self._github_branch_worker: _GithubProjectBranchWorker | None = None
        self._github_branch_gen = 0
        self._github_branch_pending: tuple[str, str] | None = None
        self._github_branch_loading: str = ""
        self._github_period_pending: (
            tuple[str, int, time_of_day | None, bool] | None
        ) = None
        self._github_period_running: str | None = None
        # Debounce + once-per-period guards so graph clicking cannot spam ``gh``.
        self._github_fetch_debounce_ms = 450
        self._github_rate_limit_backoff_s = 10 * 60
        self._github_fetch_started: set[str] = set()
        self._github_backoff_until: dict[str, float] = {}
        self._github_sync_request: (
            tuple[str, int, time_of_day | None, bool] | None
        ) = None
        self._github_sync_timer = QTimer(self)
        self._github_sync_timer.setSingleShot(True)
        self._github_sync_timer.timeout.connect(self._flush_github_period_sync)
        self._dismiss_armed = False
        self._keep_open = False
        self._nested_ui_depth = 0
        # Hold dismiss briefly after a child QMenu closes (focus blip on Plasma).
        self._nested_ui_grace_until = 0.0
        self._awaiting_login = False
        self._browser_inaccessible = False
        self._open_animation: PopupOpenAnimation = DEFAULT_POPUP_OPEN_ANIMATION
        self._open_anim_group: QParallelAnimationGroup | None = None
        self._open_notify_gen = 0
        self._popup_fixed_width = 420
        self._arm_timer = QTimer(self)
        self._arm_timer.setSingleShot(True)
        self._arm_timer.timeout.connect(self._arm_dismiss)
        self._status_clear_timer = QTimer(self)
        self._status_clear_timer.setSingleShot(True)
        self._status_clear_timer.timeout.connect(self._clear_ephemeral_status)
        self._outside_filter = _OutsideClickFilter(self)

        # Tool (not Popup): under Plasma/XWayland, Popup often never deactivates on
        # outside click. Tool + focus tracking matches normal tray-panel dismiss.
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        # Frameless Tool windows keep an opaque rectangular buffer unless this
        # is set — QSS border-radius alone leaves square filled corners.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        self.setAutoFillBackground(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedWidth(self._popup_fixed_width)
        # Transparent shell; chrome is painted in paintEvent so corners stay
        # anti-aliased and truly clear outside the 12px radius.
        self.setStyleSheet("SpendPopup { background: transparent; border: none; }")
        self._chrome_radius = 12.0
        self._chrome_fill = QColor("#141414")
        self._chrome_border = QColor("#2A2A2A")

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
        self._heading.setStyleSheet("color: #F2F2F2; background: transparent; border: none;")

        self._reset_label = QLabel("")
        reset_font = QFont()
        reset_font.setPointSize(10)
        self._reset_label.setFont(reset_font)
        self._reset_label.setStyleSheet(
            "color: #888888; background: transparent; border: none;"
        )
        self._reset_label.hide()

        heading_row = QHBoxLayout()
        heading_row.setContentsMargins(0, 0, 0, 0)
        heading_row.setSpacing(6)
        heading_row.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        heading_row.addWidget(self._heading)
        heading_row.addWidget(self._reset_label)

        self.ring_selector = UsageRingSelector()
        self.cursor_ring = self.ring_selector.cursor_ring
        self.other_ring = self.ring_selector.other_ring
        self.grok_ring = self.ring_selector.grok_ring
        self.ring_selector.selected_metric_changed.connect(self.tray_metric_changed.emit)

        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        card_layout.setSpacing(8)
        card_layout.addLayout(heading_row)
        card_layout.addWidget(self.ring_selector)

        self.browser_help = BrowserHelpBanner()
        self.habits = AgentHabitsPreview()
        self.composer_history = ComposerHistoryPreview()
        self.composer_history.focused_period_changed.connect(
            self._on_history_period_focused
        )
        self.composer_history.focused_day_changed.connect(
            self._on_history_day_focused
        )
        self.github_summary = GithubSummaryPreview()
        self.github_summary.branch_preference_changed.connect(
            self._on_github_branch_preference_changed
        )

        self._panel_host = ReorderablePanelHost()
        self._spend_panel = DraggablePanel("spend", self._card)
        self._habits_panel = DraggablePanel("habits", self.habits)
        self._composer_panel = DraggablePanel(
            "composer",
            self.composer_history,
            drag_handle=self.composer_history.drag_handle(),
        )
        self._github_panel = DraggablePanel(
            "github",
            self.github_summary,
            drag_handle=self.github_summary.drag_handle(),
        )
        self._panel_host.add_panel(self._spend_panel)
        self._panel_host.add_panel(self._habits_panel)
        self._panel_host.add_panel(self._composer_panel)
        self._panel_host.add_panel(self._github_panel)
        self._panel_host.set_order(load_popup_panel_order())
        self._panel_host.order_changed.connect(save_popup_panel_order)
        # Panel code stays mounted; flip `_SHOW_AGENT_HABITS_PANEL` to restore.
        self._habits_panel.setVisible(_SHOW_AGENT_HABITS_PANEL)

        self.countdown = CountdownLabel()
        self.countdown.clicked.connect(self.refresh_requested.emit)

        # Floats over the top-right corner of the first card, outside the layout.
        self.pin_button = PinButton(self)
        self.pin_button.toggled.connect(self.set_keep_open)

        # Same footprint as CountdownLabel so swapping them doesn't resize the popup.
        self.status = QLabel("")
        status_font = QFont()
        status_font.setPointSize(9)
        self.status.setFont(status_font)
        self.status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.status.setStyleSheet(
            """
            QLabel {
                color: #A0A0A0;
                padding: 8px 12px;
                border-radius: 8px;
                background: transparent;
                border: none;
            }
            """
        )
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setFixedHeight(self.countdown.height())
        self.status.hide()

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        root.addWidget(self.browser_help)
        root.addWidget(self._panel_host)
        root.addWidget(self.countdown)
        root.addWidget(self.status)
        self._place_pin_button()
        # Paint last-run History/GitHub immediately; worker refreshes in background.
        self._hydrate_habits_from_cache()
        self.refresh_habits()

    def _hydrate_habits_from_cache(self) -> None:
        """Show the last cached History / GitHub panels before the worker finishes."""
        snap = UsageSnapshot.load()
        renewal_day, renewal_time = snap.renewal_boundary()
        self._habits_renewal_day = renewal_day
        self._habits_renewal_time = renewal_time
        usage = load_usage_preview(
            renewal_day=renewal_day, renewal_time=renewal_time
        )
        vscdb = peek_cached_vscdb_habits_preview()
        github = peek_cached_github_habits_preview()
        painted = False
        if usage.available or vscdb is not None:
            self.composer_history.apply_preview(
                vscdb or VscdbHabitsPreview.unavailable(""),
                usage,
                SdkHabitsPreview.unavailable(""),
                github,
                renewal_day=renewal_day,
                renewal_time=renewal_time,
            )
            painted = True
        elif github is not None and github.available:
            self.composer_history.apply_github_commits(github)
        if github is not None and github.available:
            self._remember_github_preview(github)
            # Focused-period signal from apply_preview may already have painted.
            if self._focused_github_period:
                self._paint_github_for_focus()
            else:
                self.github_summary.apply_preview(github)
            painted = True
        if painted:
            self.adjustSize()

    def _remember_github_preview(self, preview: GithubHabitsPreview) -> None:
        if not preview.available or not preview.period_start:
            return
        self._github_by_period[preview.period_start] = preview
        self.composer_history.apply_github_commits(preview)

    def _github_preview_for(self, period_start: str) -> GithubHabitsPreview | None:
        if not period_start:
            return None
        cached = self._github_by_period.get(period_start)
        if cached is not None and cached.available:
            return cached
        peeked = peek_cached_github_habits_preview(period_start=period_start)
        if peeked is not None and peeked.available:
            self._github_by_period[period_start] = peeked
            self.composer_history.apply_github_commits(peeked)
            return peeked
        return None

    def _on_history_period_focused(
        self,
        period_start: str,
        renewal_day: int,
        renewal_time: object,
    ) -> None:
        """Keep GitHub projects/commits aligned with the chart's focused month."""
        typed_time = renewal_time if isinstance(renewal_time, time_of_day) else None
        # Month change clears any day drill-down from the history chart.
        self._focused_github_day = None
        self._sync_github_for_period(
            period_start,
            renewal_day=renewal_day,
            renewal_time=typed_time,
        )

    def _on_history_day_focused(self, day: object) -> None:
        """Filter GitHub projects to the selected Month-view calendar day."""
        self._focused_github_day = (
            day.strip() if isinstance(day, str) and day.strip() else None
        )
        # Day drill-down is local when day_stats exist; never spam gh per click.
        self._paint_github_for_focus()

    @staticmethod
    def _github_message_looks_rate_limited(message: str) -> bool:
        lower = (message or "").lower()
        return (
            "rate limit" in lower
            or "ratelimit" in lower
            or "secondary rate" in lower
            or ("403" in lower and "rate" in lower)
            or "http 429" in lower
            or " status 429" in lower
            or lower.strip().startswith("429")
        )

    def _github_in_backoff(self, period_start: str) -> bool:
        until = self._github_backoff_until.get(period_start)
        if until is None:
            return False
        if monotonic() >= until:
            self._github_backoff_until.pop(period_start, None)
            return False
        return True

    def _paint_github_for_focus(self) -> None:
        """Paint the GitHub panel for the focused period, optionally day-filtered."""
        period = self._focused_github_period
        if not period:
            return
        cached = self._github_preview_for(period)
        if cached is None:
            return
        day = self._focused_github_day
        if day and cached.has_day_stats():
            self.github_summary.apply_preview(github_preview_for_day(cached, day))
        else:
            # Show period totals while a one-shot day-stats upgrade may be pending.
            self.github_summary.apply_preview(cached)
            if day and not cached.has_day_stats():
                self._request_github_day_stats_upgrade(period)
        loading = self._github_branch_loading
        if loading:
            self.github_summary.set_project_loading(loading, True)
        self.adjustSize()

    def _request_github_day_stats_upgrade(self, period_start: str) -> None:
        """Fetch day breakdowns once for a period (debounced; never per-click)."""
        start = (period_start or "").strip()
        if (
            not start
            or start in self._github_fetch_started
            or self._github_in_backoff(start)
        ):
            return
        meta = self.composer_history.focused_period_meta()
        if meta is None or meta[0] != start:
            return
        _, renewal_day, renewal_time = meta
        self._github_sync_request = (start, renewal_day, renewal_time, True)
        self._github_sync_timer.start(self._github_fetch_debounce_ms)

    def _sync_github_for_period(
        self,
        period_start: str,
        *,
        renewal_day: int,
        renewal_time: time_of_day | None,
        force: bool = False,
    ) -> None:
        """Paint from cache immediately; debounce any ``gh`` collect for misses."""
        start = (period_start or "").strip()
        if not start:
            return
        self._focused_github_period = start

        cached = self._github_preview_for(start)
        if cached is not None and not force:
            self._paint_github_for_focus()
            return

        if not force and start in self._github_fetch_started:
            # Already fetched / in flight this session — do not re-hit gh on clicks.
            if cached is None and self._github_in_backoff(start):
                self.github_summary.apply_preview(
                    GithubHabitsPreview.unavailable(
                        "GitHub rate limited — try again in a few minutes"
                    )
                )
                self.adjustSize()
            return

        if not force and self._github_in_backoff(start):
            self.github_summary.apply_preview(
                GithubHabitsPreview.unavailable(
                    "GitHub rate limited — try again in a few minutes"
                )
            )
            self.adjustSize()
            return

        if cached is None:
            try:
                label = billing_period_label(
                    date.fromisoformat(start),
                    renewal_day=renewal_day,
                    renewal_time=renewal_time,
                )
            except ValueError:
                label = start
            collect_label = label
            day = self._focused_github_day
            if day:
                try:
                    collect_label = billing_short_period_label(
                        date.fromisoformat(day)
                    )
                except ValueError:
                    collect_label = day
            self.github_summary.show_collecting(period_label=collect_label)
            self.adjustSize()

        # Debounce: rapid graph clicks only keep the latest period request.
        self._github_sync_request = (start, renewal_day, renewal_time, force)
        self._github_sync_timer.start(self._github_fetch_debounce_ms)

    def _flush_github_period_sync(self) -> None:
        req = self._github_sync_request
        self._github_sync_request = None
        if req is None:
            return
        period_start, renewal_day, renewal_time, force = req
        if period_start != self._focused_github_period:
            return
        if not force:
            cached = self._github_preview_for(period_start)
            if cached is not None and (
                self._focused_github_day is None or cached.has_day_stats()
            ):
                self._paint_github_for_focus()
                return
            if period_start in self._github_fetch_started:
                return
            if self._github_in_backoff(period_start):
                return
        self._start_github_period_worker(
            period_start=period_start,
            renewal_day=renewal_day,
            renewal_time=renewal_time,
            use_cache=not force,
        )

    def _start_github_period_worker(
        self,
        *,
        period_start: str,
        renewal_day: int,
        renewal_time: time_of_day | None,
        use_cache: bool,
    ) -> None:
        worker = self._github_period_worker
        if worker is not None and worker.isRunning():
            if period_start == self._github_period_running and use_cache:
                return
            # Only keep the latest pending period — drop the rest.
            self._github_period_pending = (
                period_start,
                renewal_day,
                renewal_time,
                not use_cache,
            )
            return

        self._github_fetch_started.add(period_start)
        self._github_period_gen += 1
        gen = self._github_period_gen
        self._github_period_running = period_start
        self._github_period_pending = None
        self._github_period_worker = None

        worker = _GithubPeriodLoadWorker(
            renewal_day=renewal_day,
            renewal_time=renewal_time,
            period_start=period_start,
            use_cache=use_cache,
            parent=self,
        )
        worker.finished_ok.connect(
            lambda github, g=gen, ps=period_start: self._on_github_period_loaded(
                g, ps, github
            )
        )
        worker.finished_err.connect(
            lambda message, g=gen, ps=period_start: self._on_github_period_failed(
                g, ps, message
            )
        )
        worker.finished.connect(
            lambda w=worker: self._on_github_period_worker_finished(w)
        )
        self._github_period_worker = worker
        worker.start()

    def _on_github_period_worker_finished(
        self, worker: _GithubPeriodLoadWorker
    ) -> None:
        if self._github_period_worker is worker:
            self._github_period_worker = None
            self._github_period_running = None
        worker.deleteLater()
        pending = self._github_period_pending
        if pending is None:
            return
        period_start, renewal_day, renewal_time, force = pending
        self._github_period_pending = None
        # Only continue for the period the user still has focused.
        if period_start != self._focused_github_period:
            focused = self._focused_github_period
            if (
                focused
                and self._github_preview_for(focused) is None
                and focused not in self._github_fetch_started
                and not self._github_in_backoff(focused)
            ):
                meta = self.composer_history.focused_period_meta()
                if meta is not None and meta[0] == focused:
                    self._start_github_period_worker(
                        period_start=focused,
                        renewal_day=meta[1],
                        renewal_time=meta[2],
                        use_cache=True,
                    )
            return
        if (
            not force
            and self._github_preview_for(period_start) is not None
        ):
            self._paint_github_for_focus()
            return
        if not force and period_start in self._github_fetch_started:
            # A prior attempt already ran; do not loop.
            return
        self._start_github_period_worker(
            period_start=period_start,
            renewal_day=renewal_day,
            renewal_time=renewal_time,
            use_cache=not force,
        )

    def _on_github_period_loaded(
        self, gen: int, period_start: str, github: object
    ) -> None:
        if gen != self._github_period_gen:
            return
        assert isinstance(github, GithubHabitsPreview)
        if github.available:
            self._remember_github_preview(github)
            self._github_backoff_until.pop(period_start, None)
        elif self._github_message_looks_rate_limited(github.error_message):
            self._github_backoff_until[period_start] = (
                monotonic() + self._github_rate_limit_backoff_s
            )
        if period_start != self._focused_github_period:
            return
        if github.available:
            self._paint_github_for_focus()
        else:
            self.github_summary.apply_preview(github)
            self.adjustSize()

    def _on_github_period_failed(
        self, gen: int, period_start: str, message: str
    ) -> None:
        if gen != self._github_period_gen:
            return
        if self._github_message_looks_rate_limited(message):
            self._github_backoff_until[period_start] = (
                monotonic() + self._github_rate_limit_backoff_s
            )
        if period_start != self._focused_github_period:
            return
        self.github_summary.apply_preview(GithubHabitsPreview.unavailable(message))
        self.adjustSize()

    def _place_pin_button(self) -> None:
        inset = 12 + 6
        self.pin_button.move(self.width() - inset - self.pin_button.width(), inset)
        self.pin_button.raise_()

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        self._place_pin_button()

    def paintEvent(self, event) -> None:  # noqa: ANN001, ARG002
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setBrush(self._chrome_fill)
        painter.setPen(QPen(self._chrome_border, 1))
        painter.drawRoundedRect(rect, self._chrome_radius, self._chrome_radius)
        painter.end()

    def _on_github_branch_preference_changed(
        self, full_name: str, preference: str
    ) -> None:
        """Refresh only the flipped repo; keep sibling project cards visible."""
        full = (full_name or "").strip()
        pref = (preference or "").strip().lower()
        if not full or pref not in (BRANCH_PREF_MAIN, BRANCH_PREF_DEV):
            return
        short = full.split("/")[-1] if "/" in full else full
        self.set_status(f"Fetching {short} from {pref}…")
        self.github_summary.set_project_loading(full, True, branch=pref)
        self._github_branch_loading = full
        self._start_github_branch_worker(full, pref)

    def _start_github_branch_worker(self, full_name: str, preference: str) -> None:
        meta = self.composer_history.focused_period_meta()
        if meta is None:
            self._clear_github_branch_loading(full_name)
            self.set_status("No billing period focused")
            return
        period_start, renewal_day, renewal_time = meta
        base = self._github_preview_for(period_start)
        if base is None or not base.available:
            # No in-memory/disk period yet — fall back to a full collect once.
            self._clear_github_branch_loading(full_name)
            self._sync_github_for_period(
                period_start,
                renewal_day=renewal_day,
                renewal_time=renewal_time,
                force=True,
            )
            return

        worker = self._github_branch_worker
        if worker is not None and worker.isRunning():
            self._github_branch_pending = (full_name, preference)
            return

        self._github_branch_gen += 1
        gen = self._github_branch_gen
        self._github_branch_pending = None
        worker = _GithubProjectBranchWorker(
            base=base,
            full_name=full_name,
            preference=preference,
            renewal_day=renewal_day,
            renewal_time=renewal_time,
            parent=self,
        )
        worker.finished_ok.connect(
            lambda github, g=gen, full=full_name: self._on_github_branch_loaded(
                g, full, github
            )
        )
        worker.finished_err.connect(
            lambda message, g=gen, full=full_name: self._on_github_branch_failed(
                g, full, message
            )
        )
        worker.finished.connect(
            lambda w=worker: self._on_github_branch_worker_finished(w)
        )
        self._github_branch_worker = worker
        worker.start()

    def _clear_github_branch_loading(self, full_name: str = "") -> None:
        target = (full_name or self._github_branch_loading or "").strip()
        if target:
            self.github_summary.set_project_loading(target, False)
        if not full_name or full_name == self._github_branch_loading:
            self._github_branch_loading = ""

    def _on_github_branch_loaded(
        self, gen: int, full_name: str, github: object
    ) -> None:
        if gen != self._github_branch_gen:
            return
        assert isinstance(github, GithubHabitsPreview)
        self._clear_github_branch_loading(full_name)
        if not github.available:
            self.set_status(github.error_message or "GitHub refresh failed")
            return
        self._remember_github_preview(github)
        if github.period_start == self._focused_github_period:
            self._paint_github_for_focus()
        short = full_name.split("/")[-1] if "/" in full_name else full_name
        self.set_status(f"Updated {short}", clear_after_ms=3000)

    def _on_github_branch_failed(
        self, gen: int, full_name: str, message: str
    ) -> None:
        if gen != self._github_branch_gen:
            return
        self._clear_github_branch_loading(full_name)
        self.set_status(message or "GitHub refresh failed")

    def _on_github_branch_worker_finished(
        self, worker: _GithubProjectBranchWorker
    ) -> None:
        if self._github_branch_worker is worker:
            self._github_branch_worker = None
        worker.deleteLater()
        pending = self._github_branch_pending
        if pending is None:
            return
        self._github_branch_pending = None
        full_name, preference = pending
        self.github_summary.set_project_loading(
            full_name, True, branch=preference
        )
        self._github_branch_loading = full_name
        self._start_github_branch_worker(full_name, preference)

    def stop_habits_worker(self, *, timeout_ms: int = 8_000) -> None:
        """Halt the background habits loader before Qt tears down this popup.

        Destroying a still-running ``QThread`` (parented to this widget) aborts
        the process on quit. Reparent away, wait briefly, then let process exit
        reap a stuck scan rather than calling ``QThread.terminate``.
        """
        self._habits_pending = None
        self._habits_gen += 1
        self._github_period_pending = None
        self._github_branch_pending = None
        self._github_sync_request = None
        self._github_sync_timer.stop()
        self._github_period_gen += 1
        self._github_branch_gen += 1
        github_worker = self._github_period_worker
        self._github_period_worker = None
        self._github_period_running = None
        branch_worker = self._github_branch_worker
        self._github_branch_worker = None
        self._github_branch_loading = ""
        if branch_worker is not None:
            stop_qthread(
                branch_worker,
                wait_ms=timeout_ms,
                label="github branch worker",
                signals=(
                    branch_worker.finished_ok,
                    branch_worker.finished_err,
                    branch_worker.finished,
                ),
            )
        if github_worker is not None:
            stop_qthread(
                github_worker,
                wait_ms=timeout_ms,
                label="github period worker",
                signals=(
                    github_worker.finished_ok,
                    github_worker.finished_err,
                    github_worker.finished,
                ),
            )
        worker = self._habits_worker
        self._habits_worker = None
        if worker is None:
            return
        stop_qthread(
            worker,
            wait_ms=timeout_ms,
            label="habits worker",
            signals=(worker.finished_ok, worker.finished_err, worker.finished),
        )

    def prepare_shutdown(self) -> None:
        """Stop dismiss timers, hide, and join the habits thread for quit."""
        self._arm_timer.stop()
        self._dismiss_armed = False
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self._outside_filter)
            try:
                app.focusWindowChanged.disconnect(self._on_focus_window_changed)
            except TypeError:
                pass
        self.hide()
        self.stop_habits_worker()

    def refresh_habits(
        self,
        *,
        status_when_done: str | None = None,
        force: bool = False,
    ) -> None:
        """Reload local SDK + state.vscdb + usage-CSV habit stats off the UI thread.

        Concurrent calls with the same renewal boundary coalesce onto the in-flight
        worker (polls must not cancel a slow cache-miss scan). A different boundary
        or ``force=True`` queues one follow-up load after the current worker finishes.
        """
        if status_when_done is not None:
            self._habits_status_when_done = status_when_done
        snap = UsageSnapshot.load()
        renewal_day, renewal_time = snap.renewal_boundary()
        key = _renewal_boundary_key(renewal_day, renewal_time)

        worker = self._habits_worker
        if worker is not None and worker.isRunning():
            if not force and key == self._habits_running_key:
                return
            # Newest pending wins; keep force if any queued request asked for it.
            prev = self._habits_pending
            pending_force = force or (prev is not None and prev[2])
            self._habits_pending = (renewal_day, renewal_time, pending_force)
            return

        self._start_habits_worker(
            renewal_day, renewal_time, use_cache=not force
        )

    def _start_habits_worker(
        self,
        renewal_day: int,
        renewal_time: time_of_day | None,
        *,
        use_cache: bool,
    ) -> None:
        self._habits_gen += 1
        gen = self._habits_gen
        self._habits_running_key = _renewal_boundary_key(renewal_day, renewal_time)
        self._habits_renewal_day = renewal_day
        self._habits_renewal_time = renewal_time
        self._habits_pending = None
        # Drop the strong ref to any prior worker. Do not disconnect — the C++
        # object may already be deleteLater'd after finished, which raises
        # RuntimeError. Stale results are ignored via _habits_gen.
        self._habits_worker = None

        worker = _HabitsLoadWorker(
            renewal_day,
            renewal_time,
            use_cache=use_cache,
            parent=self,
        )
        worker.finished_ok.connect(
            lambda vscdb, usage, sdk, github, g=gen: self._on_habits_loaded(
                g, vscdb, usage, sdk, github
            )
        )
        worker.finished_err.connect(
            lambda message, g=gen: self._on_habits_load_failed(g, message)
        )
        worker.finished.connect(lambda w=worker: self._on_habits_worker_finished(w))
        self._habits_worker = worker
        worker.start()

    def _on_habits_worker_finished(self, worker: _HabitsLoadWorker) -> None:
        if self._habits_worker is worker:
            self._habits_worker = None
        worker.deleteLater()
        pending = self._habits_pending
        if pending is None:
            return
        renewal_day, renewal_time, force = pending
        self._habits_pending = None
        self._start_habits_worker(
            renewal_day, renewal_time, use_cache=not force
        )

    def _on_habits_loaded(
        self,
        gen: int,
        vscdb: object,
        usage: object,
        sdk: object,
        github: object,
    ) -> None:
        if gen != self._habits_gen:
            return
        assert isinstance(vscdb, VscdbHabitsPreview)
        assert isinstance(usage, UsageCsvPreview)
        assert isinstance(sdk, SdkHabitsPreview)
        assert isinstance(github, GithubHabitsPreview)
        self._apply_habits_previews(vscdb, usage, sdk, github)

    def _on_habits_load_failed(self, gen: int, message: str) -> None:
        if gen != self._habits_gen:
            return
        self._apply_habits_previews(
            VscdbHabitsPreview.unavailable(message),
            UsageCsvPreview.unavailable(message),
            SdkHabitsPreview.unavailable(message),
            GithubHabitsPreview.unavailable(message),
        )

    def adjustSize(self) -> None:  # noqa: N802
        """Use the fixed popup width when choosing height.

        QWidget.adjustSize() measures heightForWidth(sizeHint().width()).
        That width is narrower than this 420px popup, so the height is short
        and the layout crushes the history plot onto its bottom edge.
        """
        super().adjustSize()
        width = 420
        if not self.hasHeightForWidth():
            return
        height = self.heightForWidth(width)
        if height > 0 and (self.width() != width or self.height() != height):
            self.resize(width, height)
            layout = self.layout()
            if layout is not None:
                layout.activate()

    def _apply_habits_previews(
        self,
        vscdb: VscdbHabitsPreview,
        usage: UsageCsvPreview,
        sdk: SdkHabitsPreview,
        github: GithubHabitsPreview,
    ) -> None:
        if _SHOW_AGENT_HABITS_PANEL:
            self.habits.apply_preview(sdk)
        else:
            self._habits_panel.hide()
        # Incremental store: auto-refresh merges the live period into disk+memory.
        if github.available:
            self._remember_github_preview(github)
        self.composer_history.apply_preview(
            vscdb,
            usage,
            sdk,
            github,
            renewal_day=self._habits_renewal_day,
            renewal_time=self._habits_renewal_time,
        )
        # apply_preview notifies focus → syncs GitHub panel for that month.
        # Respect day drill-down; never clobber another month with live-period data.
        focused = self._focused_github_period
        if focused:
            self._paint_github_for_focus()
        elif github.available:
            self.github_summary.apply_preview(github)
        elif not github.available and not focused:
            self.github_summary.apply_preview(github)
        # A queued follow-up rebuild (boundary change / force) should keep the
        # status line until that load finishes.
        if self._habits_pending is not None:
            self.adjustSize()
            return
        done_status = self._habits_status_when_done
        self._habits_status_when_done = None
        if done_status is not None:
            self.set_status(done_status)
        self.adjustSize()

    def set_open_animation(self, mode: PopupOpenAnimation) -> None:
        """Select the open motion used by the next show_at()."""
        self._open_animation = mode

    def _move_popup_to(self, target: QPoint) -> None:
        """Place the frameless tool window; XWayland often ignores a lone move()."""
        self.setGeometry(target.x(), target.y(), self.width(), self.height())
        self.move(target)
        handle = self.windowHandle()
        if handle is not None:
            handle.setFramePosition(target)

    def show_at(  # noqa: ANN001
        self,
        pos,
        *,
        keep_open: bool = False,
        anchor: QPoint | None = None,
    ) -> None:
        """Show near tray; close when focus leaves or user clicks elsewhere.

        keep_open=True pins the panel until tray click / hide() — outside clicks
        and focus loss do not dismiss it.

        Habits are not rescanned here — refresh_habits() runs on startup and when
        usage updates (poll / Refresh now / countdown click).
        """
        self._dismiss_armed = False
        self._keep_open = keep_open
        self.pin_button.set_checked(keep_open)
        self._arm_timer.stop()
        self._stop_open_animation()
        self.adjustSize()
        target = pos if isinstance(pos, QPoint) else QPoint(pos.x(), pos.y())
        self._open_notify_gen += 1
        notify_gen = self._open_notify_gen
        if self._open_animation == "none":
            self.setWindowOpacity(1.0)
            # Under XWayland a single pre- or post-show move() is often ignored.
            self._move_popup_to(target)
            self.show()
            self._move_popup_to(target)
            QTimer.singleShot(
                0, lambda p=target: self._move_popup_to(p) if self.isVisible() else None
            )
            QTimer.singleShot(
                50, lambda p=target: self._move_popup_to(p) if self.isVisible() else None
            )
            # Instant open still notifies so preview→menu flows can continue.
            QTimer.singleShot(
                0, lambda g=notify_gen: self._emit_open_animation_finished(g)
            )
        else:
            self._start_open_animation(
                target, anchor=anchor, notify_gen=notify_gen
            )
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        self._wire_dismiss(keep_open)

    def _emit_open_animation_finished(self, notify_gen: int) -> None:
        if notify_gen != self._open_notify_gen:
            return
        self.open_animation_finished.emit()

    def _restore_popup_size_constraints(self) -> None:
        self.setFixedWidth(self._popup_fixed_width)

    def _stop_open_animation(self) -> None:
        group = self._open_anim_group
        self._open_anim_group = None
        if group is not None:
            group.stop()
            group.deleteLater()
        self.setWindowOpacity(1.0)
        self._restore_popup_size_constraints()

    def _scaled_start_rect(self, final: QRect, scale: float) -> QRect:
        sw = max(1, int(final.width() * scale))
        sh = max(1, int(final.height() * scale))
        return QRect(
            final.x() + (final.width() - sw) // 2,
            final.y() + (final.height() - sh) // 2,
            sw,
            sh,
        )

    def _start_open_animation(
        self,
        target: QPoint,
        *,
        anchor: QPoint | None,
        notify_gen: int,
    ) -> None:
        """Play the configured open motion, ending at ``target``."""
        width = max(self.width(), self._popup_fixed_width)
        height = max(self.height(), 1)
        final = QRect(target.x(), target.y(), width, height)
        mode = self._open_animation

        opacity = QPropertyAnimation(self, b"windowOpacity", self)
        opacity.setStartValue(0.0)
        opacity.setEndValue(1.0)

        group = QParallelAnimationGroup(self)

        if mode == "slide_fade":
            offset = 14
            # Tray above popup → slide down; tray below → slide up.
            if anchor is not None and anchor.y() > target.y() + height // 2:
                start_pos = QPoint(target.x(), target.y() + offset)
            else:
                start_pos = QPoint(target.x(), target.y() - offset)
            self.setWindowOpacity(0.0)
            self.setGeometry(start_pos.x(), start_pos.y(), width, height)
            self.show()
            pos_anim = QPropertyAnimation(self, b"pos", self)
            pos_anim.setStartValue(start_pos)
            pos_anim.setEndValue(target)
            pos_anim.setDuration(220)
            pos_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            opacity.setDuration(220)
            opacity.setEasingCurve(QEasingCurve.Type.OutCubic)
            group.addAnimation(pos_anim)
            group.addAnimation(opacity)
        else:
            # fade_scale: grow from a slightly smaller rect + fade.
            start = self._scaled_start_rect(final, 0.96)
            # Geometry anim needs free width; restore fixed width when done.
            self.setMinimumWidth(0)
            self.setMaximumWidth(16_777_215)
            self.setWindowOpacity(0.0)
            self.setGeometry(start)
            self.show()
            geo = QPropertyAnimation(self, b"geometry", self)
            geo.setStartValue(start)
            geo.setEndValue(final)
            geo.setDuration(200)
            geo.setEasingCurve(QEasingCurve.Type.OutCubic)
            opacity.setDuration(200)
            opacity.setEasingCurve(QEasingCurve.Type.OutCubic)
            group.addAnimation(geo)
            group.addAnimation(opacity)

        def _finish() -> None:
            self._open_anim_group = None
            self._restore_popup_size_constraints()
            self.setWindowOpacity(1.0)
            if self.isVisible():
                self._move_popup_to(target)
                # XWayland often ignores a single post-show move().
                QTimer.singleShot(
                    0,
                    lambda p=target: self._move_popup_to(p) if self.isVisible() else None,
                )
                QTimer.singleShot(
                    50,
                    lambda p=target: self._move_popup_to(p) if self.isVisible() else None,
                )
            self._emit_open_animation_finished(notify_gen)

        group.finished.connect(_finish)
        self._open_anim_group = group
        group.start()

    def set_keep_open(self, keep_open: bool) -> None:
        """Pin (or unpin) the visible popup without reopening it."""
        self.pin_button.set_checked(keep_open)
        if keep_open == self._keep_open:
            return
        self._keep_open = keep_open
        self._dismiss_armed = False
        self._arm_timer.stop()
        if not self.isVisible():
            return
        self.raise_()
        if not keep_open:
            self.activateWindow()
        self._wire_dismiss(keep_open)

    def _wire_dismiss(self, keep_open: bool) -> None:
        app = QApplication.instance()
        if app is None:
            return
        app.removeEventFilter(self._outside_filter)
        try:
            app.focusWindowChanged.disconnect(self._on_focus_window_changed)
        except TypeError:
            pass
        if keep_open:
            # Pinned: no outside-click filter / focus-leave dismiss wiring.
            return
        app.installEventFilter(self._outside_filter)
        # focusWindowChanged is the reliable leave signal on Plasma/XWayland.
        app.focusWindowChanged.connect(self._on_focus_window_changed)
        # Grace period: the tray Activate that opened us must not instantly dismiss.
        self._arm_timer.start(300)

    def begin_nested_ui(self) -> None:
        """Keep the unpinned popup open while a child QMenu (etc.) has focus."""
        self._nested_ui_depth += 1
        self._nested_ui_grace_until = 0.0

    def end_nested_ui(self) -> None:
        self._nested_ui_depth = max(0, self._nested_ui_depth - 1)
        if self._nested_ui_depth > 0:
            return
        # QMenu teardown often fires focusWindowChanged(None) / WindowDeactivate
        # before focus returns here — absorb that blip so the tray panel stays up.
        self._nested_ui_grace_until = monotonic() + 0.4
        if self.isVisible():
            self.raise_()
            self.activateWindow()

    def _nested_ui_open(self) -> bool:
        if self._nested_ui_depth > 0:
            return True
        return monotonic() < self._nested_ui_grace_until

    def _active_child_popup(self) -> QWidget | None:
        """QMenu (or similar) owned by this panel — e.g. the GitHub branch picker."""
        popup = QApplication.activePopupWidget()
        if popup is not None and self.isAncestorOf(popup):
            return popup
        return None

    def _owns_window(self, window) -> bool:  # noqa: ANN001
        if window is self.windowHandle():
            return True
        # Child QMenu focus can briefly be None while exec() is opening/closing.
        if self._nested_ui_open():
            return True
        if window is None:
            return False
        child = self._active_child_popup()
        return child is not None and window is child.windowHandle()

    def _contains_global(self, global_pos: QPoint) -> bool:
        # While a nested menu is up (or just closed), treat presses as inside so
        # the outside-click filter does not dismiss on the menu click itself.
        if self._nested_ui_open():
            return True
        if self.frameGeometry().contains(global_pos):
            return True
        child = self._active_child_popup()
        return child is not None and child.frameGeometry().contains(global_pos)

    def _arm_dismiss(self) -> None:
        if self._keep_open:
            return
        self._dismiss_armed = True
        # If focus already left during the grace period, close now.
        if self.isVisible() and not self._owns_window(QGuiApplication.focusWindow()):
            self.hide()

    def _on_focus_window_changed(self, window) -> None:  # noqa: ANN001
        if self._keep_open or not self._dismiss_armed or not self.isVisible():
            return
        if self._owns_window(window):
            return
        self.hide()

    def dismiss_if_outside(self, global_pos: QPoint) -> bool:
        """Hide when a press lands outside this panel. Returns True if dismissed."""
        if self._keep_open or not self._dismiss_armed or not self.isVisible():
            return False
        if self._contains_global(global_pos):
            return False
        self.hide()
        return True

    def changeEvent(self, event) -> None:  # noqa: ANN001
        if (
            event.type() == QEvent.Type.WindowDeactivate
            and self.isVisible()
            and self._dismiss_armed
            and not self._keep_open
            and not self._owns_window(QGuiApplication.focusWindow())
        ):
            self.hide()
        super().changeEvent(event)

    def hideEvent(self, event) -> None:  # noqa: ANN001
        self._arm_timer.stop()
        self._status_clear_timer.stop()
        self._stop_open_animation()
        self._dismiss_armed = False
        self._keep_open = False
        self._nested_ui_depth = 0
        self._nested_ui_grace_until = 0.0
        self.pin_button.set_checked(False)
        self.composer_history.clear_period_selection()
        BubbleTip.hide_shared()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self._outside_filter)
            try:
                app.focusWindowChanged.disconnect(self._on_focus_window_changed)
            except TypeError:
                pass
        super().hideEvent(event)

    def apply_snapshot(
        self,
        snap: UsageSnapshot,
        *,
        habits_status_when_done: str | None = None,
        force_habits: bool = False,
    ) -> None:
        self.cursor_ring.set_percent(snap.cursor_models_pct)
        self.other_ring.set_percent(snap.other_models_pct)
        self.grok_ring.set_percent(snap.grok_bot_pct)
        self.grok_ring.set_subtitle("weekly")
        grok_hint = (snap.grok_bot_reset_hint or "").strip()
        if grok_hint:
            self.grok_ring.set_tooltip(
                "Grok Bot weekly allowance (renews weekly).\n"
                f"Resets {grok_hint}.\n"
                "Pauses at the limit until the weekly reset."
            )
        else:
            self.grok_ring.set_tooltip(
                "Grok Bot weekly allowance (renews weekly).\n"
                "Pauses at the limit until the weekly reset."
            )
        reset_at = snap.effective_usage_reset_at()
        if reset_at is not None:
            self._reset_label.setText(
                f"- {format_usage_reset_label(reset_at)}"
            )
            self._reset_label.show()
        else:
            self._reset_label.clear()
            self._reset_label.hide()
        # Rescan local habits when Cursor usage updates (poll or Refresh).
        self.refresh_habits(
            status_when_done=habits_status_when_done,
            force=force_habits,
        )

    def set_browser_inaccessible(self, inaccessible: bool, launch_command: str = "") -> None:
        """Show or hide the Browser inaccessible banner with a copyable launch command."""
        self._browser_inaccessible = inaccessible
        if inaccessible:
            self._spend_panel.hide()
            self.browser_help.set_launch_command(launch_command)
            self.browser_help.show()
        else:
            self._spend_panel.show()
            self.browser_help.hide()
        self._sync_countdown_visibility()
        self.adjustSize()

    def set_awaiting_login(self, awaiting: bool) -> None:
        """Hide the countdown while signed out; only the sign-in status should show."""
        self._awaiting_login = awaiting
        self._sync_countdown_visibility()
        self.adjustSize()

    def _sync_countdown_visibility(self) -> None:
        """Show either the countdown or the status line — never both — in one slot."""
        has_status = bool(self.status.text().strip())
        hide_countdown = bool(
            has_status
            or getattr(self, "_browser_inaccessible", False)
            or getattr(self, "_awaiting_login", False)
        )
        self.countdown.setVisible(not hide_countdown)
        self.status.setVisible(has_status)
        if has_status:
            # Keep the status row the same height as the countdown button.
            self.status.setFixedHeight(self.countdown.sizeHint().height())

    def set_status(self, text: str, *, clear_after_ms: int | None = None) -> None:
        clean = (text or "").strip()
        self._status_clear_timer.stop()
        self.status.setText(clean)
        self._sync_countdown_visibility()
        self.adjustSize()
        if clean and clear_after_ms is not None and clear_after_ms > 0:
            self._status_clear_timer.start(int(clear_after_ms))

    def _clear_ephemeral_status(self) -> None:
        """Drop a timed status line so the countdown / refresh control returns."""
        self.set_status("")

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
        if (
            not self._popup.isVisible()
            or self._popup._keep_open
            or not self._popup._dismiss_armed
            or self._popup._nested_ui_open()
        ):
            return False
        # Global coordinates — works for presses on other top-levels too.
        try:
            global_pos = event.globalPosition().toPoint()
        except AttributeError:
            global_pos = event.globalPos()
        if self._popup._contains_global(global_pos):
            return False
        self._popup.hide()
        return False


# Alias kept for older imports
SpendPanel = SpendPopup
