"""
ui/boleta_widget.py
--------------------
Order entry (Boleta) widget for paper trading.

Layout (140px height, horizontal):
    ┌──────────────────────────────────────────────────────────────────┐
    │ [COMPRAR]  Preço:[_____]  Qtd:[_____]  │  Posição: +0.5 BTC     │
    │ [VENDER]   Tipo:[Market▼]             │  PnL: +$123.45          │
    │                                        │  Saldo: $10,000.00      │
    └──────────────────────────────────────────────────────────────────┘

Design decisions:
    - Price field disabled for Market orders (filled from last price)
    - Buy button green, Sell button red — with hover effects
    - PnL colored green/red based on sign
    - All signals/slots follow MVVM — widget emits OrderRequest,
      receives Position updates.
"""
from __future__ import annotations

from PySide6.QtCore import Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.paper_trading import OrderRequest, PositionSnapshot, TradeResult
from utils.logger import get_logger

log = get_logger(__name__, component="boleta")

_MONO = QFont("Consolas", 10)
_MONO.setStyleHint(QFont.StyleHint.Monospace)

_LABEL_FONT = QFont("Consolas", 9)

_BUY_STYLE = """
    QPushButton {
        background: #0a6e3a;
        color: #e8e8e8;
        font-weight: bold;
        font-size: 12px;
        border: 1px solid #0c8a4a;
        border-radius: 3px;
        padding: 6px 16px;
        min-width: 80px;
    }
    QPushButton:hover {
        background: #0c8a4a;
        border-color: #0ea55a;
    }
    QPushButton:pressed {
        background: #085a30;
    }
"""

_SELL_STYLE = """
    QPushButton {
        background: #8b1a1a;
        color: #e8e8e8;
        font-weight: bold;
        font-size: 12px;
        border: 1px solid #a52020;
        border-radius: 3px;
        padding: 6px 16px;
        min-width: 80px;
    }
    QPushButton:hover {
        background: #a52020;
        border-color: #c02828;
    }
    QPushButton:pressed {
        background: #6e1414;
    }
"""

_COMBO_STYLE = """
    QComboBox {
        background: #1a1a1a;
        color: #ccc;
        border: 1px solid #333;
        border-radius: 2px;
        padding: 3px 8px;
        min-width: 80px;
    }
    QComboBox::drop-down {
        border: none;
        width: 16px;
    }
    QComboBox QAbstractItemView {
        background: #1a1a1a;
        color: #ccc;
        selection-background-color: #333;
        border: 1px solid #333;
    }
"""

_SPINBOX_STYLE = """
    QDoubleSpinBox {
        background: #1a1a1a;
        color: #ddd;
        border: 1px solid #333;
        border-radius: 2px;
        padding: 3px 6px;
        min-width: 90px;
    }
    QDoubleSpinBox:disabled {
        color: #555;
        background: #141414;
    }
"""


