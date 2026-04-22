"""
ui/performance_widget.py
-------------------------
Painel de analytics em tempo real para a sessão em andamento.
Substitui o BacktestResultWidget durante o Live Trading.
- Painel Esquerdo: Métricas chave (Sharpe, Calmar, Drawdown, Streaks)
- Painel Direito: Curva de patrimônio dinâmica em O(1)
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from core.performance_engine import PerformanceSnapshot


class PerformanceWidget(QWidget):
    def __init__(self, initial_balance: float = 10_000.0, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._initial_balance = initial_balance
        self._setup_ui()

    def _setup_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(6, 6, 6, 6)
        
        # Style do Painel
        self.setStyleSheet("""
            QWidget { background-color: #121212; color: #f0f0f0; }
            QFrame { background-color: #1e1e1e; border: 1px solid #333; border-radius: 6px; }
            QLabel { border: none; background: transparent; }
            .MetricValue { font-size: 14px; font-weight: bold; font-family: Consolas; }
            .MetricDesc { font-size: 11px; color: #888; font-family: Consolas; }
            .Badge { padding: 3px 6px; border-radius: 4px; font-weight: bold; font-family: Consolas; font-size: 11px; }
        """)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter)

        # -------------------------------------------------------------
        # Left Panel (Metrics)
        # -------------------------------------------------------------
        metrics_frame = QFrame()
        metrics_layout = QVBoxLayout(metrics_frame)
        
        title_lbl = QLabel("LIVE ANALYTICS")
        title_lbl.setStyleSheet("font-weight: bold; font-size: 13px; color: #aaa; margin-bottom: 8px;")
        metrics_layout.addWidget(title_lbl)

        grid = QGridLayout()
        grid.setHorizontalSpacing(15)
        grid.setVerticalSpacing(10)
        
        self._lbl_winrate = QLabel("—")
        self._lbl_winrate.setProperty("class", "MetricValue")
        self._progress_winrate = QProgressBar()
        self._progress_winrate.setTextVisible(False)
        self._progress_winrate.setFixedHeight(6)

        self._lbl_profit_factor = QLabel("—")
        self._lbl_profit_factor.setProperty("class", "MetricValue")
        
        self._lbl_sharpe = QLabel("—")
        self._lbl_sharpe.setProperty("class", "MetricValue")
        
        self._lbl_calmar = QLabel("—")
        self._lbl_calmar.setProperty("class", "MetricValue")
        
        self._lbl_max_dd = QLabel("—")
        self._lbl_max_dd.setProperty("class", "MetricValue")
        self._lbl_max_dd.setStyleSheet("color: #ff5252;")
        
        self._lbl_streak = QLabel("0")
        self._lbl_streak.setProperty("class", "Badge")
        self._lbl_streak.setStyleSheet("background: #555; color: white;")
        self._lbl_streak.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._lbl_session_ret = QLabel("—")
        self._lbl_session_ret.setProperty("class", "MetricValue")

        # Layout building helper
        row = 0
        def add_metric(label_text: str, value_widget: QWidget):
            nonlocal row
            desc = QLabel(label_text)
            desc.setProperty("class", "MetricDesc")
            grid.addWidget(desc, row, 0)
            grid.addWidget(value_widget, row, 1, Qt.AlignmentFlag.AlignRight)
            row += 1

        # Winrate composition (Label + Bar)
        wr_widget = QWidget()
        wr_layout = QVBoxLayout(wr_widget)
        wr_layout.setContentsMargins(0,0,0,0)
        wr_layout.setSpacing(2)
        self._lbl_winrate.setAlignment(Qt.AlignmentFlag.AlignRight)
        wr_layout.addWidget(self._lbl_winrate)
        wr_layout.addWidget(self._progress_winrate)

        add_metric("Win Rate", wr_widget)
        add_metric("Profit Factor", self._lbl_profit_factor)
        add_metric("Sharpe Ratio", self._lbl_sharpe)
        add_metric("Calmar Ratio", self._lbl_calmar)
        add_metric("Max Drawdown", self._lbl_max_dd)
        add_metric("Current Streak", self._lbl_streak)
        add_metric("Session Return", self._lbl_session_ret)

        metrics_layout.addLayout(grid)
        metrics_layout.addStretch()
        
        splitter.addWidget(metrics_frame)

        # -------------------------------------------------------------
        # Right Panel (Equity Curve Plot)
        # -------------------------------------------------------------
        plot_frame = QFrame()
        plot_layout = QVBoxLayout(plot_frame)
        plot_layout.setContentsMargins(0, 0, 0, 0)
        
        self._plot_widget = pg.PlotWidget()
        self._plot_widget.setBackground('#1e1e1e')
        self._plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self._plot_widget.setLabel("left", "Equity", units="USD")
        
        # The InfiniteLine
        self._baseline = pg.InfiniteLine(angle=0, pen=pg.mkPen(color='#888', style=Qt.PenStyle.DashLine))
        self._baseline.setValue(self._initial_balance)
        self._plot_widget.addItem(self._baseline)

        # The Curve Item
        self._curve_item = self._plot_widget.plot(
            pen=pg.mkPen(color='#00e676', width=2),
            antialias=True
        )

        plot_layout.addWidget(self._plot_widget)
        splitter.addWidget(plot_frame)
        
        # Proportions
        splitter.setSizes([200, 600])

    @Slot(object)
    def on_performance_update(self, snapshot: PerformanceSnapshot) -> None:
        """Chamado unicamente quando um trade é fechado; operamos atualizações O(1)."""
        # --- Update Left Panel ---
        
        # Win Rate
        wr_pct_value = snapshot.win_rate * 100
        self._progress_winrate.setValue(min(max(int(wr_pct_value), 0), 100))
        self._lbl_winrate.setText(f"{wr_pct_value:.1f}%")
        if wr_pct_value > 55.0:
            bar_color = "#00e676"  # green
        elif wr_pct_value >= 45.0:
            bar_color = "#ffeb3b"  # yellow
        else:
            bar_color = "#ff5252"  # red
        self._progress_winrate.setStyleSheet(
            f"QProgressBar {{ border: none; background-color: #333; }}"
            f"QProgressBar::chunk {{ background-color: {bar_color}; }}"
        )
        
        # Profit Factor
        pf = snapshot.profit_factor
        if pf == float('inf'):
            pf_str = "INF"
        else:
            pf_str = f"{pf:.2f}"
            
        self._lbl_profit_factor.setText(pf_str)
        if pf > 1.0:
            self._lbl_profit_factor.setStyleSheet("color: #00e676;")
        else:
            self._lbl_profit_factor.setStyleSheet("color: #ff5252;")
            
        # Sharpe & Calmar
        self._lbl_sharpe.setText(f"{snapshot.sharpe:.2f}" if snapshot.sharpe is not None else "—")
        self._lbl_calmar.setText(f"{snapshot.calmar:.2f}" if snapshot.calmar is not None else "—")
        
        # Drawdown
        self._lbl_max_dd.setText(f"{snapshot.max_drawdown_pct:.2%}")
        
        # Streak
        streak = snapshot.current_streak
        if streak > 0:
            self._lbl_streak.setText(f"+{streak}")
            self._lbl_streak.setStyleSheet("background: rgba(0, 230, 118, 0.2); color: #00e676;")
        elif streak < 0:
            self._lbl_streak.setText(str(streak))
            self._lbl_streak.setStyleSheet("background: rgba(255, 82, 82, 0.2); color: #ff5252;")
        else:
            self._lbl_streak.setText("0")
            self._lbl_streak.setStyleSheet("background: #555; color: white;")
            
        # Session Return
        sret = snapshot.session_return_pct
        self._lbl_session_ret.setText(f"{sret:+.2%}")
        self._lbl_session_ret.setStyleSheet(
            "color: #00e676;" if sret >= 0 else "color: #ff5252;"
        )

        # --- Update Right Panel (Equity Curve) ---
        if snapshot.equity_values:
            # We convert to np.array because pyqtgraph handles numpy vastly faster.
            ts_array = np.array(snapshot.equity_timestamps, dtype=float)
            eq_array = np.array(snapshot.equity_values, dtype=float)
            
            # Subtraindo o tempo inicial do array (opcional, p/ o x-axis ser relativo ou absoluto)
            # Para deixar legível (já que timestamps brutos são grandes), vamos plotar index se o x n for trivial, 
            # ou apenas o timestamp normal. O pyqtgraph cuida bem disso.
            
            self._curve_item.setData(x=ts_array, y=eq_array)
            
            # Dinamicamente muda cor da linha se balance final > inicial
            if eq_array[-1] >= self._initial_balance:
                self._curve_item.setPen(pg.mkPen(color='#00e676', width=2))
            else:
                self._curve_item.setPen(pg.mkPen(color='#ff5252', width=2))
