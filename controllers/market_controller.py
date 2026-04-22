"""
controllers/market_controller.py
---------------------------------
MarketController: MVVM ViewModel layer.

Responsibilities:
- Own DataFeedManager instances (one per tracked symbol)
- Bridge asyncio callbacks → Qt Signals (thread-model: same thread via qasync)
- Throttle high-frequency bar updates to protect UI from flooding
- Manage active symbol selection (which feed drives the UI)
- Run IndicatorEngine (Phase 3): incremental O(1) updates per tick
- Generate simulated OrderBook data (Phase 3)
- Expose clean shutdown coroutine for graceful teardown

Throttle strategy — leading + trailing edge:
    First call in interval → emit immediately (low latency on first tick)
    Subsequent calls within interval → overwrite pending, emit at trailing edge
    Guarantees the *latest* value is always rendered (no stale display)

Multiple asset model:
    - All started feeds run concurrently in the asyncio loop
    - Only the active symbol emits snapshot/bar signals to the UI
    - Switching active symbol is O(1) — no feed restart needed
    - Background feeds maintain up-to-date state for instant switching
"""
from __future__ import annotations

import asyncio
import random
from typing import Any

import pandas as pd
from PySide6.QtCore import QDateTime, QObject, QTimer, Signal

from core.backtest_engine import BacktestResult
from core.algo_trading import AlgoEngine
from core.indicators import EMAIndicator, IndicatorEngine, SMAIndicator, ATRIndicator
from core.market_monitor import MarketMonitor
from core.paper_trading import PaperTradingEngine
from core.types import (
    BarData,
    FeedConfig,
    FeedStateChange,
    OrderBookLevel,
    OrderBookSnapshot,
)
from data.feed import DataFeedManager
from utils.logger import get_logger

log = get_logger(__name__, component="market_controller")

# Epoch for timestamp conversion (avoid recalculating)
_EPOCH = pd.Timestamp("1970-01-01", tz="UTC")

# Sentinel: distinct from None so we can store None as a valid pending value
_SENTINEL: object = object()


# ---------------------------------------------------------------------------
# Throttle
# ---------------------------------------------------------------------------

class _ThrottledEmitter(QObject):
    """
    Rate-limits a callable to at most 1 invocation per ``interval_ms``.

    Leading edge: first call within a quiet period fires immediately.
    Trailing edge: last call within the busy period fires at period end.

    This class is intentionally generic (Any payload) — type safety is
    enforced at the MarketController boundary where the value types are known.

    Thread-safety: Qt-thread only (QTimer is not thread-safe).
    Since qasync runs the asyncio loop on the Qt thread, all DataFeedManager
    callbacks execute on the Qt thread → safe to use here without locks.
    """

    def __init__(self, interval_ms: int, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._interval_ms = interval_ms
        self._pending: Any = _SENTINEL
        self._last_emit_ms: int = 0
        self._callbacks: list[Any] = []  # list[Callable[[Any], None]]

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_trailing_edge)

    def subscribe(self, callback: Any) -> None:  # callback: Callable[[Any], None]
        """Register a callable to invoke on each throttled emission."""
        self._callbacks.append(callback)

    def submit(self, value: Any) -> None:
        """Submit a new value. May emit immediately or defer."""
        now_ms: int = QDateTime.currentMSecsSinceEpoch()
        elapsed: int = now_ms - self._last_emit_ms

        if elapsed >= self._interval_ms:
            # Leading edge — emit immediately, cancel any pending trailing
            if self._timer.isActive():
                self._timer.stop()
            self._emit(value)
        else:
            # Within throttle window — store latest, schedule trailing edge
            self._pending = value
            if not self._timer.isActive():
                remaining: int = self._interval_ms - elapsed
                self._timer.start(max(remaining, 1))

    def _on_trailing_edge(self) -> None:
        if self._pending is not _SENTINEL:
            value = self._pending
            self._pending = _SENTINEL
            self._emit(value)

    def _emit(self, value: Any) -> None:
        self._last_emit_ms = QDateTime.currentMSecsSinceEpoch()
        for cb in self._callbacks:
            cb(value)

    def flush(self) -> None:
        """Force immediate emission of any pending value. Call before shutdown."""
        if self._timer.isActive():
            self._timer.stop()
        self._on_trailing_edge()


# ---------------------------------------------------------------------------
# OrderBook simulator (generates realistic book from last price)
# ---------------------------------------------------------------------------

