from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import (
    QEvent,
    QMimeData,
    QObject,
    QPoint,
    QPointF,
    QRectF,
    QSize,
    QTimer,
    Qt,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QDrag,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .config import (
    UsageSnapshot,
    load_popup_panel_order,
    save_popup_panel_order,
)
from .sdk_stats import SdkHabitsBatch, SdkHabitsPreview, collect_habits_preview
from .usage_csv import UsageCsvPreview, load_usage_preview
from .vscdb_stats import VscdbHabitsPreview, VscdbPeriodBucket, collect_vscdb_habits_preview

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
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("—", parent)
        self.series_name = series_name
        self._fg = fg
        self._bg = bg
        self._border = border
        self._series_on = True
        font = QFont()
        font.setPointSize(8)
        font.setWeight(QFont.Weight.DemiBold)
        self.setFont(font)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"Click to show/hide {series_name} on the chart")
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

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.MouseButton.LeftButton:
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
    """Single multi-line chart; series are min–max normalized; hover shows raw values."""

    # Desired plot body height; total widget height = legend + plot + axis.
    _PLOT_BODY = 260
    _AXIS_RESERVE = 18
    _LEGEND_TOP = 8
    _LEGEND_GAP = 10
    # Keep peaks/dots inside the plot — map series into this inset.
    _DRAW_INSET = 12.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._labels: list[str] = []
        self._weights: list[int] = []
        self._series: list[ChartSeries] = []
        self._hidden: set[str] = set()
        self._hover_index: int | None = None
        self._plot = QRectF()
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFixedHeight(self._height_for_rows(1))

    def _legend_font(self) -> QFont:
        font = QFont()
        font.setPointSize(7)
        return font

    def _legend_row_height(self) -> float:
        return float(max(12, QFontMetrics(self._legend_font()).height()))

    def _height_for_rows(self, rows: int) -> int:
        rows = max(1, rows)
        row_h = self._legend_row_height()
        legend = (
            self._LEGEND_TOP
            + rows * row_h
            + max(0, rows - 1) * 2
            + self._LEGEND_GAP
        )
        return int(legend + self._PLOT_BODY + self._AXIS_RESERVE)

    def _estimate_legend_rows(self, width: int) -> int:
        """How many legend rows are needed at the given width (matches paintEvent)."""
        if not self._series:
            return 1
        metrics = QFontMetrics(self._legend_font())
        lx = 10.0
        rows = 1
        for series in self._series:
            name_w = max(72, metrics.horizontalAdvance(series.name) + 4)
            entry_w = 14 + name_w
            if lx > 10.0 and lx + entry_w > width - 10:
                lx = 10.0
                rows += 1
            lx += entry_w + 8
        return rows

    def sizeHint(self) -> QSize:  # noqa: N802
        width = self.width() if self.width() > 1 else 360
        return QSize(360, self._height_for_rows(self._estimate_legend_rows(width)))

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(200, self._height_for_rows(1))

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        # Legend wrap depends on width — refresh fixed height when it changes.
        hint_h = self.sizeHint().height()
        if self.height() != hint_h:
            self.setFixedHeight(hint_h)
            self.updateGeometry()
        super().resizeEvent(event)

    def set_data(
        self,
        labels: list[str],
        series: list[ChartSeries],
        *,
        weights: list[int] | None = None,
        caption: str = "",
    ) -> None:
        self._labels = list(labels)
        self._series = list(series)
        self._weights = list(weights or [])
        names = {s.name for s in self._series}
        self._hidden &= names
        self._hover_index = None
        self.setToolTip(caption or "Hover a billing period for exact values")
        self.setFixedHeight(self.sizeHint().height())
        self.updateGeometry()
        self.update()

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
        return visible

    def _visible_series(self) -> list[ChartSeries]:
        return [s for s in self._series if s.name not in self._hidden]

    def _draw_rect(self) -> QRectF:
        """Inset viewing area for series — peaks stay clear of plot edges."""
        inset = self._DRAW_INSET
        r = self._plot
        if r.height() <= inset * 2 + 4:
            return r
        return QRectF(
            r.left(),
            r.top() + inset,
            r.width(),
            r.height() - 2 * inset,
        )

    def leaveEvent(self, event) -> None:  # noqa: ANN001
        self._hover_index = None
        self.update()
        super().leaveEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001
        if len(self._labels) < 2 or self._plot.width() <= 0:
            return
        x = event.position().x() if hasattr(event, "position") else event.x()
        n = len(self._labels)
        rel = (x - self._plot.left()) / max(1.0, self._plot.width())
        idx = int(round(rel * (n - 1)))
        idx = max(0, min(n - 1, idx))
        if idx != self._hover_index:
            self._hover_index = idx
            self.update()
        super().mouseMoveEvent(event)

    def _normalized_points(self, values: list[float | None]) -> list[QPointF | None]:
        numeric = [v for v in values if v is not None]
        if not numeric:
            return [None] * len(values)
        lo = min(numeric)
        hi = max(numeric)
        if hi <= lo:
            hi = lo + 1.0
        # Extra headroom so max/min don't sit on the draw-rect edge.
        pad = (hi - lo) * 0.14
        lo -= pad
        hi += pad
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

        # Legend (may wrap to multiple rows when many series)
        legend_y = 8.0
        lx = 10.0
        leg_font = QFont()
        leg_font.setPointSize(7)
        painter.setFont(leg_font)
        metrics = painter.fontMetrics()
        row_h = float(max(12, metrics.height()))
        for series in self._series:
            name_w = max(72, metrics.horizontalAdvance(series.name) + 4)
            entry_w = 14 + name_w
            if lx > 10.0 and lx + entry_w > self.width() - 10:
                lx = 10.0
                legend_y += row_h + 2
            # Dot centered on the same row box as the label.
            cy = legend_y + row_h / 2.0
            visible = series.name not in self._hidden
            dot = QColor(series.color)
            label_color = QColor("#9AA6B8")
            if not visible:
                dot.setAlpha(70)
                label_color = QColor("#4A5565")
            painter.setBrush(dot)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPointF(lx + 3, cy), 3, 3)
            painter.setPen(label_color)
            painter.drawText(
                QRectF(lx + 10, legend_y, name_w, row_h),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                series.name,
            )
            lx += entry_w + 8

        axis_reserve = self._AXIS_RESERVE
        plot_top = legend_y + row_h + self._LEGEND_GAP
        available = self.height() - plot_top - axis_reserve
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
            painter.drawText(
                self._plot,
                int(Qt.AlignmentFlag.AlignCenter),
                "Need 2+ billing periods",
            )
            painter.end()
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
            painter.end()
            return

        # Grid
        painter.setPen(QPen(QColor("#1E2633"), 1, Qt.PenStyle.DotLine))
        for frac in (0.25, 0.5, 0.75):
            y = self._plot.top() + self._plot.height() * frac
            painter.drawLine(
                QPointF(self._plot.left(), y), QPointF(self._plot.right(), y)
            )

        # Area fills first (behind), then every line/dot on top.
        area_series = [s for s in visible_series if s.area]
        line_series = [s for s in visible_series if not s.area]
        for series in area_series:
            self._paint_area_series(painter, series)
        for series in [*area_series, *line_series]:
            self._paint_line_series(painter, series)

        # Axis labels — reserved strip below the plot, never over lines
        axis = QFont()
        axis.setPointSize(7)
        painter.setFont(axis)
        painter.setPen(QColor("#5E6775"))
        painter.drawText(
            QRectF(
                self._plot.left(),
                self._plot.bottom() + 2,
                self._plot.width(),
                axis_reserve - 2,
            ),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            f"{self._labels[0]} → now",
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

            lines = [self._labels[idx]]
            if idx < len(self._weights):
                lines[0] += f" · {self._weights[idx]}"
            hover_series = visible_series
            for series in hover_series:
                raw = series.values[idx] if idx < len(series.values) else None
                lines.append(
                    f"{series.name}: {_format_chart_value(raw, series.unit)}"
                )

            card_font = QFont()
            card_font.setPointSize(8)
            painter.setFont(card_font)
            metrics = painter.fontMetrics()
            width = max(metrics.horizontalAdvance(line) for line in lines) + 16
            height = metrics.height() * len(lines) + 10
            card_x = x + 10
            if card_x + width > self.width() - 8:
                card_x = x - width - 10
            card_x = max(8.0, card_x)
            # Prefer near the plot top; clamp to the full widget so tall cards
            # (many series) are not clipped by the plot strip or widget edge.
            card_y = self._plot.top() + 4
            if card_y + height > self.height() - 8:
                card_y = max(8.0, self.height() - 8 - height)
            card = QRectF(card_x, card_y, width, height)
            painter.setBrush(QColor(20, 24, 32, 230))
            painter.setPen(QPen(QColor("#3A465A"), 1))
            painter.drawRoundedRect(card, 6, 6)
            painter.setPen(QColor("#E4E7EC"))
            ty = card.top() + 5
            for i, line in enumerate(lines):
                if i == 0:
                    painter.setPen(QColor("#D5DEEA"))
                elif i - 1 < len(hover_series):
                    painter.setPen(QColor(hover_series[i - 1].color))
                painter.drawText(
                    QRectF(card.left() + 8, ty, card.width() - 12, metrics.height()),
                    line,
                )
                ty += metrics.height()

        painter.end()

    def _paint_area_series(self, painter: QPainter, series: ChartSeries) -> None:
        """Fill under the series curve down to the plot baseline (behind lines)."""
        points = self._normalized_points(series.values)
        usable = [p for p in points if p is not None]
        if len(usable) < 2:
            return
        fill = QColor(series.color)
        fill.setAlpha(55)
        stroke = QColor(series.color)
        stroke.setAlpha(160)
        base_y = self._plot.bottom()

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

    def _paint_line_series(self, painter: QPainter, series: ChartSeries) -> None:
        """Stroke + dots for a series (drawn after area fills)."""
        if series.area:
            # Area series: dots only on top of the fill; stroke already drawn softly.
            points = self._normalized_points(series.values)
            painter.setBrush(QColor(series.color))
            painter.setPen(Qt.PenStyle.NoPen)
            for pt in points:
                if pt is not None:
                    painter.drawEllipse(pt, 2.0, 2.0)
            return

        points = self._normalized_points(series.values)
        usable = [p for p in points if p is not None]
        if len(usable) < 2:
            return
        pen = QPen(QColor(series.color), 1.9)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(pen)
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
        painter.setBrush(Qt.BrushStyle.NoBrush)


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
        self.setToolTip(
            "Local Cursor SDK run stats from ~/.cursor/projects/*/sdk-agent-store "
            "(read-only). Use this to review prompt friction — not Pro spend."
        )

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


def _merge_composer_usage_series(
    preview: VscdbHabitsPreview,
    usage: UsageCsvPreview,
) -> tuple[
    list[str],
    list[float | None],
    list[float | None],
    list[float | None],
    list[float | None],
    list[float | None],
    list[float | None],
    list[float | None],
    list[int],
]:
    """Union billing periods from vscdb + usage CSV into aligned chart series."""
    by_start: dict[str, VscdbPeriodBucket] = {}
    if preview.available:
        for p in preview.periods:
            by_start[p.period_start] = p

    usage_by: dict[str, object] = {}
    if usage.available:
        for u in usage.periods:
            usage_by[u.period_start] = u

    starts = sorted(set(by_start) | set(usage_by))
    labels: list[str] = []
    composers: list[float | None] = []
    tokens: list[float | None] = []
    abort: list[float | None] = []
    tool_err: list[float | None] = []
    accept: list[float | None] = []
    auto_pct: list[float | None] = []
    api_pct: list[float | None] = []
    weights: list[int] = []

    for key in starts:
        v = by_start.get(key)
        u = usage_by.get(key)
        label = (
            (v.label if v else None)
            or (getattr(u, "label", None) if u is not None else None)
            or key
        )
        labels.append(str(label))
        if v is not None:
            composers.append(float(v.composers))
            abort.append(
                float(v.abort_rate_pct) if v.abort_rate_pct is not None else None
            )
            tool_err.append(
                float(v.tool_error_rate_pct)
                if v.tool_error_rate_pct is not None
                else None
            )
            accept.append(
                float(v.accept_rate_pct) if v.accept_rate_pct is not None else None
            )
            weights.append(v.composers)
        else:
            composers.append(None)
            abort.append(None)
            tool_err.append(None)
            accept.append(None)
            weights.append(0)
        if u is not None:
            tok = int(getattr(u, "total_tokens", 0) or 0)
            tokens.append(float(tok) if tok or getattr(u, "event_count", 0) else None)
            cp = getattr(u, "cursor_models_pct", None)
            op = getattr(u, "other_models_pct", None)
            auto_pct.append(float(cp) if cp is not None else None)
            api_pct.append(float(op) if op is not None else None)
        else:
            tokens.append(None)
            auto_pct.append(None)
            api_pct.append(None)

    return (
        labels,
        composers,
        tokens,
        abort,
        tool_err,
        accept,
        auto_pct,
        api_pct,
        weights,
    )


class ComposerHistoryPreview(QFrame):
    """Longer-range composer/tool trends from state.vscdb (+ usage CSV tokens)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("composerHistory")
        self.setStyleSheet(
            """
            QFrame#composerHistory {
                background: #15181C;
                border: 1px solid #2A313C;
                border-radius: 10px;
            }
            """
        )

        title = QLabel("Composer history")
        title_font = QFont()
        title_font.setPointSize(11)
        title_font.setWeight(QFont.Weight.DemiBold)
        title.setFont(title_font)
        title.setStyleSheet("color: #E8ECF2; background: transparent; border: none;")

        badge = _habits_chip(
            "state.vscdb",
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
        header.addWidget(badge, stretch=0)
        header.addStretch(1)
        header.addWidget(self._window, stretch=0)

        self._chip_composers = SeriesToggleChip(
            "Composers", fg="#A8C3A4", bg="#1A2420", border="#2F4638"
        )
        self._chip_tokens = SeriesToggleChip(
            "Tokens", fg="#E0C090", bg="#242016", border="#4A3C28"
        )
        self._chip_abort = SeriesToggleChip(
            "Abort %", fg="#D0B56C", bg="#242018", border="#4A4028"
        )
        self._chip_tool = SeriesToggleChip(
            "Tool error %", fg="#D9897A", bg="#261C1A", border="#4A322E"
        )
        self._chip_accept = SeriesToggleChip(
            "Accept %", fg="#8BA4C7", bg="#1A2030", border="#2F3B52"
        )
        self._chip_auto = SeriesToggleChip(
            "AUTO %", fg="#6FA8DC", bg="#1A2030", border="#2F3B52"
        )
        self._chip_api = SeriesToggleChip(
            "API %", fg="#B0B0B0", bg="#1E1E1E", border="#3A3A3A"
        )
        self._chip_auto.hide()
        self._chip_api.hide()

        self._series_chips: list[SeriesToggleChip] = [
            self._chip_composers,
            self._chip_tokens,
            self._chip_abort,
            self._chip_tool,
            self._chip_accept,
            self._chip_auto,
            self._chip_api,
        ]
        for chip in self._series_chips:
            chip.toggled.connect(self._on_series_chip_toggled)

        chips = QVBoxLayout()
        chips.setContentsMargins(0, 0, 0, 0)
        chips.setSpacing(6)
        chip_row1 = QHBoxLayout()
        chip_row1.setContentsMargins(0, 0, 0, 0)
        chip_row1.setSpacing(6)
        chip_row1.addWidget(self._chip_composers)
        chip_row1.addWidget(self._chip_tokens)
        chip_row1.addStretch(1)
        chip_row2 = QHBoxLayout()
        chip_row2.setContentsMargins(0, 0, 0, 0)
        chip_row2.setSpacing(6)
        chip_row2.addWidget(self._chip_abort)
        chip_row2.addWidget(self._chip_tool)
        chip_row2.addWidget(self._chip_accept)
        chip_row2.addStretch(1)
        chip_row3 = QHBoxLayout()
        chip_row3.setContentsMargins(0, 0, 0, 0)
        chip_row3.setSpacing(6)
        chip_row3.addWidget(self._chip_auto)
        chip_row3.addWidget(self._chip_api)
        chip_row3.addStretch(1)
        chips.addLayout(chip_row1)
        chips.addLayout(chip_row2)
        chips.addLayout(chip_row3)

        caption = QLabel("Billing periods from state.vscdb · older → newer")
        cap_font = QFont()
        cap_font.setPointSize(8)
        caption.setFont(cap_font)
        caption.setWordWrap(True)
        caption.setStyleSheet("color: #6F7887; background: transparent; border: none;")
        self._caption = caption
        self._chart = MultiSeriesHistoryChart()

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
        content_layout.addWidget(self._caption)
        content_layout.addWidget(self._chart)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 11, 12, 11)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addWidget(self._content)
        layout.addWidget(self._fallback)

    def apply_preview(
        self,
        preview: VscdbHabitsPreview,
        usage: UsageCsvPreview | None = None,
    ) -> None:
        self.setToolTip(
            "Composer signals from state.vscdb plus dashboard usage CSV token totals "
            "(billing periods). AUTO/API % associate from spend scrapes starting now. "
            f"CSVs: {(usage.csv_dir if usage else '') or '~/.local/share/cursor-spend-tray/usage-csv'}"
        )
        usage = usage or UsageCsvPreview.unavailable("")
        (
            labels,
            composers,
            tokens,
            abort,
            tool_err,
            accept,
            auto_pct,
            api_pct,
            weights,
        ) = _merge_composer_usage_series(preview, usage)
        if len(labels) < 2:
            message = (
                preview.error_message
                or usage.error_message
                or "Not enough billing-period history yet"
            )
            self._content.hide()
            self._window.setText("")
            self._fallback.setText(message)
            self._fallback.show()
            return

        self._fallback.hide()
        self._content.show()
        earliest = preview.earliest or (usage.earliest if usage.available else "")
        latest_label = preview.latest or (usage.latest if usage.available else "")
        self._window.setText(
            f"{earliest} → {latest_label}" if earliest else latest_label
        )

        token_total = sum(int(v) for v in tokens if v is not None)
        self._caption.setText(
            f"Billing periods · {len(labels)} · {preview.composers} composers"
            + (f" · {token_total:,} tokens" if token_total else "")
            + " · older → newer · click chips to toggle series"
        )

        def _latest(values: list[float | None]) -> float | None:
            return next((v for v in reversed(values) if v is not None), None)

        latest_composers = _latest(composers)
        if latest_composers is None:
            self._chip_composers.setText("— composers")
        else:
            self._chip_composers.setText(f"{int(latest_composers)} composers")

        latest_tokens = _latest(tokens)
        if latest_tokens is None:
            self._chip_tokens.setText("— tokens")
        else:
            self._chip_tokens.setText(_format_token_chip(int(latest_tokens)))

        latest_abort = _latest(abort)
        latest_tool = _latest(tool_err)
        latest_accept = _latest(accept)
        if latest_abort is None:
            self._chip_abort.setText("— abort")
        else:
            self._chip_abort.setText(f"{latest_abort:.0f}% abort")
        if latest_tool is None:
            self._chip_tool.setText("— tool err")
        else:
            self._chip_tool.setText(f"{latest_tool:.0f}% tool err")
        if latest_accept is None:
            self._chip_accept.setText("— accept")
        else:
            self._chip_accept.setText(f"{latest_accept:.0f}% accept")

        latest_auto = _latest(auto_pct)
        latest_api = _latest(api_pct)
        has_auto = any(v is not None for v in auto_pct)
        has_api = any(v is not None for v in api_pct)
        if has_auto:
            if latest_auto is None:
                self._chip_auto.setText("— AUTO")
            else:
                self._chip_auto.setText(f"{latest_auto:.0f}% AUTO")
            self._chip_auto.show()
        else:
            self._chip_auto.hide()
        if has_api:
            if latest_api is None:
                self._chip_api.setText("— API")
            else:
                self._chip_api.setText(f"{latest_api:.0f}% API")
            self._chip_api.show()
        else:
            self._chip_api.hide()

        series = [
            ChartSeries("Composers", "#A8C3A4", composers),
            ChartSeries("Tokens", "#E0C090", tokens, area=True),
            ChartSeries("Abort %", "#D0B56C", abort, unit="%"),
            ChartSeries("Tool error %", "#D9897A", tool_err, unit="%"),
            ChartSeries("Accept %", "#8BA4C7", accept, unit="%"),
        ]
        if has_auto:
            series.append(ChartSeries("AUTO %", "#6FA8DC", auto_pct, unit="%"))
        if has_api:
            series.append(ChartSeries("API %", "#B0B0B0", api_pct, unit="%"))

        self._chart.set_data(
            labels,
            series,
            weights=weights,
            caption="Hover a period for exact values · click chips to toggle series",
        )
        self._sync_series_chip_states()

    def _on_series_chip_toggled(self, series_name: str) -> None:
        visible = self._chart.toggle_series(series_name)
        for chip in self._series_chips:
            if chip.series_name == series_name:
                chip.set_series_on(visible)
                break

    def _sync_series_chip_states(self) -> None:
        for chip in self._series_chips:
            chip.set_series_on(self._chart.is_series_visible(chip.series_name))


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


class DraggablePanel(QFrame):
    """Content card that can be reordered by dragging anywhere on the panel."""

    def __init__(
        self,
        panel_id: str,
        body: QWidget,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.panel_id = panel_id
        self.body = body
        self._press_global: QPoint | None = None
        self.setObjectName("draggablePanel")
        self.setAcceptDrops(False)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("Drag to reorder panels")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(body)
        self.installEventFilter(self)
        body.installEventFilter(self)
        for child in body.findChildren(QWidget):
            child.installEventFilter(self)

    def _widget_blocks_drag(self, widget: QObject | None) -> bool:
        """Clicks on these controls should not start a panel reorder."""
        current = widget
        while isinstance(current, QObject):
            if isinstance(current, (CopyableCommand, CountdownLabel, SeriesToggleChip)):
                return True
            current = current.parent()
        return False

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
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        drag.exec(Qt.DropAction.MoveAction)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

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
        self.habits = AgentHabitsPreview()
        self.composer_history = ComposerHistoryPreview()

        self._panel_host = ReorderablePanelHost()
        self._spend_panel = DraggablePanel("spend", self._card)
        self._habits_panel = DraggablePanel("habits", self.habits)
        self._composer_panel = DraggablePanel("composer", self.composer_history)
        self._panel_host.add_panel(self._spend_panel)
        self._panel_host.add_panel(self._habits_panel)
        self._panel_host.add_panel(self._composer_panel)
        self._panel_host.set_order(load_popup_panel_order())
        self._panel_host.order_changed.connect(save_popup_panel_order)

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
        root.addWidget(self.browser_help)
        root.addWidget(self._panel_host)
        root.addWidget(self.countdown)
        root.addWidget(self.status)
        self.refresh_habits()

    def refresh_habits(self) -> None:
        """Reload local SDK + state.vscdb + usage-CSV habit stats (soft-fails)."""
        try:
            preview = collect_habits_preview()
        except Exception:  # noqa: BLE001 — popup must stay usable
            preview = SdkHabitsPreview.unavailable("Could not read local SDK stats")
        self.habits.apply_preview(preview)
        try:
            vscdb = collect_vscdb_habits_preview()
        except Exception:  # noqa: BLE001
            vscdb = VscdbHabitsPreview.unavailable("Could not read state.vscdb")
        try:
            usage = load_usage_preview()
        except Exception:  # noqa: BLE001
            usage = UsageCsvPreview.unavailable("Could not read usage CSV totals")
        self.composer_history.apply_preview(vscdb, usage)
        self.adjustSize()

    def show_at(self, pos) -> None:  # noqa: ANN001
        """Show near tray; close when focus leaves or user clicks elsewhere."""
        self._dismiss_armed = False
        self._arm_timer.stop()
        self.refresh_habits()
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
        # Usage CSV sync + AUTO/API associations run with the scrape.
        self.refresh_habits()

    def set_browser_inaccessible(self, inaccessible: bool, launch_command: str = "") -> None:
        """Show or hide the Browser inaccessible banner with a copyable launch command."""
        if inaccessible:
            self._spend_panel.hide()
            self.browser_help.set_launch_command(launch_command)
            self.browser_help.show()
            self.countdown.hide()
        else:
            self._spend_panel.show()
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
