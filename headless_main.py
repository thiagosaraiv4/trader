#!/usr/bin/env python3
"""
headless_main.py
-----------------
Headless entry point for cloud / GitHub Codespaces deployment.

Pure asyncio — no Qt, no qasync, no GUI.
Designed for 24/7 autonomous operation with structured JSON logging.

Architecture:
    DataFeedManager (Binance via ccxt)
        → IndicatorEngine (O(1) incremental)
        → AlgoEngine (Strategy → RiskManager)
        → PaperTradingCore (pure execution)

Signal handlers (SIGINT / SIGTERM) save session before exit.
Periodic session save every 300s via asyncio.create_task.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

import pandas as pd

from core.algo_trading import AlgoEngine, TrendFilteredMACrossStrategyV3
from core.bar_log import append_bars, bars_from_dataframe
from core.config import AppConfig
from core.indicators import (
    ATRIndicator,
    EMAIndicator,
    IndicatorEngine,
    SMAIndicator,
)
from core.paper_trading import OrderRequest, PaperTradingCore, PositionSnapshot, TradeResult
from core.session import load_session, save_session
from core.types import BarData, FeedConfig, FeedStateChange, ProviderID
from data.feed import DataFeedManager


# ---------------------------------------------------------------------------
# Structured JSON Logger (headless-specific)
# ---------------------------------------------------------------------------

class _HeadlessJsonFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        # Merge extra fields
        for key in (
            "component", "symbol", "side", "price", "qty", "commission",
            "balance", "pnl", "pnl_pct", "bars", "indicators",
            "trades", "win_rate", "profit_factor", "drawdown",
            "return_pct", "streak", "reason", "elapsed",
            "from_state", "to_state", "error", "provider", "strategy",
            "retry", "backoff_sec", "consecutive_errors", "position",
        ):
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val
        if record.exc_info and record.exc_info[1] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def _configure_headless_logging() -> logging.Logger:
    """Configure root logger with JSON formatter for headless mode."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.INFO)
    handler.setFormatter(_HeadlessJsonFormatter())
    root.addHandler(handler)

    return logging.getLogger("headless")


log = _configure_headless_logging()


# ---------------------------------------------------------------------------
# HeadlessTradingBot
# ---------------------------------------------------------------------------