_BOOK_LEVELS: int = 15

def _generate_simulated_book(last_price: float) -> OrderBookSnapshot:
    """
    Generate a simulated order book from the last traded price.

    - Dynamic spread: proportional to price with random factor
    - Decreasing volume per level (highest near spread)
    - Slight random noise on prices and volumes
    """
    if last_price <= 0:
        empty: tuple[OrderBookLevel, ...] = ()
        return OrderBookSnapshot(bids=empty, asks=empty, spread=0.0)

    # Dynamic spread: ~0.01% of price with random jitter
    base_spread = last_price * 0.0001
    spread = base_spread * random.uniform(0.5, 2.0)
    half_spread = spread / 2.0

    mid = last_price
    tick = last_price * 0.00005  # Price increment per level (~0.005%)

    asks: list[OrderBookLevel] = []
    bids: list[OrderBookLevel] = []

    for i in range(_BOOK_LEVELS):
        # Volume decreases with distance from spread + noise
        base_vol = max(0.01, 5.0 / (1.0 + i * 0.5)) * random.uniform(0.8, 1.2)
        price_noise = random.uniform(-0.0001, 0.0001) * last_price

        ask_price = mid + half_spread + i * tick + abs(price_noise)
        bid_price = mid - half_spread - i * tick - abs(price_noise)

        asks.append(OrderBookLevel(price=round(ask_price, 2), quantity=round(base_vol, 4)))
        bids.append(OrderBookLevel(price=round(bid_price, 2), quantity=round(base_vol, 4)))

    actual_spread = asks[0].price - bids[0].price if asks and bids else 0.0

    return OrderBookSnapshot(
        bids=tuple(bids),
        asks=tuple(asks),
        spread=round(actual_spread, 2),
    )


# ---------------------------------------------------------------------------
# MarketController
# ---------------------------------------------------------------------------

