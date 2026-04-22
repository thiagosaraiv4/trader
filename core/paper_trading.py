"""
core/paper_trading.py
---------------------
Paper trading engine — Hexagonal Architecture (Ports & Adapters).

Layers:
    1. PaperTradingCore   — Pure business logic. No Qt, no logging, no signals.
                            Uses optional callbacks for event notification.
    2. PaperTradingEngine — Qt adapter (desktop). Delegates to Core,
                            translates callbacks → Qt Signals.

Commission model: 0.1% per fill (Binance spot tier).

Position averaging:
    When adding to an existing position:
        new_size = old_size + fill_qty
        new_avg  = (old_size * old_avg + fill_qty * fill_price) / new_size
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Data types (shared by all adapters)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class OrderRequest:
    """Immutable order submission."""
    symbol: str
    side: str           # "BUY" | "SELL"
    quantity: float
    price: float | None  # None = Market order
    order_type: str     # "MARKET" | "LIMIT"
    stop_loss_price: float | None = None
    trail_distance_abs: float | None = None


@dataclass(frozen=True, slots=True)
class TradeResult:
    """Immutable record of a filled order."""
    fill_price: float
    fill_quantity: float
    commission: float
    side: str
    timestamp: datetime


@dataclass(slots=True)
class Position:
    """Mutable position state. side: 'LONG' | 'SHORT' | 'FLAT'."""
    symbol: str = ""
    side: str = "FLAT"
    size: float = 0.0
    entry_price: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    trailing_stop_price: float = 0.0
    trail_distance_abs: float = 0.0
    trailing_active: bool = False
    atr_at_entry: float = 0.0
    entry_time_iso: str = ""


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    """Immutable snapshot for UI / logging consumption."""
    balance: float
    has_position: bool
    entry_price: float | None
    size: float | None
    unrealized_pnl_pct: float | None
    last_signal: str
    exit_mechanism: str | None
    trailing_active: bool


# ---------------------------------------------------------------------------
# Commission
# ---------------------------------------------------------------------------

_COMMISSION_RATE: float = 0.001  # 0.1%


# ---------------------------------------------------------------------------
# PaperTradingCore — Pure business logic (Port)
# ---------------------------------------------------------------------------

class PaperTradingCore:
    """
    Pure paper trading engine. No Qt, no logging, no framework deps.

    Event notification via optional callbacks (Ports & Adapters pattern).
    All methods are O(1) per call except restore replay.
    """

    def __init__(
        self,
        initial_balance: float = 10_000.0,
        restore: Any = None,
        on_position_updated: Callable[[PositionSnapshot], None] | None = None,
        on_order_filled: Callable[[TradeResult], None] | None = None,
        on_trade_closed: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._balance: float = initial_balance
        self._initial_balance: float = initial_balance
        self._position: Position = Position()
        self._pending_orders: list[OrderRequest] = []
        self._last_price: float = 0.0
        self._last_signal: str = "NONE"
        self._last_exit_mechanism: str | None = None
        self._trade_log: list[dict[str, Any]] = []

        # Callbacks (ports)
        self._on_position_updated = on_position_updated
        self._on_order_filled = on_order_filled
        self._on_trade_closed = on_trade_closed

        # Restore from saved session
        if restore is not None:
            self._balance = restore.balance
            self._initial_balance = restore.balance
            self._trade_log = list(restore.trade_log) if restore.trade_log else []
            if restore.position and restore.position.get("side") not in (None, "FLAT"):
                p = restore.position
                self._position.symbol = p.get("symbol", "")
                self._position.side = p.get("side", "FLAT")
                self._position.size = float(p.get("size", 0.0))
                self._position.entry_price = float(p.get("entry_price", 0.0))
                self._position.trailing_stop_price = float(p.get("trailing_stop_price", 0.0))
                self._position.trail_distance_abs = float(p.get("trail_distance_abs", 0.0))
                self._position.trailing_active = bool(p.get("trailing_active", False))
                self._position.entry_time_iso = p.get("entry_time_iso", "")

    # -- Properties --------------------------------------------------------

    @property
    def balance(self) -> float:
        return self._balance

    @property
    def position(self) -> Position:
        return self._position

    @property
    def trade_log(self) -> list[dict[str, Any]]:
        return self._trade_log

    # -- Snapshot -----------------------------------------------------------

    def make_snapshot(self) -> PositionSnapshot:
        """Build a frozen PositionSnapshot from current mutable state."""
        has_pos = self._position.side != "FLAT" and self._position.size > 0
        if has_pos and self._position.entry_price > 0:
            pnl_pct = (
                (self._last_price - self._position.entry_price)
                / self._position.entry_price
            ) if self._last_price > 0 else 0.0
        else:
            pnl_pct = None

        if has_pos:
            if self._position.trailing_active:
                mechanism = "Trailing"
            elif self._position.trailing_stop_price > 0:
                mechanism = "Hard Stop"
            else:
                mechanism = "Momentum"
        else:
            mechanism = None

        return PositionSnapshot(
            balance=self._balance,
            has_position=has_pos,
            entry_price=self._position.entry_price if has_pos else None,
            size=self._position.size if has_pos else None,
            unrealized_pnl_pct=pnl_pct,
            last_signal=self._last_signal,
            exit_mechanism=mechanism,
            trailing_active=self._position.trailing_active,
        )

    def _notify_position(self) -> None:
        if self._on_position_updated is not None:
            self._on_position_updated(self.make_snapshot())

    # -- Session state ------------------------------------------------------

    def get_session_state(self) -> dict[str, Any]:
        """Return serializable state for session persistence."""
        pos = self._position
        open_position: dict[str, Any] | None = None
        if pos.side != "FLAT" and pos.size > 0:
            open_position = {
                "symbol": pos.symbol,
                "side": pos.side,
                "size": pos.size,
                "entry_price": pos.entry_price,
                "trailing_stop_price": pos.trailing_stop_price,
                "trail_distance_abs": pos.trail_distance_abs,
                "trailing_active": pos.trailing_active,
                "entry_time_iso": pos.entry_time_iso,
            }
        return {
            "balance": self._balance,
            "open_position": open_position,
            "trade_log": self._trade_log,
        }

    # -- Force restore ------------------------------------------------------

    def force_restore_position(self, pos_data: dict[str, Any]) -> None:
        """Force-set position from external data (stale session rehydration)."""
        self._position.symbol = pos_data.get("symbol", "")
        self._position.side = pos_data.get("side", "FLAT")
        self._position.size = float(pos_data.get("size", 0.0))
        self._position.entry_price = float(pos_data.get("entry_price", 0.0))
        self._position.trailing_stop_price = float(pos_data.get("trailing_stop_price", 0.0))
        self._position.trail_distance_abs = float(pos_data.get("trail_distance_abs", 0.0))
        self._position.trailing_active = bool(pos_data.get("trailing_active", False))
        self._position.entry_time_iso = pos_data.get("entry_time_iso", "")
        self._notify_position()

    # -- Public API ---------------------------------------------------------

    def execute_order(self, request: OrderRequest) -> None:
        """Process an order request. Market orders execute immediately."""
        if request.quantity <= 0:
            return

        if request.order_type == "MARKET":
            if self._last_price <= 0:
                return
            self._fill_order(request, self._last_price)
        elif request.order_type == "LIMIT":
            if request.price is None or request.price <= 0:
                return
            self._pending_orders.append(request)

    def update_mark_price(self, bar: Any) -> None:
        """
        Update mark price, check pending orders, update PnL and trailing stop.
        bar must have .close, .low, .high attributes (BarData protocol).
        """
        self._last_price = float(bar.close)

        # Check pending limit orders
        self._check_pending_orders(bar)

        # Update unrealized PnL
        if self._position.side != "FLAT" and self._position.size > 0:
            if self._position.side == "LONG":
                self._position.unrealized_pnl = (
                    (self._last_price - self._position.entry_price)
                    * self._position.size
                )

                # Trailing Stop Logic (LONG only)
                pnl_pct = (
                    (self._last_price - self._position.entry_price)
                    / self._position.entry_price
                ) if self._position.entry_price > 0 else 0.0

                if not self._position.trailing_active and pnl_pct >= 0.003:
                    self._position.trailing_active = True
                    self._last_exit_mechanism = "Trailing"

                if self._position.trailing_active:
                    new_stop = self._last_price - self._position.trail_distance_abs
                    self._position.trailing_stop_price = max(
                        self._position.trailing_stop_price, new_stop
                    )

                # Check Hard Stop / Trailing Stop Trigger
                if self._last_price <= self._position.trailing_stop_price:
                    self._last_exit_mechanism = (
                        "Trailing" if self._position.trailing_active else "Hard Stop"
                    )
                    self.execute_order(OrderRequest(
                        symbol=self._position.symbol,
                        side="SELL",
                        quantity=self._position.size,
                        price=None,
                        order_type="MARKET",
                    ))
                    return  # execute_order already notifies

            elif self._position.side == "SHORT":
                self._position.unrealized_pnl = (
                    (self._position.entry_price - self._last_price)
                    * self._position.size
                )
            self._notify_position()

    # -- Internal: order execution ------------------------------------------

    def _fill_order(self, request: OrderRequest, fill_price: float) -> None:
        """Execute the order at the given price."""
        qty = request.quantity
        cost = fill_price * qty
        commission = cost * _COMMISSION_RATE

        trade_closed_pnl: float | None = None
        trade_pnl_pct: float | None = None

        # Check balance for buys
        if request.side == "BUY" and (cost + commission) > self._balance:
            return

        # Deduct commission
        self._balance -= commission
        pos = self._position

        if request.side == "BUY":
            self._balance -= cost
            if pos.side == "FLAT" or pos.side == "LONG":
                new_size = pos.size + qty
                if new_size > 0:
                    new_avg = (pos.size * pos.entry_price + qty * fill_price) / new_size
                else:
                    new_avg = fill_price
                pos.side = "LONG"
                pos.size = new_size
                pos.entry_price = new_avg
                pos.symbol = request.symbol
                pos.entry_time_iso = datetime.now(timezone.utc).isoformat()
                if request.stop_loss_price is not None:
                    pos.trailing_stop_price = request.stop_loss_price
                if request.trail_distance_abs is not None:
                    pos.trail_distance_abs = request.trail_distance_abs
                pos.trailing_active = False
            elif pos.side == "SHORT":
                if qty >= pos.size:
                    closed_pnl = (pos.entry_price - fill_price) * pos.size
                    trade_closed_pnl = closed_pnl
                    trade_pnl_pct = (
                        (pos.entry_price - fill_price) / pos.entry_price
                        if pos.entry_price > 0 else 0.0
                    )
                    pos.realized_pnl += closed_pnl
                    self._balance += pos.size * pos.entry_price
                    remainder = qty - pos.size
                    if remainder > 0:
                        pos.side = "LONG"
                        pos.size = remainder
                        pos.entry_price = fill_price
                        pos.entry_time_iso = datetime.now(timezone.utc).isoformat()
                    else:
                        pos.side = "FLAT"
                        pos.size = 0.0
                        pos.entry_price = 0.0
                        pos.entry_time_iso = ""
                else:
                    closed_pnl = (pos.entry_price - fill_price) * qty
                    trade_closed_pnl = closed_pnl
                    trade_pnl_pct = (
                        (pos.entry_price - fill_price) / pos.entry_price
                        if pos.entry_price > 0 else 0.0
                    )
                    pos.realized_pnl += closed_pnl
                    self._balance += qty * pos.entry_price
                    pos.size -= qty

        elif request.side == "SELL":
            if pos.side == "FLAT" or pos.side == "SHORT":
                self._balance += cost
                new_size = pos.size + qty
                if new_size > 0 and pos.size > 0:
                    new_avg = (pos.size * pos.entry_price + qty * fill_price) / new_size
                else:
                    new_avg = fill_price
                pos.side = "SHORT"
                pos.size = new_size
                pos.entry_price = new_avg
                pos.symbol = request.symbol
                pos.entry_time_iso = datetime.now(timezone.utc).isoformat()
            elif pos.side == "LONG":
                if qty >= pos.size:
                    closed_pnl = (fill_price - pos.entry_price) * pos.size
                    trade_closed_pnl = closed_pnl
                    trade_pnl_pct = (
                        (fill_price - pos.entry_price) / pos.entry_price
                        if pos.entry_price > 0 else 0.0
                    )
                    pos.realized_pnl += closed_pnl
                    self._balance += pos.size * fill_price
                    remainder = qty - pos.size
                    if remainder > 0:
                        self._balance += remainder * fill_price
                        pos.side = "SHORT"
                        pos.size = remainder
                        pos.entry_price = fill_price
                        pos.entry_time_iso = datetime.now(timezone.utc).isoformat()
                    else:
                        pos.side = "FLAT"
                        pos.size = 0.0
                        pos.entry_price = 0.0
                        pos.entry_time_iso = ""
                        pos.trailing_stop_price = 0.0
                        pos.trail_distance_abs = 0.0
                        pos.trailing_active = False
                else:
                    closed_pnl = (fill_price - pos.entry_price) * qty
                    trade_closed_pnl = closed_pnl
                    trade_pnl_pct = (
                        (fill_price - pos.entry_price) / pos.entry_price
                        if pos.entry_price > 0 else 0.0
                    )
                    pos.realized_pnl += closed_pnl
                    self._balance += qty * fill_price
                    pos.size -= qty

        # Update unrealized PnL for remaining position
        if pos.side == "LONG" and pos.size > 0:
            pos.unrealized_pnl = (self._last_price - pos.entry_price) * pos.size
        elif pos.side == "SHORT" and pos.size > 0:
            pos.unrealized_pnl = (pos.entry_price - self._last_price) * pos.size
        else:
            pos.unrealized_pnl = 0.0

        result = TradeResult(
            fill_price=fill_price,
            fill_quantity=qty,
            commission=commission,
            side=request.side,
            timestamp=datetime.now(tz=timezone.utc),
        )

        # Track signal state
        if request.side == "BUY":
            self._last_signal = "LONG"
            self._last_exit_mechanism = None
        elif request.side == "SELL" and pos.side == "FLAT":
            self._last_signal = "FLAT"

        # Append to trade log
        log_entry: dict[str, Any] = {
            "side": result.side,
            "price": result.fill_price,
            "quantity": result.fill_quantity,
            "commission": result.commission,
            "timestamp": result.timestamp,
            "balance_after": self._balance,
        }

        if trade_closed_pnl is not None:
            log_entry["closed_pnl"] = trade_closed_pnl
            log_entry["pnl_pct"] = trade_pnl_pct

        self._trade_log.append(log_entry)

        # Notify via callbacks
        if trade_closed_pnl is not None and self._on_trade_closed is not None:
            self._on_trade_closed(log_entry)

        if self._on_order_filled is not None:
            self._on_order_filled(result)

        self._notify_position()

    def _check_pending_orders(self, bar: Any) -> None:
        """Check all pending limit orders against the current bar."""
        remaining: list[OrderRequest] = []
        for order in self._pending_orders:
            filled = False
            if order.side == "BUY" and order.price is not None:
                if float(bar.low) <= order.price:
                    self._fill_order(order, order.price)
                    filled = True
            elif order.side == "SELL" and order.price is not None:
                if float(bar.high) >= order.price:
                    self._fill_order(order, order.price)
                    filled = True
            if not filled:
                remaining.append(order)
        self._pending_orders = remaining


# ---------------------------------------------------------------------------
# PaperTradingEngine — Qt Adapter (Desktop)
# ---------------------------------------------------------------------------

try:
    from PySide6.QtCore import QObject, Signal

    class PaperTradingEngine(QObject):
        """
        Qt adapter for PaperTradingCore.

        Translates Core callbacks → Qt Signals.
        Zero business logic — pure adaptation layer.

        Preserves the exact same public API as the previous monolithic class
        to maintain backwards compatibility with main.py and controllers.
        """

        position_updated: Signal = Signal(object)   # PositionSnapshot
        order_filled: Signal = Signal(object)        # TradeResult
        trade_closed: Signal = Signal(object)        # dict (trade log entry)

        def __init__(
            self,
            initial_balance: float = 10_000.0,
            restore: Any = None,
            parent: QObject | None = None,
        ) -> None:
            super().__init__(parent)

            self._core = PaperTradingCore(
                initial_balance=initial_balance,
                restore=restore,
                on_position_updated=lambda snap: self.position_updated.emit(snap),
                on_order_filled=lambda result: self.order_filled.emit(result),
                on_trade_closed=lambda trade: self.trade_closed.emit(trade),
            )

            from utils.logger import get_logger
            self._log = get_logger(__name__, component="paper_trading")
            self._log.info(
                "PaperTradingEngine initialized",
                extra={"balance": self._core.balance, "restored": restore is not None},
            )

        # -- Delegated properties ------------------------------------------

        @property
        def balance(self) -> float:
            return self._core.balance

        @property
        def position(self) -> Position:
            return self._core.position

        # -- Delegated methods ---------------------------------------------

        def execute_order(self, request: OrderRequest) -> None:
            self._core.execute_order(request)

        def update_mark_price(self, bar: Any) -> None:
            self._core.update_mark_price(bar)

        def force_restore_position(self, pos_data: dict[str, Any]) -> None:
            self._log.info("Position manually FORCE restored", extra={"side": pos_data.get("side")})
            self._core.force_restore_position(pos_data)

        def get_session_state(self) -> dict[str, Any]:
            return self._core.get_session_state()

except ImportError:
    # PySide6 not available — headless environment
    # PaperTradingEngine is not needed; use PaperTradingCore directly.
    pass
