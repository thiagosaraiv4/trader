"""
ui/custom_graphics.py
----------------------
CandlestickGraphicsObject: high-performance PyQtGraph candle renderer.

Rendering architecture:
    Two-layer QPicture display list strategy:

    Layer 1 — _history_pic (QPicture):
        Contains ALL closed bars recorded as a display list.
        Rebuilt when: initial snapshot loaded, or a new bar closes.
        Cost: O(n) — but infrequent (once per candle period).
        QPainter.drawRect/drawLine calls are recorded, not executed.

    Layer 2 — _live_pic (QPicture):
        Contains ONLY the current forming bar.
        Rebuilt on every throttled tick update (O(1)).
        Semi-transparent to indicate it's still forming.

    paint() → QPicture.play() → O(1) regardless of bar count.
    Qt's raster paint engine replays the recorded commands via memcpy-like
    display list execution — significantly faster than per-frame QPainter calls.

boundingRect caching:
    Qt calls boundingRect() on EVERY frame for view-frustum culling.
    We cache a QRectF and mark it dirty only when bars extend the range.
    Recompute cost: O(1) via numpy min/max on pre-existing arrays.

Coordinate system:
    X: Unix epoch seconds (float) — aligns with DateTimeAxisItem
    Y: Price (float)

Optimization notes:
    - Pen/brush state changes are batched: wicks drawn in one pass,
      bull bodies in one pass, bear bodies in one pass.
      QPainter state changes (setPen/setBrush) are the dominant perf cost
      for large bar counts — batching reduces O(3n) state changes to O(3).
    - numpy vectorized operations for coordinate extraction (no Python loops
      over DataFrame rows).
    - antialiasing disabled: candlesticks are always axis-aligned rectangles;
      AA adds cost with no visual benefit.
"""
from __future__ import annotations


import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6.QtCore import QLineF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QPainter,
    QPen,
    QPicture,
)
from PySide6.QtWidgets import QStyleOptionGraphicsItem, QWidget

from core.types import BarData
from utils.logger import get_logger

log = get_logger(__name__, component="candlestick_gfx")

# Epoch reference for DatetimeIndex → seconds conversion (pandas 3.x safe)
_EPOCH = pd.Timestamp("1970-01-01", tz="UTC")

# ---------------------------------------------------------------------------
# Visual constants
# ---------------------------------------------------------------------------

_BULL_COLOR   = QColor(0, 200, 100)      # Close >= Open
_BEAR_COLOR   = QColor(220, 60, 60)      # Close < Open
_WICK_COLOR   = QColor(150, 150, 150)    # High/Low wicks
_LIVE_ALPHA   = 170                      # Alpha for the forming candle body

_BODY_WIDTH_RATIO: float = 0.72          # Body width as fraction of bar spacing
_MIN_BODY_HEIGHT: float  = 0.0001        # Doji guard: prevents zero-height rect


# ---------------------------------------------------------------------------
# Pre-built QPen/QBrush constants (created once, reused across redraws)
# ---------------------------------------------------------------------------

def _make_pen(color: QColor, width: float = 1.0) -> QPen:
    pen = QPen(color, width, Qt.PenStyle.SolidLine)
    pen.setCosmetic(True)   # Width in screen pixels, not scene units
    return pen


def _make_brush(color: QColor, alpha: int = 255) -> QBrush:
    c = QColor(color)
    c.setAlpha(alpha)
    return QBrush(c)


_PEN_WICK      = _make_pen(_WICK_COLOR)
_PEN_BULL      = _make_pen(_BULL_COLOR)
_PEN_BEAR      = _make_pen(_BEAR_COLOR)
_BRUSH_BULL    = _make_brush(_BULL_COLOR)
_BRUSH_BEAR    = _make_brush(_BEAR_COLOR)
_BRUSH_BULL_LV = _make_brush(_BULL_COLOR, _LIVE_ALPHA)
_BRUSH_BEAR_LV = _make_brush(_BEAR_COLOR, _LIVE_ALPHA)
_BRUSH_NONE    = QBrush(Qt.BrushStyle.NoBrush)