class MarketController(QObject):
    """
    MVVM ViewModel: mediates between the async data layer and the Qt UI.

    Signals (Phase 1):
        snapshot_ready(pd.DataFrame)    — initial historical OHLCV data.
        bar_updated(BarData)            — throttled live bar.
        state_changed(FeedStateChange)  — feed lifecycle transition.
        active_symbol_changed(str)      — the active symbol changed.

    Signals (Phase 3 — Indicators):
        overlay_snapshot(str, object, object) — (name, x_list, y_list)
        overlay_point(str, object, float)     — (name, timestamp, value)

    Signals (Phase 3 — Order Book):
        orderbook_updated(object)        — OrderBookSnapshot

    Usage::

        ctrl = MarketController(bar_throttle_ms=100)
        ctrl.snapshot_ready.connect(chart.on_snapshot)
        ctrl.bar_updated.connect(chart.on_bar_update)
        ctrl.overlay_snapshot.connect(chart.on_overlay_snapshot)
        ctrl.overlay_point.connect(chart.on_overlay_point)
        await ctrl.start_feed(config)
    """

    # Phase 1 signals
    snapshot_ready:        Signal = Signal(object)   # pd.DataFrame
    bar_updated:           Signal = Signal(object)   # BarData
    state_changed:         Signal = Signal(object)   # FeedStateChange
    active_symbol_changed: Signal = Signal(str)

    # Phase 3 signals — Indicators
    overlay_snapshot: Signal = Signal(str, object, object)   # (name, x_list, y_list)
    overlay_point:    Signal = Signal(str, object, float)    # (name, timestamp, value)

    # Phase 3 signals — Order Book
    orderbook_updated: Signal = Signal(object)   # OrderBookSnapshot

    # Phase 4 signals — Algo Engine
    algo_status: Signal = Signal(str)   # Bot action description for UI
    backtest_completed: Signal = Signal(object) # BacktestResult
    circuit_breaker_triggered: Signal = Signal(str)
    halt_state_changed: Signal = Signal(bool)
    stale_position_detected: Signal = Signal(object, float)
    performance_updated: Signal = Signal(object) # PerformanceSnapshot

    def __init__(
        self,
        timeframe_seconds: int = 60,
        *,
        bar_throttle_ms: int = 100,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)

        # symbol_key → DataFeedManager
        self._feeds: dict[str, DataFeedManager] = {}
        self._active_symbol: str | None = None

        # Throttled forwarder for bar_updated signal
        self._bar_throttle = _ThrottledEmitter(bar_throttle_ms, parent=self)
        self._bar_throttle.subscribe(self._on_throttled_bar)

        # Phase 3: Indicator Engine with SMA(20) and EMA(9) pre-registered
        self._indicator_engine = IndicatorEngine()
        self._indicator_engine.register(ATRIndicator(period=14))
        self._indicator_engine.register(SMAIndicator(period=200))
        self._indicator_engine.register(SMAIndicator(period=20))
        self._indicator_engine.register(EMAIndicator(period=9))

        # Phase 4: Algo Engine and Paper Trading Engine (set via set_engines)
        self._algo_engine: AlgoEngine | None = None
        self._paper_engine: PaperTradingEngine | None = None

        # Operation Protection Layers (V3)
        self._market_monitor = MarketMonitor(timeframe_seconds=timeframe_seconds, parent=self)
        self._market_monitor.feed_stale.connect(self._on_feed_stale)
        self._market_monitor.feed_restored.connect(self._on_feed_restored)
        self._pending_restore: dict | None = None

        # Last price for order book generation
        self._last_price: float = 0.0

        self._log = get_logger(__name__, component="market_controller")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def active_symbol(self) -> str | None:
        return self._active_symbol

    @property
    def tracked_symbols(self) -> list[str]:
        return list(self._feeds.keys())

    def set_engines(
        self,
        algo_engine: AlgoEngine,
        paper_engine: PaperTradingEngine,
    ) -> None:
        """
        Inject the AlgoEngine and PaperTradingEngine references.

        Called from main.py after construction. Keeps the constructor
        clean and allows late-binding of dependencies.
        """
        self._algo_engine = algo_engine
        self._paper_engine = paper_engine
        self._log.info("AlgoEngine and PaperEngine references set")

    def set_pending_restore(self, position_data: dict) -> None:
        """Seta a posição pendente para reidratação com gap no primeiro bar recebido."""
        self._pending_restore = position_data

    def emit_circuit_breaker(self, reason: str) -> None:
        """Usado como callback pelo AlgoEngine para disparar o Circuit Breaker na UI."""
        self._log.critical(f"Emitindo Circuit Breaker para a UI: {reason}")
        self.circuit_breaker_triggered.emit(reason)
        # Ao acionar, a UI também precisa saber do estado HALT
        self.halt_state_changed.emit(True)

    def _on_feed_stale(self, elapsed: float) -> None:
        if self._algo_engine:
            self._algo_engine.set_halted(True)
        self.halt_state_changed.emit(True)

    def _on_feed_restored(self) -> None:
        if self._algo_engine:
            self._algo_engine.set_halted(False)
        self.halt_state_changed.emit(False)

    async def start_feed(self, config: FeedConfig) -> None:
        """
        Start a data feed for the given config.

        If this is the first feed started, it becomes the active symbol
        automatically. Subsequent feeds run in hot-standby background mode.

        Raises:
            RuntimeError: If a feed for this symbol is already running.
        """
        key = str(config.symbol)

        if key in self._feeds:
            self._log.warning("Feed already running — ignoring", extra={"symbol": key})
            return

        feed = DataFeedManager(
            config=config,
            on_snapshot=self._make_snapshot_callback(key),
            on_bar_update=self._make_bar_callback(key),
            on_state_change=self._make_state_callback(key),
        )
        self._feeds[key] = feed

        # Auto-activate first feed
        if self._active_symbol is None:
            self._set_active_symbol(key)

        await feed.start()
        self._market_monitor.start()
        self._log.info("Feed registered and started", extra={"symbol": key})

    def stop_active_feed(self) -> None:
        """Stop the currently active feed. (Does not remove it from memory)."""
        if self._active_symbol and self._active_symbol in self._feeds:
            feed = self._feeds[self._active_symbol]
            feed.stop()
        self._market_monitor.stop()
            
    # ------------------------------------------------------------------
    # Async Backtest
    # ------------------------------------------------------------------

    def run_backtest_async(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "1h",
        start_date=None,
        end_date=None,
        initial_balance: float = 10_000.0,
        risk_pct: float = 0.02,
    ) -> None:
        """
        Runs the BacktestEngine asynchronously without freezing the UI.
        On completion, emits `backtest_completed(result)`.
        """
        self._log.info(f"Starting async backtest for {symbol}...")
        
        # Configuration dictionary to pass to the isolated runner
        config = {
            "symbol": symbol,
            "timeframe": timeframe,
            "start_date": start_date,
            "end_date": end_date,
            "initial_balance": initial_balance,
            "trade_size": 0.01,
            "cooldown_sec": 3600.0,
            "risk_pct": risk_pct,
            "use_ccxt": True,
        }

        loop = asyncio.get_event_loop()
        
        def _on_done(future):
            try:
                result = future.result()
                self._log.info("Async backtest completed successfully.")
                self.backtest_completed.emit(result)
            except Exception as e:
                self._log.error(f"Async backtest failed: {e}", exc_info=True)

        task = loop.run_in_executor(None, _run_isolated_backtest, config)
        
        # In python 3.7+ run_in_executor returns a concurrent.futures.Future
        # Wrapping in asyncio.ensure_future to add done callback if needed,
        # but asyncio tasks can just add_done_callback if it's an asyncio Future.
        asyncio.ensure_future(task).add_done_callback(_on_done)


    async def stop_feed(self, symbol: str) -> None:
        """Stop and remove a specific feed. No-op if symbol not tracked."""
        feed = self._feeds.pop(symbol, None)
        if feed is None:
            self._log.warning("stop_feed: symbol not found", extra={"symbol": symbol})
            return
        await feed.stop()

        if self._active_symbol == symbol:
            # Switch to another running feed if available
            remaining = list(self._feeds.keys())
            self._set_active_symbol(remaining[0] if remaining else None)
            if not remaining:
                self._market_monitor.stop()

        self._log.info("Feed stopped", extra={"symbol": symbol})

    def set_active_symbol(self, symbol: str) -> None:
        """
        Switch the actively displayed symbol.

        The previous symbol's feed continues running in the background.
        The new symbol's current DataFrame is NOT re-emitted automatically —
        caller should request a re-render if needed.

        Raises:
            KeyError: If the symbol has no registered feed.
        """
        if symbol not in self._feeds:
            raise KeyError(f"No feed registered for symbol: {symbol!r}")
        self._set_active_symbol(symbol)

    async def shutdown(self) -> None:
        """
        Gracefully stop all running feeds.

        Flushes any pending throttled update before stopping to ensure
        the UI reflects the final state. Must be awaited before the
        event loop is stopped.
        """
        self._log.info("Shutdown initiated", extra={"feed_count": len(self._feeds)})

        # Emit any buffered bar update so UI is in final state
        self._bar_throttle.flush()

        # Stop all feeds concurrently
        stop_tasks = [feed.stop() for feed in self._feeds.values()]
        results = await asyncio.gather(*stop_tasks, return_exceptions=True)

        self._market_monitor.stop()

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                self._log.error(
                    "Feed stop error",
                    extra={"feed_index": i},
                    exc_info=result,
                )

        self._feeds.clear()
        self._log.info("MarketController shutdown complete")

    # ------------------------------------------------------------------
    # Internal: symbol management
    # ------------------------------------------------------------------

    def _set_active_symbol(self, symbol: str | None) -> None:
        if self._active_symbol == symbol:
            return
        self._active_symbol = symbol
        if symbol is not None:
            self.active_symbol_changed.emit(symbol)
            self._log.info("Active symbol set", extra={"symbol": symbol})

    # ------------------------------------------------------------------
    # Internal: throttled bar handler (Phase 3 additions)
    # ------------------------------------------------------------------

    def _on_throttled_bar(self, bar: BarData) -> None:
        """
        Called by the throttle emitter for each (throttled) bar update.

        Pipeline (UI-only — indicator and algo logic runs unthrottled):
        1. Emit bar_updated signal (Phase 1 — chart rendering)
        2. Emit cached indicator overlay points (Phase 3)
        3. Generate and emit simulated order book (Phase 3)
        """
        # 1. Forward the bar to chart
        self.bar_updated.emit(bar)

        # 2. Emit latest indicator values for chart overlays
        #    (indicators were already updated unthrottled in _make_bar_callback)
        if hasattr(self, "_last_indicator_values") and self._last_indicator_values:
            ts = bar.timestamp
            for name, value in self._last_indicator_values.items():
                if value is not None:
                    self.overlay_point.emit(name, ts, value)

        # 3. Update order book
        self._last_price = float(bar.close)
        book = _generate_simulated_book(self._last_price)
        self.orderbook_updated.emit(book)

    # ------------------------------------------------------------------
    # Internal: callback factories
    # ------------------------------------------------------------------

    def _make_snapshot_callback(self, symbol_key: str):
        """
        Returns a closure that forwards snapshots only for the active symbol.

        Phase 3 addition: also initializes the IndicatorEngine and
        emits overlay_snapshot signals for each indicator.
        """
        def _on_snapshot(df: pd.DataFrame) -> None:
            if symbol_key == self._active_symbol:
                self._log.debug(
                    "Emitting snapshot",
                    extra={"symbol": symbol_key, "bars": len(df)},
                )
                self.snapshot_ready.emit(df)

                # Phase 3: Initialize indicators on snapshot
                indicator_series = self._indicator_engine.initialize(df)
                for name, series in indicator_series.items():
                    # Convert to Python lists for chart (no numpy in signal)
                    valid = series.dropna()
                    if valid.empty:
                        continue
                    x_data = (
                        (valid.index - _EPOCH)
                        .total_seconds()
                        .tolist()
                    )
                    y_data = valid.tolist()
                    self.overlay_snapshot.emit(name, x_data, y_data)
                    self._log.debug(
                        "Overlay snapshot emitted",
                        extra={"indicator": name, "points": len(x_data)},
                    )

                # Set initial price for order book
                if not df.empty:
                    self._last_price = float(df["close"].iloc[-1])

        return _on_snapshot

    def _make_bar_callback(self, symbol_key: str):
        """
        Returns a closure for UNTHROTTLED bar processing.

        Execution order (strict and immutable):
            1. indicator_engine.update(bar)  — O(1) per indicator
            2. algo_engine.evaluate(...)     — strategy + risk check
            3. bar_throttle.submit(bar)      — UI update (throttled)

        The AlgoEngine MUST NOT be called before indicators are updated.
        """
        def _on_bar(bar: BarData) -> None:
            if symbol_key != self._active_symbol:
                return

            if self._pending_restore is not None:
                self._log.info("Pending restore detected on first bar. Emitting stale_position_detected.")
                self.stale_position_detected.emit(self._pending_restore, float(bar.close))
                self._pending_restore = None

            # Step 1: Update indicators (MUST be first)
            indicator_values = self._indicator_engine.update(bar)
            self._last_indicator_values = indicator_values

            # Step 2: Evaluate algo engine with fresh indicators
            if self._algo_engine is not None and self._paper_engine is not None:
                # Update mark price for trailing stop / PnL tracking
                self._paper_engine.update_mark_price(bar)

                order = self._algo_engine.evaluate(
                    price=float(bar.close),
                    indicators=indicator_values,
                    position=self._paper_engine.position,
                    balance=self._paper_engine.balance,
                    timestamp=bar.timestamp.timestamp(),
                )
                if order is not None:
                    self._paper_engine.execute_order(order)
                    self.algo_status.emit(
                        f"\U0001f916 {order.side} {order.quantity:.6f} @ {float(bar.close):,.2f}"
                    )

            # Step 3: Throttle for UI (MUST be last)
            self._bar_throttle.submit(bar)
            self._market_monitor.on_bar_received(bar)

        return _on_bar

    def _make_state_callback(self, symbol_key: str):
        """
        Returns a closure that forwards state changes for ALL symbols.

        State changes are always forwarded regardless of active symbol
        so the status bar reflects all feed health, not just the active one.
        """
        def _on_state(change: FeedStateChange) -> None:
            self._log.debug(
                "Feed state change",
                extra={
                    "symbol": symbol_key,
                    "from": change.previous.name,
                    "to": change.current.name,
                },
            )
            self.state_changed.emit(change)

        return _on_state


# ---------------------------------------------------------------------------
# Isolated Backtest Runner
# ---------------------------------------------------------------------------

def _run_isolated_backtest(config: dict) -> BacktestResult:
    """
    Pure method to instantiate and run the Backtest Engine.
    Called inside a ThreadPoolExecutor to isolate state and prevent race conditions.
    """
    from core.backtest_engine import BacktestEngine
    
    engine = BacktestEngine(
        symbol=config["symbol"],
        timeframe=config["timeframe"],
        start_date=config["start_date"],
        end_date=config["end_date"],
        initial_balance=config["initial_balance"],
        trade_size=config["trade_size"],
        cooldown_sec=config["cooldown_sec"],
        risk_pct=config["risk_pct"],
        use_ccxt=config["use_ccxt"],
    )
    
    return engine.run()
