"""
ui/backtest_result_widget.py
-----------------------------
Widget for displaying backtest results and equity curve.
"""
from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (
    QGridLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
    QFrame,
)

from core.backtest_engine import BacktestResult


class BacktestResultWidget(QWidget):
    """
    Displays static metrics and an equity curve for a completed backtest.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)

        # -------------------------------------------------------------
        # Metrics Grid
        # -------------------------------------------------------------
        metrics_frame = QFrame(self)
        metrics_frame.setStyleSheet(
            "QFrame {"
            "  background: #1a1a1a;"
            "  border: 1px solid #2a2a2a;"
            "  border-radius: 4px;"
            "}"
            "QLabel {"
            "  color: #d0d0d0;"
            "  border: none;"
            "}"
        )
        grid = QGridLayout(metrics_frame)
        grid.setContentsMargins(8, 8, 8, 8)
        grid.setSpacing(8)

        self._labels: dict[str, QLabel] = {}

        def _add_metric(row: int, col: int, key: str, label_text: str):
            title = QLabel(f"<b>{label_text}</b>")
            title.setStyleSheet("color: #888; font-size: 10px;")
            value = QLabel("—")
            value.setStyleSheet("font-size: 12px; font-weight: bold;")
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            
            grid.addWidget(title, row * 2, col)
            grid.addWidget(value, row * 2 + 1, col)
            self._labels[key] = value

        # Row 0
        _add_metric(0, 0, "return", "Total Return")
        _add_metric(0, 1, "profit_factor", "Profit Factor")
        _add_metric(0, 2, "sharpe", "Sharpe Ratio")
        _add_metric(0, 3, "calmar", "Calmar Ratio")
        
        # Row 1
        _add_metric(1, 0, "max_drawdown", "Max Drawdown")
        _add_metric(1, 1, "win_rate", "Win Rate")
        _add_metric(1, 2, "avg_winner", "Avg Winner")
        _add_metric(1, 3, "avg_loser", "Avg Loser")

        # Row 2
        _add_metric(2, 0, "stops_hit", "Stops Hit")
        _add_metric(2, 1, "trail_exits", "Trailing Exits")
        _add_metric(2, 2, "momentum_exits", "Momentum Exits")
        _add_metric(2, 3, "trades_month", "Trades/Month")

        layout.addWidget(metrics_frame)

        # -------------------------------------------------------------
        # Equity Curve Chart
        # -------------------------------------------------------------
        self._plot = pg.PlotWidget(background=QColor(18, 18, 18))
        self._plot.setAntialiasing(True)
        self._plot.hideButtons()
        self._plot.setMenuEnabled(False)
        self._plot.showGrid(x=False, y=True, alpha=0.15)

        # Styling
        axis_pen = pg.mkPen(color="#333")
        tick_font = QFont("Consolas", 8)
        for ax_name in ("left", "bottom"):
            ax = self._plot.getAxis(ax_name)
            ax.setPen(axis_pen)
            ax.setStyle(tickLength=-5, stopAxisAtTick=(True, True))
            ax.setTickFont(tick_font)
            ax.setTextPen(pg.mkPen(color="#666"))

        # We don't use DateTimeAxisItem here since equity_curve is just a list of floats (points), 
        # but if we wanted timestamps, we would need to pass x_axis as timestamps.
        # For simplicity, we plot index vs equity.
        
        layout.addWidget(self._plot, stretch=1)

    @Slot(object)
    def update_result(self, result: BacktestResult) -> None:
        """Update metrics and draw equity curve when backtest completes."""
        if not result:
            return

        # Update metrics
        self._labels["return"].setText(f"{result.total_return_pct:.2%}")
        self._labels["return"].setStyleSheet(
            f"color: {'#00c864' if result.total_return_pct >= 0 else '#dc3c3c'}; font-weight: bold; font-size: 12px;"
        )
        
        self._labels["profit_factor"].setText(f"{result.profit_factor:.2f}")
        self._labels["sharpe"].setText(f"{result.sharpe_ratio:.2f}")
        self._labels["calmar"].setText(f"{result.calmar_ratio:.2f}")
        self._labels["max_drawdown"].setText(f"{result.max_drawdown:.2%}")
        self._labels["win_rate"].setText(f"{result.win_rate:.1%}")
        self._labels["avg_winner"].setText(f"{result.avg_winner_pct:.2%}")
        self._labels["avg_loser"].setText(f"{result.avg_loser_pct:.2%}")
        
        self._labels["stops_hit"].setText(str(result.stops_hit_count))
        self._labels["trail_exits"].setText(str(result.trailing_exits_count))
        self._labels["momentum_exits"].setText(str(result.momentum_exits_count))
        self._labels["trades_month"].setText(f"{result.trades_per_month_avg:.1f}")

        if result.low_sample_warning:
            self._labels["trades_month"].setStyleSheet("color: #d4881a; font-weight: bold; font-size: 12px;")

        # Update equity curve
        self._plot.clear()
        
        if result.equity_curve:
            pen = pg.mkPen(color="#2196F3", width=2)
            self._plot.plot(result.equity_curve, pen=pen)
            self._plot.autoRange()
