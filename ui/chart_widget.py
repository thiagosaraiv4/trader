"""
ui/chart_widget.py
-------------------
ChartWidget: the primary OHLCV chart view.

Design contract:
- NEVER fetches or holds a reference to DataFeedManager or any Model class.
- Updates exclusively via Qt Slots wired by main.py.
- All rendering delegated to CandlestickGraphicsObject (custom_graphics.py).
- Crosshair implemented with InfiniteLines + scene mouse tracking.
- Header label updated on every slot call (symbol, OHLCV, cursor position).

PyQtGraph configuration:
- Background: dark (#121212)
- Grid: subtle horizontal only (Y axis reference lines)
- Axes: custom DateTimeAxisItem for X (Unix ts → human datetime string)
- antialiasing=False: candlesticks are axis-aligned; AA is cost without benefit
- setClipToView(True): only renders items intersecting the viewport

Zoom/pan behavior:
- Mouse wheel: X-axis zoom (natural for time-series)
- Right-click drag: Y-axis zoom
- Middle-click drag or left-drag on empty area: pan
- All provided natively by PyQtGraph ViewBox — no custom code needed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd
import pyqtgraph as pg
from PySide6.QtCore import QPointF, Qt, Slot
from PySide6.QtGui import QColor, QFont, QPen
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.types import BarData, Timeframe
from ui.custom_graphics import CandlestickGraphicsObject
from utils.logger import get_logger

log = get_logger(__name__, component="chart_widget")

# Maximum overlay data points to prevent memory growth in long sessions.
# When exceeded, oldest points are removed.
_MAX_OVERLAY_POINTS: int = 5000

# Overlay line colors — assigned by registration order
_OVERLAY_COLORS: list[str] = ["#2196F3", "#FFD600", "#FF5722", "#4CAF50", "#AB47BC", "#26C6DA"]


@dataclass
class _OverlayState:
    """Per-overlay mutable state. Uses Python lists for O(1) amortized append."""
    plot_item: pg.PlotDataItem
    x_data: list[float] = field(default_factory=list)   # Python list, NOT numpy
    y_data: list[float] = field(default_factory=list)   # Python list, NOT numpy


# ---------------------------------------------------------------------------
# Custom AxisItem: Unix timestamps → human-readable datetime
# ---------------------------------------------------------------------------

class DateTimeAxisItem(pg.AxisItem):
    """
    AxisItem subclass that formats X-axis tick values (Unix seconds) as
    human-readable datetime strings.

    Two-line format: "HH:MM" on top, "MM/DD" below — maximizes information
    density without truncation at common chart widths.
    """

    def tickStrings(
        self,
        values: list[float],
        scale: float,
        spacing: float,
    ) -> list[str]:
        result: list[str] = []
        for v in values:
            try:
                dt = datetime.fromtimestamp(v, tz=timezone.utc)
                result.append(dt.strftime("%H:%M\n%m/%d"))
            except (OSError, ValueError, OverflowError):
                result.append("")
        return result


# ---------------------------------------------------------------------------
# ChartWidget
# ---------------------------------------------------------------------------

class ChartWidget(QWidget):
    """
    Self-contained candlestick chart widget.

    External interface (signals to connect from Controller):
        Slot on_snapshot(df: pd.DataFrame)  — fires once on feed start
        Slot on_bar_update(bar: BarData)     — fires on each throttled tick

    The widget emits no signals of its own in Phase 1.
    In Phase 3, it will emit crosshair_moved(ts, price) for synchronizing
    multi-chart layouts.

    Layout:
        ┌─────────────────────────────────────────────┐
        │ [SYMBOL]  O:xxx  H:xxx  L:xxx  C:xxx  V:xxx │  ← header bar
        ├─────────────────────────────────────────────┤
        │                                             │
        │          PlotWidget (candlestick area)      │
        │                                             │
        └─────────────────────────────────────────────┘
    """

    def __init__(
        self,
        timeframe: Timeframe = Timeframe.M1,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._timeframe = timeframe
        self._overlays: dict[str, _OverlayState] = {}  # Phase 3: overlay registry
        self._log = get_logger(
            __name__,
            component="chart_widget",
            timeframe=timeframe.value,
        )
        self._setup_ui()
        self._setup_crosshair()
        self._setup_interactions()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ---- Header bar ----
        header = QWidget(self)
        header.setFixedHeight(30)
        header.setStyleSheet(
            "background: #1a1a1a;"
            "border-bottom: 1px solid #2a2a2a;"
        )
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 0, 10, 0)
        header_layout.setSpacing(0)

        mono = QFont("Consolas", 10)
        mono.setStyleHint(QFont.StyleHint.Monospace)

        self._lbl_symbol = QLabel("—", header)
        self._lbl_symbol.setFont(QFont("Consolas", 11))
        self._lbl_symbol.setStyleSheet(
            "color: #e8e8e8; font-weight: bold; padding-right: 12px;"
        )

        self._lbl_tf = QLabel(self._timeframe.value, header)
        self._lbl_tf.setFont(mono)
        self._lbl_tf.setStyleSheet(
            "color: #555; padding-right: 16px;"
        )

        self._lbl_ohlcv = QLabel("", header)
        self._lbl_ohlcv.setFont(mono)
        self._lbl_ohlcv.setTextFormat(Qt.TextFormat.RichText)
        self._lbl_ohlcv.setStyleSheet("color: #888;")

        self._lbl_cursor = QLabel("", header)
        self._lbl_cursor.setFont(mono)
        self._lbl_cursor.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._lbl_cursor.setStyleSheet("color: #555;")
        self._lbl_cursor.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )

        header_layout.addWidget(self._lbl_symbol)
        header_layout.addWidget(self._lbl_tf)
        header_layout.addWidget(self._lbl_ohlcv)
        header_layout.addStretch()
        header_layout.addWidget(self._lbl_cursor)

        # ---- Plot ----
        self._plot = pg.PlotWidget(
            background=QColor(18, 18, 18),
            axisItems={"bottom": DateTimeAxisItem(orientation="bottom")},
        )
        self._plot.setAntialiasing(False)
        self._plot.hideButtons()
        self._plot.setMenuEnabled(False)
        self._plot.setClipToView(True)
        self._plot.showGrid(x=False, y=True, alpha=0.12)

        # Axis styling
        tick_style: dict = {
            "tickLength": -5,
            "stopAxisAtTick": (True, True),
        }
        axis_pen = QPen(QColor("#333"))
        tick_font = QFont("Consolas", 8)

        for axis_name in ("left", "bottom"):
            ax = self._plot.getAxis(axis_name)
            ax.setPen(axis_pen)
            ax.setStyle(**tick_style)
            ax.setTickFont(tick_font)
            ax.setTextPen(QPen(QColor("#666")))

        # Candlestick item
        bar_width_sec = self._timeframe.to_seconds()
        self._candles = CandlestickGraphicsObject(bar_width_sec=float(bar_width_sec))
        self._plot.addItem(self._candles)

        root_layout.addWidget(header)
        root_layout.addWidget(self._plot, stretch=1)

    def _setup_crosshair(self) -> None:
        """
        Attach crosshair InfiniteLines to the plot.

        Using ``ignoreBounds=True`` ensures crosshairs don't affect autoRange.
        DashLine style chosen for minimal visual interference with price action.
        """
        crosshair_pen = QPen(QColor(110, 110, 110, 160), 1, Qt.PenStyle.DashLine)

        self._vline = pg.InfiniteLine(
            angle=90, movable=False, pen=crosshair_pen
        )
        self._hline = pg.InfiniteLine(
            angle=0, movable=False, pen=crosshair_pen
        )
        self._plot.addItem(self._vline, ignoreBounds=True)
        self._plot.addItem(self._hline, ignoreBounds=True)

    def _setup_interactions(self) -> None:
        """Wire mouse movement to crosshair updates via scene signal."""
        self._plot.scene().sigMouseMoved.connect(self._on_mouse_moved)

        # Configure ViewBox for time-series interaction
        vb: pg.ViewBox = self._plot.plotItem.vb
        vb.setMouseMode(pg.ViewBox.RectMode)
        # Enable mouse interaction on both axes for zoom/pan
        vb.setMouseEnabled(x=True, y=True)

    # ------------------------------------------------------------------
    # Public Slots (wired by main.py from Controller signals)
    # ------------------------------------------------------------------

    @Slot(object)
    def on_snapshot(self, df: pd.DataFrame) -> None:
        """
        Load initial historical data.

        Triggers full history draw (O(n) — happens once).
        Calls autoRange() to fit all bars in view.
        """
        if df.empty:
            self._log.warning("Empty snapshot received — skipping")
            return

        self._log.debug("Snapshot received", extra={"bars": len(df)})
        self._candles.set_historical_data(df)
        self._plot.autoRange()

        # Update OHLCV label with last closed bar
        last = df.iloc[-1]
        self._refresh_ohlcv_label(
            o=float(last["open"]),
            h=float(last["high"]),
            l=float(last["low"]),
            c=float(last["close"]),
            v=float(last["volume"]),
        )

    @Slot(object)
    def on_bar_update(self, bar: BarData) -> None:
        """
        Incremental live update. Delegates to CandlestickGraphicsObject
        which determines whether to update the history or live layer.

        This slot is throttled at the Controller level — called at most
        once per ``bar_throttle_ms``.
        """
        self._candles.update_bar(bar)
        self._refresh_ohlcv_label(
            o=float(bar.open),
            h=float(bar.high),
            l=float(bar.low),
            c=float(bar.close),
            v=float(bar.volume),
        )

    def set_symbol_label(self, symbol: str) -> None:
        """Update the symbol label in the header. Called by Controller signal."""
        self._lbl_symbol.setText(symbol)

    # ------------------------------------------------------------------
    # Phase 3: Overlay Slots (indicator lines on chart)
    # ------------------------------------------------------------------

    @Slot(str, object, object)
    def on_overlay_snapshot(
        self,
        name: str,
        x_data: list[float],
        y_data: list[float],
    ) -> None:
        """
        Create or replace an overlay line from full historical data.

        Called once per indicator on snapshot. Stores data as Python lists
        (NOT numpy arrays) for O(1) amortized append in on_overlay_point.

        Args:
            name: Indicator name (e.g. 'SMA_20').
            x_data: X coordinates (epoch seconds) as Python list.
            y_data: Y coordinates (indicator values) as Python list.
        """
        # Remove existing overlay if re-initializing
        if name in self._overlays:
            old = self._overlays[name]
            self._plot.removeItem(old.plot_item)
            del self._overlays[name]

        # Assign color by order of creation
        color_idx = len(self._overlays) % len(_OVERLAY_COLORS)
        color = _OVERLAY_COLORS[color_idx]
        pen = pg.mkPen(color=color, width=1.5)

        # Create PlotDataItem with connect='finite' to skip NaN gaps
        item = pg.PlotDataItem(
            x_data, y_data,
            pen=pen,
            connect="finite",
            name=name,
        )
        self._plot.addItem(item)

        # Store as Python lists (copy to decouple from caller)
        state = _OverlayState(
            plot_item=item,
            x_data=list(x_data),
            y_data=list(y_data),
        )
        self._overlays[name] = state

        self._log.debug(
            "Overlay snapshot loaded",
            extra={"indicator": name, "points": len(x_data)},
        )

    @Slot(str, object, float)
    def on_overlay_point(
        self,
        name: str,
        timestamp: datetime,
        value: float,
    ) -> None:
        """
        Append a single point to an existing overlay line.

        O(1) amortized: Python list.append + setData.
        Enforces MAX_OVERLAY_POINTS to prevent unbounded memory growth.

        Args:
            name: Indicator name (must match a prior on_overlay_snapshot call).
            timestamp: Bar timestamp (datetime, converted to epoch seconds).
            value: New indicator value.
        """
        state = self._overlays.get(name)
        if state is None:
            return  # Overlay not yet initialized (edge case)

        ts = timestamp.timestamp()
        state.x_data.append(ts)
        state.y_data.append(value)

        # Enforce memory limit
        n = len(state.x_data)
        if n > _MAX_OVERLAY_POINTS:
            excess = n - _MAX_OVERLAY_POINTS
            del state.x_data[:excess]
            del state.y_data[:excess]

        state.plot_item.setData(state.x_data, state.y_data)

    # ------------------------------------------------------------------
    # Internal: crosshair and labels
    # ------------------------------------------------------------------

    @Slot(object)
    def _on_mouse_moved(self, scene_pos: QPointF) -> None:
        """
        Update crosshair position and cursor info label.

        Maps scene coordinates → view coordinates via ViewBox.
        Skips update if cursor is outside the plot bounding rect.
        """
        if not self._plot.sceneBoundingRect().contains(scene_pos):
            return

        vb: pg.ViewBox = self._plot.plotItem.vb
        view_pos: QPointF = vb.mapSceneToView(scene_pos)
        x, y = view_pos.x(), view_pos.y()

        self._vline.setPos(x)
        self._hline.setPos(y)

        try:
            dt = datetime.fromtimestamp(x, tz=timezone.utc)
            ts_str = dt.strftime("%Y-%m-%d %H:%M UTC")
        except (OSError, ValueError, OverflowError):
            ts_str = "—"

        self._lbl_cursor.setText(f"{ts_str}  |  {y:>12,.2f}")

    def _refresh_ohlcv_label(
        self,
        o: float,
        h: float,
        l: float,
        c: float,
        v: float,
    ) -> None:
        """
        Render OHLCV values as colored HTML in the header label.

        Color reflects bar direction: green for bullish, red for bearish.
        Uses RichText with inline styles (no stylesheet class lookups —
        faster for high-frequency updates).
        """
        color = "#00c864" if c >= o else "#dc3c3c"
        change_pct = ((c - o) / o * 100.0) if o != 0.0 else 0.0
        sign = "+" if change_pct >= 0 else ""

        html = (
            f"<span style='color:{color};'>"
            f"O:{o:,.2f}  H:{h:,.2f}  L:{l:,.2f}  C:{c:,.2f}"
            f"</span>"
            f"<span style='color:#555;'>  V:{v:.4f}"
            f"  ({sign}{change_pct:.2f}%)</span>"
        )
        self._lbl_ohlcv.setText(html)
