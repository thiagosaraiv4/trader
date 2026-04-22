"""
ui/order_book_widget.py
------------------------
Order Book (Depth of Market) widget.

Displays 15 levels of bids and asks in a compact vertical layout
styled for professional trading visualization.

Anti-flicker strategy:
    setUpdatesEnabled(False) before batch cell update,
    setUpdatesEnabled(True) after — guarantees single-frame repaint.
    Without this, QTableWidget repaints after EACH setCellWidget/setItem
    call, causing visible flicker at high update rates.

Visual design:
    - Asks (sell side) displayed top-down: highest ask at top
    - Bids (buy side) displayed top-down: highest bid at top (nearest spread)
    - Spread label centered between the two tables
    - Volume bars as semi-transparent colored backgrounds
    - Price column right-aligned with .2f formatting
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.types import OrderBookSnapshot
from utils.logger import get_logger

log = get_logger(__name__, component="order_book")

# Colors
_ASK_BG = QColor(220, 60, 60, 35)       # Subtle red background
_ASK_TEXT = QColor(220, 90, 90)          # Red text
_BID_BG = QColor(0, 200, 100, 35)       # Subtle green background
_BID_TEXT = QColor(0, 180, 90)           # Green text
_QTY_COLOR = QColor(160, 160, 160)       # Gray for quantities
_SPREAD_COLOR = QColor(100, 100, 100)    # Muted for spread label

_MONO_FONT = QFont("Consolas", 9)
_MONO_FONT.setStyleHint(QFont.StyleHint.Monospace)


def _create_table(rows: int) -> QTableWidget:
    """Factory for a styled order book table (asks or bids)."""
    table = QTableWidget(rows, 2)
    table.setHorizontalHeaderLabels(["Price", "Qty"])
    table.verticalHeader().setVisible(False)
    table.setFont(_MONO_FONT)
    table.setShowGrid(False)
    table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)

    # Stretch columns to fill width
    header = table.horizontalHeader()
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
    header.setFont(_MONO_FONT)
    header.setFixedHeight(20)
    header.setStyleSheet(
        "QHeaderView::section {"
        "  background: #161616;"
        "  color: #555;"
        "  border: none;"
        "  border-bottom: 1px solid #252525;"
        "  padding: 2px 4px;"
        "  font-size: 9px;"
        "}"
    )

    table.setStyleSheet(
        "QTableWidget {"
        "  background: #0e0e0e;"
        "  border: none;"
        "  gridline-color: transparent;"
        "}"
        "QTableWidget::item {"
        "  padding: 1px 4px;"
        "  border: none;"
        "}"
    )

    # Compact row height
    table.verticalHeader().setDefaultSectionSize(18)

    return table


class OrderBookWidget(QWidget):
    """
    Depth of Market (DOM) widget with anti-flicker batch rendering.

    Layout:
        ┌─────────────────┐
        │   Asks (sells)   │  ← highest ask at top
        │   ...            │
        │   lowest ask     │
        ├──── Spread ──────┤
        │   highest bid    │
        │   ...            │
        │   Bids (buys)    │  ← lowest bid at bottom
        └─────────────────┘
    """

    _LEVELS: int = 15

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._setup_ui()
        log.info("OrderBookWidget initialized")

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.setStyleSheet("background: #0e0e0e; border: 1px solid #1e1e1e;")

        # Asks table (sell side)
        self._asks_table = _create_table(self._LEVELS)
        layout.addWidget(self._asks_table, stretch=1)

        # Spread label
        self._lbl_spread = QLabel("Spread: —")
        self._lbl_spread.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_spread.setFont(QFont("Consolas", 8))
        self._lbl_spread.setFixedHeight(22)
        self._lbl_spread.setStyleSheet(
            f"color: {_SPREAD_COLOR.name()};"
            "background: #121212;"
            "border-top: 1px solid #1e1e1e;"
            "border-bottom: 1px solid #1e1e1e;"
        )
        layout.addWidget(self._lbl_spread)

        # Bids table (buy side)
        self._bids_table = _create_table(self._LEVELS)
        layout.addWidget(self._bids_table, stretch=1)

    # ------------------------------------------------------------------
    # Slot
    # ------------------------------------------------------------------

    @Slot(object)
    def on_update(self, snapshot: OrderBookSnapshot) -> None:
        """
        Update the order book display from a snapshot.

        Uses setUpdatesEnabled(False/True) to batch all cell updates
        into a single repaint frame — eliminates flicker.
        """
        asks = snapshot.asks
        bids = snapshot.bids

        # Find max quantity for volume bar scaling
        max_qty = 1.0
        for level in asks:
            if level.quantity > max_qty:
                max_qty = level.quantity
        for level in bids:
            if level.quantity > max_qty:
                max_qty = level.quantity

        # --- Batch update asks (anti-flicker) ---
        self._asks_table.setUpdatesEnabled(False)
        for i in range(self._LEVELS):
            if i < len(asks):
                level = asks[i]
                # Asks displayed: highest at top → use reversed index
                row = i
                self._set_cell(
                    self._asks_table, row,
                    price=level.price,
                    qty=level.quantity,
                    max_qty=max_qty,
                    is_ask=True,
                )
            else:
                self._clear_row(self._asks_table, i)
        self._asks_table.setUpdatesEnabled(True)

        # --- Batch update bids (anti-flicker) ---
        self._bids_table.setUpdatesEnabled(False)
        for i in range(self._LEVELS):
            if i < len(bids):
                level = bids[i]
                self._set_cell(
                    self._bids_table, i,
                    price=level.price,
                    qty=level.quantity,
                    max_qty=max_qty,
                    is_ask=False,
                )
            else:
                self._clear_row(self._bids_table, i)
        self._bids_table.setUpdatesEnabled(True)

        # --- Spread ---
        self._lbl_spread.setText(f"Spread: {snapshot.spread:.2f}")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _set_cell(
        self,
        table: QTableWidget,
        row: int,
        price: float,
        qty: float,
        max_qty: float,
        is_ask: bool,
    ) -> None:
        """Set a single row's price and quantity with styled background."""
        text_color = _ASK_TEXT if is_ask else _BID_TEXT
        bg_base = _ASK_BG if is_ask else _BID_BG

        # Volume bar intensity (alpha scaled by qty proportion)
        intensity = min(qty / max_qty, 1.0) if max_qty > 0 else 0.0
        bg = QColor(bg_base)
        bg.setAlpha(int(25 + intensity * 55))  # 25..80 alpha range

        # Price cell
        price_item = table.item(row, 0)
        if price_item is None:
            price_item = QTableWidgetItem()
            price_item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            table.setItem(row, 0, price_item)
        price_item.setText(f"{price:,.2f}")
        price_item.setForeground(QBrush(text_color))
        price_item.setBackground(QBrush(bg))

        # Quantity cell
        qty_item = table.item(row, 1)
        if qty_item is None:
            qty_item = QTableWidgetItem()
            qty_item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            table.setItem(row, 1, qty_item)
        qty_item.setText(f"{qty:.4f}")
        qty_item.setForeground(QBrush(_QTY_COLOR))
        qty_item.setBackground(QBrush(bg))

    def _clear_row(self, table: QTableWidget, row: int) -> None:
        """Clear a row (no data for this level)."""
        for col in range(2):
            item = table.item(row, col)
            if item is None:
                item = QTableWidgetItem()
                table.setItem(row, col, item)
            item.setText("")
            item.setBackground(QBrush(QColor(14, 14, 14)))