# ---------------------------------------------------------------------------
# CandlestickGraphicsObject
# ---------------------------------------------------------------------------

class CandlestickGraphicsObject(pg.GraphicsObject):
    """
    Candlestick chart item for PyQtGraph using QPicture display list caching.

    Public interface (all called from ChartWidget slots — no direct data access):
        set_historical_data(df)  — load snapshot, rebuild full history picture
        update_bar(bar)          — incremental update (closed or live bar)

    Internal render contract:
        paint()      — replays _history_pic + _live_pic, O(1) cost
        boundingRect() — returns cached QRectF, recomputed only when dirty
    """

    def __init__(self, bar_width_sec: float = 60.0) -> None:
        super().__init__()

        self._bar_width_sec: float = bar_width_sec
        self._half_body_w: float   = bar_width_sec * _BODY_WIDTH_RATIO / 2.0

        # QPicture display list caches
        self._history_pic: QPicture = QPicture()
        self._live_pic:    QPicture = QPicture()

        # Closed bar DataFrame — canonical source of truth
        self._df: pd.DataFrame = pd.DataFrame(
            columns=["open", "high", "low", "close", "volume"]
        )

        # Current forming bar (None when no live data yet)
        self._live_bar: BarData | None = None

        # Bounds cache — recomputed only when _bounds_dirty is True
        self._bounds: QRectF     = QRectF(0.0, 0.0, 1.0, 1.0)
        self._bounds_dirty: bool = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def set_historical_data(self, df: pd.DataFrame) -> None:
        """
        Load the full historical DataFrame and rebuild the history picture.

        Called once on snapshot. Triggers full O(n) redraw of all bars,
        then the item never redraws them again (just replays the QPicture).

        Args:
            df: Canonical OHLCV DataFrame (UTC DatetimeIndex, float64 columns).
        """
        self._df = df.copy()
        self._live_bar = None
        self._live_pic = QPicture()
        self._bounds_dirty = True

        self.prepareGeometryChange()
        self._rebuild_history_picture()
        self.update()

        log.debug("Historical data loaded", extra={"bars": len(df)})

    def update_bar(self, bar: BarData) -> None:
        """
        Incorporate a new or updated bar.

        Decision tree:
        - If bar.is_closed AND timestamp is new → append to history, rebuild
          history picture (O(n), but rare — happens once per candle period).
        - If bar.is_closed AND timestamp exists → update row in-place, rebuild
          history picture.
        - If bar.is_live → rebuild live picture only (O(1), frequent path).

        Args:
            bar: New BarData from DataFeedManager (may be closed or forming).
        """
        if bar.is_closed:
            self._merge_closed_bar(bar)
        else:
            self._update_live_bar(bar)

        self.update()

    # ------------------------------------------------------------------
    # pg.GraphicsObject protocol
    # ------------------------------------------------------------------

    def paint(
        self,
        painter: QPainter,
        option: "QStyleOptionGraphicsItem",
        widget: "QWidget | None" = None,
    ) -> None:
        """
        Replay both QPicture display lists.

        Cost: O(1) — QPicture.play() executes pre-recorded draw commands
        via Qt's internal display list engine, independent of bar count.
        """
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        self._history_pic.play(painter)
        self._live_pic.play(painter)

    def boundingRect(self) -> QRectF:
        """
        Return cached bounding rect. Recomputed only when dirty.

        Qt calls this on EVERY frame for view-frustum culling and layout.
        Must be O(1) — the dirty flag ensures recompute is rare.
        """
        if self._bounds_dirty:
            self._bounds = self._compute_bounds()
            self._bounds_dirty = False
        return self._bounds

    # ------------------------------------------------------------------
    # Internal: bar merging
    # ------------------------------------------------------------------

    def _merge_closed_bar(self, bar: BarData) -> None:
        """Append or update a closed bar in the DataFrame, then rebuild history."""
        ts = bar.timestamp
        row_data = {
            "open":   float(bar.open),
            "high":   float(bar.high),
            "low":    float(bar.low),
            "close":  float(bar.close),
            "volume": float(bar.volume),
        }

        if ts in self._df.index:
            for col, val in row_data.items():
                self._df.loc[ts, col] = val
        else:
            new_row = pd.DataFrame(
                [list(row_data.values())],
                index=pd.DatetimeIndex([ts], tz="UTC"),
                columns=list(row_data.keys()),
                dtype=np.float64,
            )
            self._df = pd.concat([self._df, new_row])

        # Clear live bar — the forming candle has now closed
        self._live_bar = None
        self._live_pic = QPicture()
        self._bounds_dirty = True

        self.prepareGeometryChange()
        self._rebuild_history_picture()

    def _update_live_bar(self, bar: BarData) -> None:
        """Update the live (forming) bar without touching history."""
        prev = self._live_bar
        self._live_bar = bar

        # Expand bounds only if the live bar extends the current range
        if prev is None or (
            float(bar.high) > self._bounds.top() + self._bounds.height()
            or float(bar.low) < self._bounds.top()
        ):
            self._bounds_dirty = True
            self.prepareGeometryChange()

        self._rebuild_live_picture()

    # ------------------------------------------------------------------
    # Internal: QPicture builders
    # ------------------------------------------------------------------

    def _rebuild_history_picture(self) -> None:
        """
        Record all closed bars into a fresh QPicture display list.

        Uses numpy vectorized extraction to avoid Python-level DataFrame
        iteration. Pen/brush state changes are batched to minimize
        QPainter state transitions (dominant cost for large bar counts).
        """
        pic = QPicture()

        if self._df.empty:
            self._history_pic = pic
            return

        painter = QPainter(pic)
        try:
            self._draw_bars_vectorized(painter, self._df)
        finally:
            painter.end()

        self._history_pic = pic

    def _rebuild_live_picture(self) -> None:
        """Record the current forming bar into a fresh QPicture display list."""
        pic = QPicture()

        if self._live_bar is None:
            self._live_pic = pic
            return

        painter = QPainter(pic)
        try:
            bar = self._live_bar
            x = bar.timestamp.timestamp()
            self._draw_single_candle(
                painter,
                x=x,
                o=float(bar.open),
                h=float(bar.high),
                l=float(bar.low),
                c=float(bar.close),
                is_live=True,
            )
        finally:
            painter.end()

        self._live_pic = pic

    # ------------------------------------------------------------------
    # Internal: drawing primitives
    # ------------------------------------------------------------------

    def _draw_bars_vectorized(self, painter: QPainter, df: pd.DataFrame) -> None:
        """
        Draw all bars with minimal pen/brush state changes.

        Pass 1: All wicks (one pen state for all bars).
        Pass 2: All bull bodies (one pen+brush state).
        Pass 3: All bear bodies (one pen+brush state).

        Total state changes: 6 (setPen×3, setBrush×3) regardless of bar count.
        Contrast with naive loop: 4n state changes.
        """
        # Extract arrays — pandas-native epoch conversion (safe for tz-aware index)
        ts_sec: np.ndarray  = (df.index - _EPOCH).total_seconds().to_numpy(dtype=np.float64)
        opens:  np.ndarray  = df["open"].to_numpy(dtype=np.float64)
        highs:  np.ndarray  = df["high"].to_numpy(dtype=np.float64)
        lows:   np.ndarray  = df["low"].to_numpy(dtype=np.float64)
        closes: np.ndarray  = df["close"].to_numpy(dtype=np.float64)

        hw = self._half_body_w

        # --- Pass 1: Wicks (all bars, single pen) ---
        painter.setPen(_PEN_WICK)
        painter.setBrush(_BRUSH_NONE)
        for i in range(len(df)):
            x = ts_sec[i]
            painter.drawLine(QLineF(x, highs[i], x, lows[i]))

        # --- Pass 2: Bull bodies (close >= open) ---
        bull_mask: np.ndarray = closes >= opens
        if bull_mask.any():
            painter.setPen(_PEN_BULL)
            painter.setBrush(_BRUSH_BULL)
            for i in np.where(bull_mask)[0]:
                x = ts_sec[i]
                painter.drawRect(QRectF(
                    x - hw,
                    opens[i],
                    hw * 2,
                    max(closes[i] - opens[i], _MIN_BODY_HEIGHT),
                ))

        # --- Pass 3: Bear bodies (close < open) ---
        bear_mask: np.ndarray = closes < opens
        if bear_mask.any():
            painter.setPen(_PEN_BEAR)
            painter.setBrush(_BRUSH_BEAR)
            for i in np.where(bear_mask)[0]:
                x = ts_sec[i]
                painter.drawRect(QRectF(
                    x - hw,
                    closes[i],
                    hw * 2,
                    max(opens[i] - closes[i], _MIN_BODY_HEIGHT),
                ))

    def _draw_single_candle(
        self,
        painter: QPainter,
        x: float,
        o: float,
        h: float,
        l: float,
        c: float,
        is_live: bool = False,
    ) -> None:
        """
        Draw one candlestick. Used exclusively for the live (forming) bar.
        Live bar uses a reduced alpha to signal it's still forming.
        """
        is_bull = c >= o
        hw = self._half_body_w

        # Wick
        painter.setPen(_PEN_WICK)
        painter.setBrush(_BRUSH_NONE)
        painter.drawLine(QLineF(x, h, x, l))

        # Body
        if is_bull:
            painter.setPen(_PEN_BULL)
            painter.setBrush(_BRUSH_BULL_LV if is_live else _BRUSH_BULL)
            body_y   = o
            body_h   = max(c - o, _MIN_BODY_HEIGHT)
        else:
            painter.setPen(_PEN_BEAR)
            painter.setBrush(_BRUSH_BEAR_LV if is_live else _BRUSH_BEAR)
            body_y   = c
            body_h   = max(o - c, _MIN_BODY_HEIGHT)

        painter.drawRect(QRectF(x - hw, body_y, hw * 2, body_h))

    # ------------------------------------------------------------------
    # Internal: bounds computation
    # ------------------------------------------------------------------

    def _compute_bounds(self) -> QRectF:
        """
        Compute the bounding rect that encloses all bars (history + live).

        Uses numpy min/max for O(1) array ops — DataFrame column arrays
        are contiguous float64, so this is cache-friendly.
        Adds a 5% vertical margin to prevent data clipping at chart edge.
        """
        has_history = not self._df.empty
        has_live    = self._live_bar is not None

        if not has_history and not has_live:
            return QRectF(0.0, 0.0, 1.0, 1.0)

        # X bounds from history
        if has_history:
            ts_sec = (self._df.index - _EPOCH).total_seconds().to_numpy(dtype=np.float64)
            x_min  = float(ts_sec[0])  - self._half_body_w
            x_max  = float(ts_sec[-1]) + self._half_body_w * 2.0
            y_low  = float(self._df["low"].min())
            y_high = float(self._df["high"].max())
        else:
            # Only live bar exists
            x = self._live_bar.timestamp.timestamp()  # type: ignore[union-attr]
            x_min  = x - self._half_body_w
            x_max  = x + self._half_body_w * 2.0
            y_low  = float(self._live_bar.low)        # type: ignore[union-attr]
            y_high = float(self._live_bar.high)        # type: ignore[union-attr]

        # Extend Y bounds to include live bar
        if has_live:
            y_low  = min(y_low,  float(self._live_bar.low))   # type: ignore[union-attr]
            y_high = max(y_high, float(self._live_bar.high))  # type: ignore[union-attr]

        y_range  = y_high - y_low
        y_margin = y_range * 0.05 if y_range > 0 else abs(y_high) * 0.01 or 1.0

        return QRectF(
            x_min,
            y_low  - y_margin,
            x_max  - x_min,
            y_range + 2.0 * y_margin,
        )