class HeadlessTradingBot:
    """
    Autonomous trading bot for cloud deployment.

    Lifecycle:
        1. Restore session
        2. Initialize engines (IndicatorEngine, AlgoEngine, PaperTradingCore)
        3. Start DataFeedManager (Binance provider)
        4. Process bars: indicators → strategy → risk → execution
        5. Periodic session save (300s)
        6. Graceful shutdown on SIGINT / SIGTERM
    """

    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self._running: bool = False
        self._feed: DataFeedManager | None = None
        self._shutdown_event: asyncio.Event = asyncio.Event()
        self._last_save_ts: float = time.monotonic()
        self._bar_count: int = 0
        # Latest poll of the still-forming bar (see _on_bar)
        self._forming_bar: BarData | None = None

        # -- Restore session -----------------------------------------------
        session = load_session(
            timeframe_seconds=cfg.default_timeframe.to_seconds()
        )

        # -- Indicator Engine (warm-up via snapshot, then O(1) updates) ----
        self._indicator_engine = IndicatorEngine()
        self._indicator_engine.register(ATRIndicator(period=14))
        self._indicator_engine.register(SMAIndicator(period=200))
        self._indicator_engine.register(SMAIndicator(period=20))
        self._indicator_engine.register(EMAIndicator(period=9))

        # -- Paper Trading Core (no Qt) ------------------------------------
        self._core = PaperTradingCore(
            initial_balance=10_000.0,
            restore=session,
            on_position_updated=self._on_position_updated,
            on_order_filled=self._on_order_filled,
            on_trade_closed=self._on_trade_closed,
        )

        # -- Algo Engine + Strategy ----------------------------------------
        # Circuit breaker baseline = equity, not cash (a restored open
        # position has its cost already deducted from the cash balance).
        pos = self._core.position
        start_equity = self._core.balance
        if pos.side == "LONG":
            start_equity += pos.size * pos.entry_price
        elif pos.side == "SHORT":
            start_equity -= pos.size * pos.entry_price

        # Same strategy and parameters as BacktestEngine / run_comparison.py
        self._algo_engine = AlgoEngine(
            risk_pct=0.02,
            cooldown_sec=180.0,
            initial_session_balance=start_equity,
        )
        strategy = TrendFilteredMACrossStrategyV3(
            symbol=str(cfg.default_symbol),
        )
        self._algo_engine.set_strategy(strategy)
        self._algo_engine.enable()

        # -- Performance tracking (O(1) incremental) -----------------------
        self._perf: dict[str, Any] = {
            "trades": 0,
            "wins": 0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "peak_balance": start_equity,
            "max_drawdown_pct": 0.0,
            "streak": 0,
        }

        # Rehydrate from trade log
        if session and session.trade_log:
            for trade in session.trade_log:
                if "closed_pnl" in trade:
                    self._update_perf(trade)

        log.info(
            "bot_started",
            extra={
                "component": "headless",
                "symbol": str(cfg.default_symbol),
                "balance": round(self._core.balance, 2),
                "trades": self._perf["trades"],
            },
        )

    # -- Callbacks (Headless Adapter) --------------------------------------

    def _on_position_updated(self, snapshot: PositionSnapshot) -> None:
        pass  # Only log trade events, not every tick update

    def _on_order_filled(self, result: TradeResult) -> None:
        event = "trade_open" if result.side == "BUY" else "trade_close"
        log.info(
            event,
            extra={
                "component": "headless",
                "symbol": str(self._cfg.default_symbol),
                "side": result.side,
                "price": round(result.fill_price, 2),
                "qty": round(result.fill_quantity, 6),
                "commission": round(result.commission, 4),
                "balance": round(self._core.balance, 2),
            },
        )

    def _on_trade_closed(self, trade: dict[str, Any]) -> None:
        self._update_perf(trade)
        log.info(
            "trade_close",
            extra={
                "component": "headless",
                "pnl": round(trade.get("closed_pnl", 0.0), 4),
                "pnl_pct": round(trade.get("pnl_pct", 0.0), 4),
                "balance": round(trade.get("balance_after", 0.0), 2),
            },
        )
        self._emit_performance_log()

    def _update_perf(self, trade: dict[str, Any]) -> None:
        """O(1) incremental performance metric update."""
        pnl = trade.get("closed_pnl", 0.0)
        self._perf["trades"] += 1
        if pnl > 0:
            self._perf["wins"] += 1
            self._perf["gross_profit"] += pnl
            if self._perf["streak"] < 0:
                self._perf["streak"] = 0
            self._perf["streak"] += 1
        elif pnl < 0:
            self._perf["gross_loss"] += abs(pnl)
            if self._perf["streak"] > 0:
                self._perf["streak"] = 0
            self._perf["streak"] -= 1

        balance = trade.get("balance_after", self._core.balance)
        if balance > self._perf["peak_balance"]:
            self._perf["peak_balance"] = balance
        if self._perf["peak_balance"] > 0:
            dd = (self._perf["peak_balance"] - balance) / self._perf["peak_balance"]
            if dd > self._perf["max_drawdown_pct"]:
                self._perf["max_drawdown_pct"] = dd

    def _emit_performance_log(self) -> None:
        """Emit structured performance snapshot via log.info."""
        t = self._perf["trades"]
        wr = (self._perf["wins"] / t * 100) if t > 0 else 0.0
        gl = self._perf["gross_loss"]
        pf = (
            self._perf["gross_profit"] / gl if gl > 0
            else float("inf") if self._perf["gross_profit"] > 0
            else 0.0
        )
        ret_pct = (
            (self._core.balance - 10_000.0) / 10_000.0 * 100
        )
        log.info(
            "performance_snapshot",
            extra={
                "component": "headless",
                "trades": t,
                "win_rate": round(wr, 2),
                "profit_factor": round(pf, 4),
                "drawdown": round(self._perf["max_drawdown_pct"] * 100, 2),
                "return_pct": round(ret_pct, 4),
                "streak": self._perf["streak"],
            },
        )

    # -- Data Feed Callbacks -----------------------------------------------

    def _on_snapshot(self, df: pd.DataFrame) -> None:
        """Initialize indicators with historical snapshot (also on reconnect)."""
        # Exchanges return the still-forming bar as the last row. Indicators
        # must only see closed bars, exactly like the backtest.
        closed_df = df
        tf_sec = self._cfg.default_timeframe.to_seconds()
        if not df.empty and df.index[-1].timestamp() + tf_sec > time.time():
            closed_df = df.iloc[:-1]
        self._forming_bar = None

        indicator_series = self._indicator_engine.initialize(closed_df)
        # Warm-up bars too, so run_comparison.py can replay from the same state
        append_bars(bars_from_dataframe(closed_df))
        log.info(
            "snapshot_loaded",
            extra={
                "component": "headless",
                "bars": len(closed_df),
                "indicators": list(indicator_series.keys()),
            },
        )
        # Set initial price in core
        if not df.empty:
            first_bar_price = float(df["close"].iloc[-1])
            self._core._last_price = first_bar_price

    def _on_bar(self, bar: BarData) -> None:
        """
        Called on every poll with the still-forming bar.

        - Mark price (PnL, hard/trailing stop) is updated on every poll.
        - Indicators + strategy run once per CLOSED bar, as in the backtest.
          A bar counts as closed when the next one starts; its final values
          are those of the last poll (at most one poll interval old).
        """
        self._core.update_mark_price(bar)

        prev = self._forming_bar
        self._forming_bar = bar
        if prev is not None and bar.timestamp > prev.timestamp:
            self._on_bar_closed(prev)

    def _on_bar_closed(self, bar: BarData) -> None:
        """
        Process a closed bar. All O(1) operations:
            1. IndicatorEngine.update(bar)
            2. AlgoEngine.evaluate(...)
            3. PaperTradingCore.execute_order(order) — fills at the live price
        """
        self._bar_count += 1
        append_bars([bar])

        # Step 1: Update indicators — O(1) per indicator
        indicators = self._indicator_engine.update(bar)

        # Step 2: Strategy → RiskManager evaluation
        order = self._algo_engine.evaluate(
            price=float(bar.close),
            indicators=indicators,
            position=self._core.position,
            balance=self._core.balance,
            timestamp=bar.timestamp.timestamp(),
        )

        # Step 3: Execute order if approved
        if order is not None:
            self._core.execute_order(order)

    def _on_state_change(self, change: FeedStateChange) -> None:
        log.info(
            "feed_state",
            extra={
                "component": "headless",
                "from_state": change.previous.name,
                "to_state": change.current.name,
                "reason": change.reason,
            },
        )

    # -- Session Persistence -----------------------------------------------

    def save(self) -> None:
        """Save current session state to disk."""
        state = self._core.get_session_state()
        save_session(
            balance=state["balance"],
            open_position=state["open_position"],
            trade_log=state["trade_log"],
        )
        log.info(
            "session_saved",
            extra={
                "component": "headless",
                "balance": round(state["balance"], 2),
                "trades": len(state["trade_log"]),
            },
        )

    # -- Main Loop ---------------------------------------------------------

    async def run(self) -> None:
        """Start the bot and run until shutdown signal."""
        self._running = True

        # Configure data feed
        feed_config = FeedConfig(
            symbol=self._cfg.default_symbol,
            timeframe=self._cfg.default_timeframe,
            provider_id=ProviderID.BINANCE,
            snapshot_limit=201,  # 200 closed bars (SMA_200) + the forming one
            poll_interval_sec=5.0,
        )

        self._feed = DataFeedManager(
            config=feed_config,
            on_snapshot=self._on_snapshot,
            on_bar_update=self._on_bar,
            on_state_change=self._on_state_change,
        )

        log.info(
            "feed_starting",
            extra={
                "component": "headless",
                "symbol": str(self._cfg.default_symbol),
                "provider": "binance",
            },
        )

        await self._feed.start()

        # Start periodic save task
        save_task = asyncio.create_task(self._periodic_save())

        # Wait for shutdown signal
        await self._shutdown_event.wait()

        # Cleanup
        save_task.cancel()
        try:
            await save_task
        except asyncio.CancelledError:
            pass

        if self._feed is not None:
            await self._feed.stop()

        # Final save
        self.save()
        log.info("bot_stopped", extra={"component": "headless"})

    async def _periodic_save(self) -> None:
        """Save session every 300 seconds (5 min — minimize data loss on Codespace shutdown)."""
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=300.0,
                )
                break  # shutdown requested
            except asyncio.TimeoutError:
                self._emit_heartbeat()
                self.save()
                self._emit_performance_log()

    def _emit_heartbeat(self) -> None:
        """Periodic proof of life: last price and closed bars processed."""
        log.info(
            "heartbeat",
            extra={
                "component": "headless",
                "price": round(self._core._last_price, 2),
                "bars": self._bar_count,
                "position": self._core.position.side,
                "balance": round(self._core.balance, 2),
            },
        )

    def request_shutdown(self) -> None:
        """Request graceful shutdown (called from signal handler)."""
        log.info("shutdown_requested", extra={"component": "headless"})
        self._shutdown_event.set()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run() -> None:
    """Async entry point. Installs signal handlers and runs the bot."""
    cfg = AppConfig()
    bot = HeadlessTradingBot(cfg)

    # Install signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, bot.request_shutdown)
        except NotImplementedError:
            # Windows: signal handlers not supported in asyncio
            # Fallback: use signal.signal for SIGINT (KeyboardInterrupt)
            signal.signal(sig, lambda s, f: bot.request_shutdown())

    await bot.run()


def _acquire_instance_lock() -> TextIO | None:
    """
    Hold an exclusive lock for the whole process lifetime so two bots never
    write the same session.json (e.g. watchdog + a manual `python headless_main.py`).

    Returns the open lock file (keep a reference), or None if another
    instance already holds it. Unix only; a no-op elsewhere.
    """
    try:
        import fcntl
    except ImportError:
        return open(os.devnull, "w")

    lock_file = open(Path(__file__).resolve().parent / "session.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return None
    return lock_file


def main() -> None:
    """Synchronous entry point."""
    lock = _acquire_instance_lock()
    if lock is None:
        log.error(
            "another_instance_running — exiting",
            extra={"component": "headless", "reason": "session.lock is held"},
        )
        sys.exit(0)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt — exiting", extra={"component": "headless"})
    sys.exit(0)


if __name__ == "__main__":
    main()