class BoletaWidget(QWidget):
    """
    Order entry widget emitting OrderRequest signals.

    External interface:
        Signal: order_submitted(object) — emits OrderRequest
        Slot:   on_position_updated(Position) — updates display labels
        Slot:   on_order_filled(TradeResult) — shows fill confirmation
    """

    order_submitted: Signal = Signal(object)  # OrderRequest
    bot_toggled: Signal = Signal(bool)         # Phase 4: algo on/off

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current_symbol: str = "BTC/USDT"
        self._setup_ui()
        self._connect_internal()
        log.info("BoletaWidget initialized")

    def set_symbol(self, symbol: str) -> None:
        """Update the active trading symbol."""
        self._current_symbol = symbol

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        self.setStyleSheet(
            "background: #0e0e0e;"
            "border: 1px solid #1e1e1e;"
        )

        root = QHBoxLayout(self)
        root.setContentsMargins(10, 6, 10, 6)
        root.setSpacing(12)

        # --- Left: order controls ---
        controls = QVBoxLayout()
        controls.setSpacing(6)

        # Row 1: Buy button + Price
        row1 = QHBoxLayout()
        row1.setSpacing(8)

        self._btn_buy = QPushButton("COMPRAR")
        self._btn_buy.setStyleSheet(_BUY_STYLE)
        row1.addWidget(self._btn_buy)

        lbl_price = QLabel("Preço:")
        lbl_price.setFont(_LABEL_FONT)
        lbl_price.setStyleSheet("color: #888; border: none;")
        row1.addWidget(lbl_price)

        self._spin_price = QDoubleSpinBox()
        self._spin_price.setFont(_MONO)
        self._spin_price.setStyleSheet(_SPINBOX_STYLE)
        self._spin_price.setRange(0.01, 999_999.99)
        self._spin_price.setDecimals(2)
        self._spin_price.setValue(50_000.00)
        self._spin_price.setSingleStep(10.0)
        row1.addWidget(self._spin_price)

        lbl_qty = QLabel("Qtd:")
        lbl_qty.setFont(_LABEL_FONT)
        lbl_qty.setStyleSheet("color: #888; border: none;")
        row1.addWidget(lbl_qty)

        self._spin_qty = QDoubleSpinBox()
        self._spin_qty.setFont(_MONO)
        self._spin_qty.setStyleSheet(_SPINBOX_STYLE)
        self._spin_qty.setRange(0.0001, 100.0)
        self._spin_qty.setDecimals(4)
        self._spin_qty.setValue(0.01)
        self._spin_qty.setSingleStep(0.01)
        row1.addWidget(self._spin_qty)

        controls.addLayout(row1)

        # Row 2: Sell button + Type
        row2 = QHBoxLayout()
        row2.setSpacing(8)

        self._btn_sell = QPushButton("VENDER")
        self._btn_sell.setStyleSheet(_SELL_STYLE)
        row2.addWidget(self._btn_sell)

        lbl_type = QLabel("Tipo:")
        lbl_type.setFont(_LABEL_FONT)
        lbl_type.setStyleSheet("color: #888; border: none;")
        row2.addWidget(lbl_type)

        self._combo_type = QComboBox()
        self._combo_type.setFont(_MONO)
        self._combo_type.setStyleSheet(_COMBO_STYLE)
        self._combo_type.addItems(["Market", "Limit"])
        row2.addWidget(self._combo_type)

        row2.addStretch()
        controls.addLayout(row2)

        # Row 3: Bot toggle (Phase 4)
        row3 = QHBoxLayout()
        row3.setSpacing(8)

        self._btn_bot = QPushButton("🤖 Bot OFF")
        self._btn_bot.setCheckable(True)
        self._btn_bot.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        self._btn_bot.setStyleSheet("""
            QPushButton {
                background: #1a1a1a;
                color: #555;
                font-weight: bold;
                border: 1px solid #333;
                border-radius: 3px;
                padding: 4px 14px;
                min-width: 90px;
            }
            QPushButton:hover {
                background: #222;
                border-color: #444;
            }
            QPushButton:checked {
                background: #0a5e2a;
                color: #4ade80;
                border-color: #0c8a4a;
            }
            QPushButton:checked:hover {
                background: #0c7a3a;
            }
        """)
        row3.addWidget(self._btn_bot)

        self._lbl_bot_status = QLabel("INATIVO")
        self._lbl_bot_status.setFont(QFont("Consolas", 9))
        self._lbl_bot_status.setStyleSheet("color: #555; border: none;")
        row3.addWidget(self._lbl_bot_status)

        row3.addStretch()
        controls.addLayout(row3)

        root.addLayout(controls, stretch=3)

        # --- Divider ---
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setStyleSheet("color: #2a2a2a; border: none; background: #2a2a2a; max-width: 1px;")
        root.addWidget(divider)

        # --- Right: position info ---
        info = QVBoxLayout()
        info.setSpacing(4)

        self._lbl_position = QLabel("Posição: FLAT")
        self._lbl_position.setFont(_MONO)
        self._lbl_position.setStyleSheet("color: #aaa; border: none;")
        info.addWidget(self._lbl_position)

        self._lbl_pnl = QLabel("PnL: $0.00")
        self._lbl_pnl.setFont(_MONO)
        self._lbl_pnl.setStyleSheet("color: #888; border: none;")
        info.addWidget(self._lbl_pnl)

        self._lbl_balance = QLabel("Saldo: $10,000.00")
        self._lbl_balance.setFont(_MONO)
        self._lbl_balance.setStyleSheet("color: #666; border: none;")
        info.addWidget(self._lbl_balance)

        self._lbl_status = QLabel("")
        self._lbl_status.setFont(QFont("Consolas", 8))
        self._lbl_status.setStyleSheet("color: #555; border: none;")
        info.addWidget(self._lbl_status)

        root.addLayout(info, stretch=2)

    def _connect_internal(self) -> None:
        """Wire internal button clicks to order submission logic."""
        self._btn_buy.clicked.connect(lambda: self._submit_order("BUY"))
        self._btn_sell.clicked.connect(lambda: self._submit_order("SELL"))
        self._combo_type.currentTextChanged.connect(self._on_type_changed)
        self._btn_bot.toggled.connect(self._on_bot_toggled)

        # Market mode: disable price field (uses current price)
        self._on_type_changed(self._combo_type.currentText())

    # ------------------------------------------------------------------
    # Internal logic
    # ------------------------------------------------------------------

    def _on_type_changed(self, order_type: str) -> None:
        """Enable/disable price field based on order type."""
        is_limit = order_type == "Limit"
        self._spin_price.setEnabled(is_limit)

    def _on_bot_toggled(self, checked: bool) -> None:
        """Handle bot toggle button state change."""
        if checked:
            self._btn_bot.setText("🤖 Bot ON")
            self._lbl_bot_status.setText("ATIVO")
            self._lbl_bot_status.setStyleSheet("color: #4ade80; border: none;")
            log.info("Bot toggled ON by user")
        else:
            self._btn_bot.setText("🤖 Bot OFF")
            self._lbl_bot_status.setText("INATIVO")
            self._lbl_bot_status.setStyleSheet("color: #555; border: none;")
            log.info("Bot toggled OFF by user")
        self.bot_toggled.emit(checked)

    def _submit_order(self, side: str) -> None:
        """Build and emit an OrderRequest."""
        order_type = self._combo_type.currentText().upper()
        qty = self._spin_qty.value()

        if order_type == "MARKET":
            price = None
        else:
            price = self._spin_price.value()

        request = OrderRequest(
            symbol=self._current_symbol,
            side=side,
            quantity=qty,
            price=price,
            order_type=order_type,
        )

        log.info(
            "Order submitted",
            extra={"side": side, "type": order_type, "qty": qty, "price": price},
        )
        self.order_submitted.emit(request)

    # ------------------------------------------------------------------
    # Public Slots
    # ------------------------------------------------------------------

    @Slot(object)
    def on_position_updated(self, snap: PositionSnapshot) -> None:
        """Update position labels from PaperTradingEngine signal."""
        # Position
        if not snap.has_position:
            pos_text = "Posição: FLAT"
            pos_color = "#888"
        elif snap.last_signal == "LONG":
            pos_text = f"Posição: +{snap.size:.4f} @ {snap.entry_price:,.2f}"
            pos_color = "#00c864"
        else: # SHORT
            pos_text = f"Posição: -{snap.size:.4f} @ {snap.entry_price:,.2f}"
            pos_color = "#dc3c3c"
        self._lbl_position.setText(pos_text)
        self._lbl_position.setStyleSheet(f"color: {pos_color}; border: none;")

        # Update PnL representation 
        if snap.unrealized_pnl_pct is not None:
            pnl_sign = "+" if snap.unrealized_pnl_pct >= 0 else ""
            pnl_color = "#00c864" if snap.unrealized_pnl_pct >= 0 else "#dc3c3c"
            self._lbl_pnl.setText(f"PnL: {pnl_sign}{snap.unrealized_pnl_pct:.2%}")
            self._lbl_pnl.setStyleSheet(f"color: {pnl_color}; border: none;")
        else:
            self._lbl_pnl.setText("PnL: $0.00")
            self._lbl_pnl.setStyleSheet("color: #888; border: none;")

    @Slot(object)
    def on_order_filled(self, result: TradeResult) -> None:
        """Show fill confirmation in status label."""
        self._lbl_status.setText(
            f"Fill: {result.side} {result.fill_quantity:.4f} "
            f"@ {result.fill_price:,.2f}  (fee: ${result.commission:.2f})"
        )

    def update_balance(self, balance: float) -> None:
        """Update balance display. Called from main wiring."""
        self._lbl_balance.setText(f"Saldo: ${balance:,.2f}")

    @Slot(str)
    def on_algo_status(self, message: str) -> None:
        """Display algo engine action in the status label."""
        self._lbl_status.setText(message)
