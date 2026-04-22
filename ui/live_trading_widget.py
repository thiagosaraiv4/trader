"""
ui/live_trading_widget.py
-------------------------
Widget to display live paper trading session state.
Receives PositionSnapshot from PaperTradingEngine and updates UI instantly.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Slot, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.paper_trading import PositionSnapshot


class LiveTradingWidget(QWidget):
    """
    Displays real-time PaperTradingEngine position and session balance.
    Updates via update_snapshot Slot (no polling).
    """

    reset_requested: Signal = Signal()

    def __init__(self, initial_balance: float = 10_000.0, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._initial_balance = initial_balance
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)

        # Style definitions
        self.setStyleSheet(
            "QFrame {"
            "  background: rgba(30, 30, 30, 0.6);"
            "  border: 1px solid #2a2a2a;"
            "  border-radius: 6px;"
            "}"
            "QLabel {"
            "  color: #d0d0d0;"
            "  border: none;"
            "  background: transparent;"
            "}"
            ".Badge {"
            "  border-radius: 4px;"
            "  padding: 2px 6px;"
            "  font-weight: bold;"
            "  font-size: 10px;"
            "  color: white;"
            "}"
        )

        title_font = QFont("Consolas", 10, QFont.Weight.Bold)
        value_font = QFont("Consolas", 12, QFont.Weight.Bold)
        label_font = QFont("Consolas", 9)

        # -------------------------------------------------------------
        # Banners (Alerts)
        # -------------------------------------------------------------
        self._stale_banner = QLabel("⚠ FEED STALE — 0s sem dados")
        self._stale_banner.setStyleSheet("background: #dc3c3c; color: white; padding: 6px; border-radius: 4px; font-weight: bold;")
        self._stale_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._stale_banner.setVisible(False)
        layout.addWidget(self._stale_banner)
        
        self._cb_banner = QFrame()
        self._cb_banner.setStyleSheet("background: #ff9800; border-radius: 4px;")
        cb_layout = QHBoxLayout(self._cb_banner)
        cb_layout.setContentsMargins(6, 6, 6, 6)
        self._cb_label = QLabel("🛑 CIRCUIT BREAKER ATIVO")
        self._cb_label.setStyleSheet("color: white; font-weight: bold; border: none;")
        self._cb_reset_btn = QPushButton("Reset Manual")
        self._cb_reset_btn.setStyleSheet("QPushButton { background: #d84315; color: white; border: 1px solid #bf360c; border-radius: 2px; padding: 2px 6px; } QPushButton:hover { background: #e64a19; }")
        self._cb_reset_btn.clicked.connect(self._on_reset_clicked)
        cb_layout.addWidget(self._cb_label)
        cb_layout.addStretch()
        cb_layout.addWidget(self._cb_reset_btn)
        self._cb_banner.setVisible(False)
        layout.addWidget(self._cb_banner)

        # -------------------------------------------------------------
        # Account Panel
        # -------------------------------------------------------------
        acc_frame = QFrame()
        acc_layout = QVBoxLayout(acc_frame)
        
        acc_title = QLabel("ACCOUNT BALANCE")
        acc_title.setFont(title_font)
        acc_title.setStyleSheet("color: #888;")
        self._lbl_balance = QLabel(f"${self._initial_balance:,.2f}")
        self._lbl_balance.setFont(value_font)
        self._lbl_balance.setAlignment(Qt.AlignmentFlag.AlignRight)
        
        acc_layout.addWidget(acc_title)
        acc_layout.addWidget(self._lbl_balance)
        layout.addWidget(acc_frame)

        # -------------------------------------------------------------
        # Position Panel
        # -------------------------------------------------------------
        pos_frame = QFrame()
        pos_layout = QVBoxLayout(pos_frame)
        
        pos_title = QLabel("CURRENT POSITION")
        pos_title.setFont(title_font)
        pos_title.setStyleSheet("color: #888;")
        pos_layout.addWidget(pos_title)

        def _make_row(label_text: str) -> tuple[QWidget, QLabel]:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            lbl = QLabel(label_text)
            lbl.setFont(label_font)
            val = QLabel("—")
            val.setFont(QFont("Consolas", 11, QFont.Weight.Bold))
            val.setAlignment(Qt.AlignmentFlag.AlignRight)
            row_layout.addWidget(lbl)
            row_layout.addWidget(val)
            return row, val

        pos_widget1, self._lbl_entry = _make_row("Entry Price:")
        pos_widget2, self._lbl_size = _make_row("Size (Qty):")
        pos_widget3, self._lbl_pnl = _make_row("Unrealized PnL:")

        pos_layout.addWidget(pos_widget1)
        pos_layout.addWidget(pos_widget2)
        pos_layout.addWidget(pos_widget3)
        layout.addWidget(pos_frame)

        # -------------------------------------------------------------
        # Signals Panel
        # -------------------------------------------------------------
        sig_frame = QFrame()
        sig_layout = QVBoxLayout(sig_frame)
        
        sig_title = QLabel("ALGO ENGINE STATE")
        sig_title.setFont(title_font)
        sig_title.setStyleSheet("color: #888;")
        sig_layout.addWidget(sig_title)

        # Badges row
        badge_widget = QWidget()
        badge_layout = QHBoxLayout(badge_widget)
        badge_layout.setContentsMargins(0, 0, 0, 0)
        badge_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)

        self._lbl_last_signal = QLabel("NONE")
        self._lbl_last_signal.setProperty("class", "Badge")
        self._lbl_last_signal.setStyleSheet("background: #555;")
        
        self._lbl_exit_mech = QLabel("NOT ACTIVE")
        self._lbl_exit_mech.setProperty("class", "Badge")
        self._lbl_exit_mech.setStyleSheet("background: #555;")

        badge_layout.addWidget(QLabel("Signal:"))
        badge_layout.addWidget(self._lbl_last_signal)
        badge_layout.addSpacing(10)
        badge_layout.addWidget(QLabel("Exit:"))
        badge_layout.addWidget(self._lbl_exit_mech)

        sig_layout.addWidget(badge_widget)
        layout.addWidget(sig_frame)

        layout.addStretch()

    @Slot(object)
    def update_snapshot(self, snapshot: PositionSnapshot) -> None:
        """Update the UI with the latest immutable snapshot."""
        # Account Balance
        if snapshot.balance >= self._initial_balance:
            color = "#00c864"  # Green
        else:
            color = "#dc3c3c"  # Red
        self._lbl_balance.setText(f"${snapshot.balance:,.2f}")
        self._lbl_balance.setStyleSheet(f"color: {color};")

        # Position Panel
        if snapshot.has_position:
            self._lbl_entry.setText(f"${snapshot.entry_price:,.2f}")
            self._lbl_size.setText(f"{snapshot.size:.6f}")
            
            pnl = snapshot.unrealized_pnl_pct
            if pnl is not None:
                pnl_color = "#00c864" if pnl >= 0 else "#dc3c3c"
                self._lbl_pnl.setText(f"{pnl:.2%}")
                self._lbl_pnl.setStyleSheet(f"color: {pnl_color};")
            else:
                self._lbl_pnl.setText("—")
                self._lbl_pnl.setStyleSheet("color: #d0d0d0;")
        else:
            self._lbl_entry.setText("—")
            self._lbl_size.setText("—")
            self._lbl_pnl.setText("—")
            self._lbl_pnl.setStyleSheet("color: #d0d0d0;")

        # Signal Badges
        sig = snapshot.last_signal
        sig_bg = "#555"
        if sig == "LONG":
            sig_bg = "#00c864"
        elif sig == "FLAT":
            sig_bg = "#dc3c3c"
        self._lbl_last_signal.setText(sig)
        self._lbl_last_signal.setStyleSheet(f"background: {sig_bg};")

        mech = snapshot.exit_mechanism
        mech_bg = "#555"
        if mech == "Trailing" and snapshot.trailing_active:
            mech_bg = "#2196F3"  # Blue
        elif mech == "Hard Stop":
            mech_bg = "#ff9800"  # Orange
        elif mech == "Momentum":
            mech_bg = "#9c27b0"  # Purple
            
        self._lbl_exit_mech.setText(mech if mech else "NOT ACTIVE")
        self._lbl_exit_mech.setStyleSheet(f"background: {mech_bg};")

    @Slot(float)
    def on_feed_stale(self, elapsed: float) -> None:
        self._stale_banner.setText(f"⚠ FEED STALE — {elapsed:.0f}s sem dados")
        if not self._cb_banner.isVisible():
            self._stale_banner.setVisible(True)

    @Slot()
    def on_feed_restored(self) -> None:
        self._stale_banner.setVisible(False)

    @Slot(str)
    def on_circuit_breaker(self, reason: str) -> None:
        self._cb_label.setText(f"🛑 CIRCUIT BREAKER ATIVO — {reason}")
        self._stale_banner.setVisible(False)  # CB tem prioridade
        self._cb_banner.setVisible(True)

    @Slot()
    def _on_reset_clicked(self) -> None:
        self.reset_requested.emit()
        self._cb_banner.setVisible(False)

