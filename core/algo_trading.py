"""
core/algo_trading.py
---------------------
Algorithmic trading engine with Strategy + Risk Manager + Orchestrator.

Architecture (Strategy → RiskManager → AlgoEngine):

    ┌──────────────────────────────────────────────────┐
    │  AlgoEngine.evaluate(price, indicators, pos, bal)│
    │       │                                          │
    │       ▼                                          │
    │  IStrategy.on_tick(price, indicators, pos)       │
    │       │ OrderRequest | None                      │
    │       ▼                                          │
    │  AlgoEngine: position sizing (risk-based)        │
    │       │ OrderRequest (sized)                     │
    │       ▼                                          │
    │  RiskManager.validate(order, balance)            │
    │       │ bool                                     │
    │       ▼                                          │
    │  Return order (approved) or None (rejected)      │
    └──────────────────────────────────────────────────┘

Separation of concerns:
    - IStrategy:   WHAT to trade (signal generation, entry/exit rules)
    - RiskManager: WHETHER it's safe (balance check, risk limits)
    - AlgoEngine:  HOW MUCH to trade (position sizing) + orchestration

Extensibility:
    1. New strategies: implement IStrategy, call engine.set_strategy()
       → zero changes to AlgoEngine, RiskManager, Controller, or Chart.
    2. Multiple strategies: extend AlgoEngine to hold list[IStrategy]
       and aggregate signals (majority vote, priority queue, etc.).
    3. Real execution: replace PaperTradingEngine.execute_order() with
       ccxt order submission — AlgoEngine is execution-agnostic.

Concurrency: single-threaded (qasync event loop). No locks needed.

Prohibited:
    - Strategy logic inside the Controller
    - Direct DataFrame access from strategy
    - Multiple simultaneous positions
    - Execution without risk validation
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from core.paper_trading import OrderRequest, Position
from utils.logger import get_logger


# ---------------------------------------------------------------------------
# Strategy Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class IStrategy(Protocol):
    """
    Structural contract for a trading strategy.

    Strategies are pure signal generators: they decide WHAT to trade
    based on price, indicators, and current position. They do NOT
    handle sizing (that's AlgoEngine) or validation (that's RiskManager).

    Returns OrderRequest with a requested quantity, or None for no action.
    The AlgoEngine may resize the quantity based on risk parameters.
    """

    @property
    def name(self) -> str:
        """Human-readable strategy name for logging/UI."""
        ...

    def on_tick(
        self,
        current_price: float,
        indicators: dict[str, float | None],
        current_position: Position | None,
    ) -> OrderRequest | str | None:
        """
        Evaluate market conditions and generate a trade signal.

        Args:
            current_price: Latest bar close price.
            indicators: Dict of indicator_name → value (None if not ready).
            current_position: Current open position, or None/FLAT.

        Returns:
            OrderRequest if a trade should be placed, None otherwise.
        """
        ...


# ---------------------------------------------------------------------------
# MovingAverageCrossStrategy
# ---------------------------------------------------------------------------

class MovingAverageCrossStrategy:
    """
    EMA(9) / SMA(20) crossover strategy with stop loss and take profit.

    Entry (BUY):
        - EMA_9 > SMA_20 (bullish crossover)
        - No existing position (anti-overtrading)
        - Cooldown elapsed since last position close

    Exit (SELL) — any of:
        1. EMA_9 < SMA_20 (bearish crossover)
        2. Take profit: unrealized PnL >= +2%
        3. Stop loss: unrealized PnL <= -1%

    Protections:
        - Anti-overtrading: never opens if position already exists
        - Cooldown: 30s (configurable) between closing and opening
        - Indicator validation: skips tick if EMA or SMA is None
    """

    _TP_PCT: float = 0.02    # Take profit: +2%
    _SL_PCT: float = -0.01   # Stop loss: -1%

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        trade_size: float = 0.01,
        cooldown_sec: float = 30.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._symbol: str = symbol
        self._trade_size: float = trade_size
        self._cooldown_sec: float = cooldown_sec
        self._clock: Callable[[], float] = clock or time.monotonic
        self._last_close_time: float = 0.0
        self._log = get_logger(__name__, component="ma_cross_strategy")

    @property
    def name(self) -> str:
        return "MA_Cross_EMA9_SMA20"

    def on_tick(
        self,
        current_price: float,
        indicators: dict[str, float | None],
        current_position: Position | None,
    ) -> OrderRequest | None:
        """
        Evaluate EMA/SMA crossover and position PnL for trade signals.

        Returns OrderRequest for BUY or SELL, or None for no action.
        """
        # --- Guard: indicators must be available ---
        ema = indicators.get("EMA_9")
        sma = indicators.get("SMA_20")

        if ema is None or sma is None:
            return None

        has_position = (
            current_position is not None
            and current_position.side != "FLAT"
            and current_position.size > 0
        )

        # --- EXIT LOGIC (priority: check exits before entries) ---
        if has_position and current_position is not None:
            assert current_position.side == "LONG"  # Only long positions in this strategy

            # Calculate PnL percentage
            pnl_pct = (
                (current_price - current_position.entry_price)
                / current_position.entry_price
            ) if current_position.entry_price > 0 else 0.0

            # Stop Loss (CRITICAL — always check first)
            if pnl_pct <= self._SL_PCT:
                self._log.info(
                    "🔴 STOP LOSS triggered",
                    extra={
                        "pnl_pct": f"{pnl_pct:.4%}",
                        "price": current_price,
                        "entry": current_position.entry_price,
                    },
                )
                self._last_close_time = self._clock()
                return OrderRequest(
                    symbol=self._symbol,
                    side="SELL",
                    quantity=current_position.size,
                    price=None,
                    order_type="MARKET",
                )

            # Take Profit
            if pnl_pct >= self._TP_PCT:
                self._log.info(
                    "🟢 TAKE PROFIT triggered",
                    extra={
                        "pnl_pct": f"{pnl_pct:.4%}",
                        "price": current_price,
                        "entry": current_position.entry_price,
                    },
                )
                self._last_close_time = self._clock()
                return OrderRequest(
                    symbol=self._symbol,
                    side="SELL",
                    quantity=current_position.size,
                    price=None,
                    order_type="MARKET",
                )

            # Bearish crossover (EMA crosses below SMA)
            if ema < sma:
                self._log.info(
                    "📉 Bearish crossover — closing position",
                    extra={
                        "ema": round(ema, 2),
                        "sma": round(sma, 2),
                        "price": current_price,
                    },
                )
                self._last_close_time = self._clock()
                return OrderRequest(
                    symbol=self._symbol,
                    side="SELL",
                    quantity=current_position.size,
                    price=None,
                    order_type="MARKET",
                )

            # Hold — no action
            return None

        # --- ENTRY LOGIC ---
        # Anti-overtrading: no new position if one exists
        if has_position:
            return None

        # Cooldown check
        elapsed = self._clock() - self._last_close_time
        if self._last_close_time > 0 and elapsed < self._cooldown_sec:
            return None

        # Bullish crossover (EMA above SMA)
        if ema > sma:
            self._log.info(
                "📈 Bullish crossover — opening position",
                extra={
                    "ema": round(ema, 2),
                    "sma": round(sma, 2),
                    "price": current_price,
                },
            )
            return OrderRequest(
                symbol=self._symbol,
                side="BUY",
                quantity=self._trade_size,
                price=None,
                order_type="MARKET",
            )

        return None


from collections import deque

# ---------------------------------------------------------------------------
# TrendFilteredMACrossStrategyV2
# ---------------------------------------------------------------------------

class TrendFilteredMACrossStrategyV2:
    """
    Trend Filtered MA Cross Strategy V2.
    
    Entry: 
      - Real Crossover: ema9_prev <= sma20_prev AND ema9 > sma20
      - Trend: current_price > sma200
      - Volatility: atr_mean * 1.1 < atr < atr_mean * 2.5
    Exit: 
      - EMA_9 < SMA_20 OR unrealized PnL >= 3% OR unrealized PnL <= -1.5%.
    """
    _TP_PCT: float = 0.03
    _SL_PCT: float = -0.015

    def __init__(self, symbol: str = "BTC/USDT", trade_size: float = 0.01) -> None:
        self._symbol: str = symbol
        self._trade_size: float = trade_size
        self._log = get_logger(__name__, component="trend_filtered_ma_strategy_v2")
        
        self._ema_9_prev: float | None = None
        self._sma_20_prev: float | None = None
        
        self._atr_window: deque[float] = deque(maxlen=14)
        self._atr_sum: float = 0.0

    @property
    def name(self) -> str:
        return "TrendFilteredMACrossV2"

    def on_tick(
        self,
        current_price: float,
        indicators: dict[str, float | None],
        current_position: Position | None,
    ) -> OrderRequest | None:
        ema_9 = indicators.get("EMA_9")
        sma_20 = indicators.get("SMA_20")
        sma_200 = indicators.get("SMA_200")
        atr = indicators.get("ATR_14")

        if ema_9 is None or sma_20 is None or sma_200 is None or atr is None:
            return None

        # 1. Update ATR average (O(1))
        if len(self._atr_window) == 14:
            evicted = self._atr_window[0]
            self._atr_sum -= evicted
        
        self._atr_window.append(atr)
        self._atr_sum += atr
        atr_mean = self._atr_sum / len(self._atr_window)

        # Retrieve prev state for crossover check
        ema_9_prev = self._ema_9_prev
        sma_20_prev = self._sma_20_prev
        
        # Persist new state
        self._ema_9_prev = ema_9
        self._sma_20_prev = sma_20

        if ema_9_prev is None or sma_20_prev is None:
            return None

        has_position = (
            current_position is not None
            and current_position.side != "FLAT"
            and current_position.size > 0
        )

        if has_position and current_position is not None:
            pnl_pct = (
                (current_price - current_position.entry_price)
                / current_position.entry_price
            ) if current_position.entry_price > 0 else 0.0

            if pnl_pct <= self._SL_PCT:
                self._log.info("🔴 STOP LOSS triggered", extra={"pnl_pct": f"{pnl_pct:.4%}", "price": current_price})
                return OrderRequest(symbol=self._symbol, side="SELL", quantity=current_position.size, price=None, order_type="MARKET")
            if pnl_pct >= self._TP_PCT:
                self._log.info("🟢 TAKE PROFIT triggered", extra={"pnl_pct": f"{pnl_pct:.4%}", "price": current_price})
                return OrderRequest(symbol=self._symbol, side="SELL", quantity=current_position.size, price=None, order_type="MARKET")
            if ema_9 < sma_20:
                self._log.info("📉 Bearish crossover — closing position", extra={"price": current_price})
                return OrderRequest(symbol=self._symbol, side="SELL", quantity=current_position.size, price=None, order_type="MARKET")

            return None

        # Real entry logic
        if ema_9_prev <= sma_20_prev and ema_9 > sma_20:
            if current_price > sma_200:
                if len(self._atr_window) >= 14 and (atr_mean * 1.1 < atr < atr_mean * 2.5):
                    self._log.info("📈 Verified Bullish Entry with Volatility Filter", extra={"price": current_price, "atr": atr, "atr_mean": atr_mean})
                    return OrderRequest(symbol=self._symbol, side="BUY", quantity=self._trade_size, price=None, order_type="MARKET")

        return None

# ---------------------------------------------------------------------------
# TrendFilteredMACrossStrategyV3
# ---------------------------------------------------------------------------

class TrendFilteredMACrossStrategyV3:
    """
    Trend Filtered MA Cross Strategy V3.
    Purely Declarative. Returns `LONG`, `FLAT` or `NONE`.
    """
    def __init__(self, symbol: str = "BTC/USDT") -> None:
        self._symbol: str = symbol
        self._log = get_logger(__name__, component="trend_filtered_ma_strategy_v3")
        
        self._ema_9_prev: float | None = None
        self._sma_20_prev: float | None = None
        
        self._atr_window: deque[float] = deque(maxlen=14)
        self._atr_sum: float = 0.0

    @property
    def name(self) -> str:
        return "TrendFilteredMACrossV3"
        
    @property
    def symbol(self) -> str:
        return self._symbol

    def on_tick(
        self,
        current_price: float,
        indicators: dict[str, float | None],
        current_position: Position | None,
    ) -> str:
        ema_9 = indicators.get("EMA_9")
        sma_20 = indicators.get("SMA_20")
        sma_200 = indicators.get("SMA_200")
        atr = indicators.get("ATR_14")

        if ema_9 is None or sma_20 is None or sma_200 is None or atr is None:
            return "NONE"

        if len(self._atr_window) == 14:
            evicted = self._atr_window[0]
            self._atr_sum -= evicted
        
        self._atr_window.append(atr)
        self._atr_sum += atr
        atr_mean = self._atr_sum / len(self._atr_window)

        ema_9_prev = self._ema_9_prev
        sma_20_prev = self._sma_20_prev
        
        self._ema_9_prev = ema_9
        self._sma_20_prev = sma_20

        if ema_9_prev is None or sma_20_prev is None:
            return "NONE"

        has_position = (
            current_position is not None
            and current_position.side != "FLAT"
            and current_position.size > 0
        )

        if has_position:
            if sma_20 - ema_9 > 0.3 * atr:
                self._log.info("📉 Bearish crossover — momentum loss", extra={"price": current_price})
                return "FLAT"
            return "NONE"

        if ema_9_prev <= sma_20_prev and ema_9 > sma_20:
            if current_price > sma_200:
                if len(self._atr_window) >= 14 and (atr_mean * 1.05 < atr < atr_mean * 3.0):
                    self._log.info("📈 Verified Bullish Entry", extra={"price": current_price, "atr": atr})
                    return "LONG"

        return "NONE"

# ---------------------------------------------------------------------------
# RiskManager
# ---------------------------------------------------------------------------

class RiskManager:
    """
    Pre-trade risk validation gate.

    Rules:
        - REJECT entry orders if cost exceeds available balance.
        - ALWAYS ALLOW exit orders (closing positions is risk-reducing).
        - Position sizing: caps quantity at max_risk_per_trade_pct of balance.

    This class is stateless — all context is passed per evaluation.
    """

    def __init__(self, max_risk_per_trade_pct: float = 0.02) -> None:
        self._max_risk_pct: float = max_risk_per_trade_pct
        self._log = get_logger(__name__, component="risk_manager")

    @property
    def max_risk_per_trade_pct(self) -> float:
        return self._max_risk_pct

    def size_order(self, balance: float, current_price: float, stop_price: float, max_position_pct: float = 0.20, fallback_realistic_stop: bool = True) -> float:
        """Dynamic position sizing explicitly by initial hard stop distance."""
        risk_amount = balance * self._max_risk_pct
        stop_distance = current_price - stop_price
        
        if fallback_realistic_stop:
            stop_distance = current_price * 0.005
            
        # 1. Stop mínimo absoluto (floor) para evitar alavancagem infinita
        floor = current_price * 0.005
        if stop_distance < floor:
            self._log.warning("Stop distance hit floor limit (0.5%)", extra={"original_stop": stop_distance, "floored": floor})
            stop_distance = max(stop_distance, floor)
        
        if stop_distance <= 0:
            self._log.warning("Invalid stop distance", extra={"stop": stop_price, "price": current_price})
            return 0.0
            
        raw_qty = risk_amount / stop_distance
        
        # 2. Teto dinâmico por balance
        max_position_usd = balance * max_position_pct
        max_qty = max_position_usd / current_price
        
        final_qty = min(raw_qty, max_qty)
        return round(final_qty, 6)

    def validate(
        self,
        order: OrderRequest,
        balance: float,
        current_price: float,
    ) -> OrderRequest | None:
        """
        Validate and potentially resize an order.

        For BUY orders:
            - Caps quantity at max_risk_per_trade_pct of balance / price
            - Rejects if even minimum order exceeds balance

        For SELL orders:
            - Always approved (exits reduce risk)

        Returns:
            Approved OrderRequest (possibly resized), or None if rejected.
        """
        # Exit orders always pass
        if order.side == "SELL":
            self._log.debug(
                "Exit order approved",
                extra={"qty": order.quantity},
            )
            return order

        # Entry order — validate and size
        if current_price <= 0 or balance <= 0:
            self._log.warning("Invalid price or balance for risk check")
            return None

        max_cost = balance * self._max_risk_pct
        max_qty = max_cost / current_price

        if max_qty < 0.0001:
            self._log.warning(
                "Insufficient balance for minimum order",
                extra={"balance": round(balance, 2), "max_cost": round(max_cost, 2)},
            )
            return None

        # Cap quantity at risk limit
        final_qty = min(order.quantity, max_qty)
        final_cost = final_qty * current_price

        if final_cost > balance:
            self._log.warning(
                "Order cost exceeds balance",
                extra={"cost": round(final_cost, 2), "balance": round(balance, 2)},
            )
            return None

        if final_qty != order.quantity:
            self._log.info(
                "Order resized by risk manager",
                extra={
                    "original_qty": order.quantity,
                    "approved_qty": round(final_qty, 6),
                },
            )

        approved = OrderRequest(
            symbol=order.symbol,
            side=order.side,
            quantity=round(final_qty, 6),
            price=order.price,
            order_type=order.order_type,
        )

        self._log.debug(
            "Entry order approved",
            extra={"qty": approved.quantity, "cost": round(final_cost, 2)},
        )
        return approved


# ---------------------------------------------------------------------------
# AlgoEngine (Orchestrator)
# ---------------------------------------------------------------------------

class AlgoEngine:
    """
    Top-level orchestrator: Strategy → RiskManager → Order.

    Responsibilities:
        - Hold and invoke the active strategy
        - Pass strategy output through risk validation
        - Enable/disable bot execution via UI toggle
        - Log all decisions for audit trail

    The engine is execution-agnostic: it returns an OrderRequest
    but does not execute it. The caller (Controller/main.py) decides
    how to execute (paper vs real).

    Thread-safety: single-threaded (qasync). No locks.
    """

    def __init__(
        self,
        risk_pct: float = 0.02,
        cooldown_sec: float = 180.0,
        initial_session_balance: float = 10_000.0,
        on_circuit_breaker: Callable[[str], None] | None = None,
    ) -> None:
        self._strategy: IStrategy | None = None
        self._risk_manager: RiskManager = RiskManager(max_risk_per_trade_pct=risk_pct)
        self._enabled: bool = False
        self._cooldown_sec: float = cooldown_sec
        self._last_trade_timestamp: float = 0.0
        self._prev_position_side: str = "FLAT"
        self._halted: bool = False
        self._initial_session_balance: float = initial_session_balance
        self._on_circuit_breaker: Callable[[str], None] | None = on_circuit_breaker
        self._log = get_logger(__name__, component="algo_engine")

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def strategy_name(self) -> str:
        return self._strategy.name if self._strategy else "None"

    def enable(self) -> None:
        """Activate the algo engine. Bot will trade on next tick."""
        self._enabled = True
        self._log.info(
            "AlgoEngine ENABLED",
            extra={"strategy": self.strategy_name},
        )

    def disable(self) -> None:
        """Deactivate the algo engine. No trades will be generated."""
        self._enabled = False
        self._log.info("AlgoEngine DISABLED")

    def set_strategy(self, strategy: IStrategy) -> None:
        """
        Set the active trading strategy.

        Can be called at runtime to switch strategies without
        restarting the engine.
        """
        self._strategy = strategy
        self._log.info(
            "Strategy set",
            extra={"strategy": strategy.name},
        )

    def set_halted(self, halted: bool) -> None:
        """Set the halted state (e.g. by MarketMonitor)."""
        self._halted = halted

    def reset_halt(self) -> None:
        """Manually unhalt the engine."""
        self._halted = False
        self._log.warning("AlgoEngine unhalted manually (reset_halt)")

    def evaluate(
        self,
        price: float,
        indicators: dict[str, float | None],
        position: Position | None,
        balance: float,
        timestamp: float | None = None,
    ) -> OrderRequest | None:
        """
        Full evaluation pipeline: Strategy → RiskManager → Order.

        Returns:
            Approved OrderRequest if the strategy signals a trade
            AND the RiskManager approves it. None otherwise.
        """
        if not self._enabled:
            return None

        if self._strategy is None:
            return None

        # O(1) Circuit breaker check — on equity (cash + open position value).
        # Cash alone drops by the position cost on every entry, which would
        # trip the breaker right after the first trade.
        equity = balance
        if position is not None and position.size > 0:
            if position.side == "LONG":
                equity += position.size * price
            elif position.side == "SHORT":
                equity -= position.size * price
        if equity < self._initial_session_balance * 0.93 and not self._halted:
            self._halted = True
            reason = "Drawdown >= 7% of session start balance"
            self._log.critical("CIRCUIT BREAKER TRIGGERED: " + reason)
            if self._on_circuit_breaker is not None:
                self._on_circuit_breaker(reason)

        # Track position side changes to determine exit timestamps
        curr_side = position.side if position else "FLAT"
        if self._prev_position_side != "FLAT" and curr_side == "FLAT":
            if timestamp is not None:
                self._last_trade_timestamp = timestamp
        self._prev_position_side = curr_side

        # Step 1: Strategy generates trade signal
        raw_signal = self._strategy.on_tick(price, indicators, position)

        if raw_signal is None or raw_signal == "NONE":
            return None

        is_entry = (position is None or position.side == "FLAT")

        # Branch for V3 Strategy String Signals
        if isinstance(raw_signal, str):
            if raw_signal == "FLAT":
                if not is_entry and position is not None:
                    # Strategy emitted FLAT (momentum loss). AlgoEngine closes immediately.
                    # Exit orders bypass _halted state.
                    approved = OrderRequest(
                        symbol=getattr(self._strategy, "symbol", "BTC/USDT"),
                        side="SELL",
                        quantity=position.size,
                        price=None,
                        order_type="MARKET"
                    )
                    return approved
                return None
            elif raw_signal == "LONG":
                if not is_entry:
                    return None
                    
                if self._halted:
                    return None

                    
                # Check cooldown only for entry orders
                if timestamp is not None:
                    elapsed = timestamp - self._last_trade_timestamp
                    if self._last_trade_timestamp > 0 and elapsed < self._cooldown_sec:
                        self._log.debug("Order REJECTED by cooldown", extra={"elapsed": round(elapsed, 1), "required": self._cooldown_sec})
                        return None
                
                atr = indicators.get("ATR_14")
                if atr is None:
                    return None
                    
                stop_price = price - (1.5 * atr)
                trail_distance_abs = max(1.0 * atr, price * 0.005)
                
                final_qty = self._risk_manager.size_order(balance, price, stop_price, fallback_realistic_stop=True)
                if final_qty <= 0:
                    self._log.info("Order REJECTED by risk manager (size <= 0)")
                    return None
                    
                approved = OrderRequest(
                    symbol=getattr(self._strategy, "symbol", "BTC/USDT"),
                    side="BUY",
                    quantity=final_qty,
                    price=None,
                    order_type="MARKET",
                    stop_loss_price=stop_price,
                    trail_distance_abs=trail_distance_abs
                )
                
                self._log.info(
                    "🤖 Bot order APPROVED (V3)",
                    extra={
                        "strategy": self.strategy_name,
                        "side": approved.side,
                        "qty": approved.quantity,
                        "type": approved.order_type,
                        "price": price,
                    },
                )
                return approved

        # Backwards compatibility branch (V1 / V2 Strategies)
        raw_order = raw_signal

        if is_entry and self._halted:
            return None

        # Check cooldown only for entry orders
        if is_entry and timestamp is not None:
            elapsed = timestamp - self._last_trade_timestamp
            if self._last_trade_timestamp > 0 and elapsed < self._cooldown_sec:
                self._log.debug(
                    "Order REJECTED by cooldown",
                    extra={"elapsed": round(elapsed, 1), "required": self._cooldown_sec}
                )
                return None

        # Step 2: Risk Manager validates and potentially resizes
        approved = self._risk_manager.validate(raw_order, balance, price)

        if approved is None:
            self._log.info(
                "Order REJECTED by risk manager",
                extra={
                    "side": raw_order.side,
                    "qty": raw_order.quantity,
                    "balance": round(balance, 2),
                },
            )
            return None

        self._log.info(
            "🤖 Bot order APPROVED",
            extra={
                "strategy": self.strategy_name,
                "side": approved.side,
                "qty": approved.quantity,
                "type": approved.order_type,
                "price": price,
            },
        )
        return approved
