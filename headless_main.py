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
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO

import pandas as pd

from core.algo_trading import AlgoEngine
from core.strategy_factory import build_indicator_engine, create_strategy
from core.bar_log import BAR_LOG_PATH, append_bars, bars_from_dataframe, new_run_id
from core.config import AppConfig
from core.paper_trading import OrderRequest, PaperTradingCore, PositionSnapshot, TradeResult
from core.session import SESSION_PATH, load_session, save_session
from core.strategy_params import InvalidParams, StrategyParams, load_params
from core.types import BarData, FeedConfig, FeedStateChange, ProviderID, Timeframe
from data.feed import DataFeedManager


# ---------------------------------------------------------------------------
# Structured JSON Logger (headless-specific)
# ---------------------------------------------------------------------------

# Attributes every LogRecord has; anything else on a record came from extra={...}
_STANDARD_RECORD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime"}


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
        # Every extra field (a fixed list silently dropped new ones)
        for key, val in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS and key not in entry and val is not None:
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

    def __init__(self, cfg: AppConfig, params: StrategyParams | None = None) -> None:
        # Strategy parameters (strategy.json) also choose the timeframe
        self._params: StrategyParams = params or load_params()
        cfg = replace(cfg, default_timeframe=Timeframe(self._params.timeframe))
        self._cfg = cfg
        self._running: bool = False
        self._feed: DataFeedManager | None = None
        self._shutdown_event: asyncio.Event = asyncio.Event()
        self._last_save_ts: float = time.monotonic()
        self._bar_count: int = 0
        # Latest poll of the still-forming bar (see _on_bar)
        self._forming_bar: BarData | None = None
        # live_bars.csv segment: "<run>:<snapshot n>" (see core/bar_log.py)
        self._run_id: str = new_run_id()
        self._snapshot_no: int = 0
        self._seg: str = ""

        # -- Restore session -----------------------------------------------
        session_existed = SESSION_PATH.exists()
        session = load_session(
            timeframe_seconds=cfg.default_timeframe.to_seconds()
        )
        if session is None and session_existed:
            # Unreadable session.json (moved aside by load_session): don't
            # mix the old live_bars.csv into the fresh session
            archive = _archive_run()
            log.error(
                "session_unreadable — starting fresh",
                extra={"component": "headless", "reason": str(archive)},
            )
        if session is not None and self._params_changed(session.meta):
            # Different parameters = a different experiment: start a clean
            # session so live results and run_comparison.py stay meaningful.
            archive = _archive_run()
            log.warning(
                "strategy_params_changed — previous session archived, starting fresh",
                extra={"component": "headless", "reason": str(archive)},
            )
            session = None

        log.info(
            "strategy_params",
            extra={"component": "headless", "reason": json.dumps(self._params.to_dict())},
        )

        # -- Indicator Engine (warm-up via snapshot, then O(1) updates) ----
        self._indicator_engine = build_indicator_engine(self._params)

        # -- Paper Trading Core (no Qt) ------------------------------------
        self._core = PaperTradingCore(
            initial_balance=10_000.0,
            restore=session,
            on_position_updated=self._on_position_updated,
            on_order_filled=self._on_order_filled,
            on_trade_closed=self._on_trade_closed,
            trail_activation_pct=self._params.trail_activation_pct,
        )

        # -- Algo Engine + Strategy ----------------------------------------
        # Circuit breaker state survives restarts (session meta), so a
        # restart neither clears a tripped breaker nor moves its baseline.
        # Baseline = equity, not cash (a restored open position has its cost
        # already deducted from the cash balance).
        breaker = (session.meta.get("breaker") if session else None) or {}
        pos = self._core.position
        start_equity = self._core.balance
        if pos.side == "LONG":
            start_equity += pos.size * pos.entry_price
        elif pos.side == "SHORT":
            start_equity -= pos.size * pos.entry_price
        baseline = float(breaker.get("baseline", start_equity))

        # Same strategy and parameters as BacktestEngine / run_comparison.py
        # Risk per trade comes from the params (strategy.json risk_pct)
        self._algo_engine = AlgoEngine(
            cooldown_sec=180.0,
            initial_session_balance=baseline,
            params=self._params,
            halted=bool(breaker.get("halted", False)),
        )
        strategy = create_strategy(str(cfg.default_symbol), self._params)
        self._algo_engine.set_strategy(strategy)
        self._algo_engine.enable()

        # -- Performance tracking (O(1) incremental) -----------------------
        # Every session starts at 10,000 (a parameter change archives the
        # old one), and the trade log replay below rebuilds the real peak.
        self._perf: dict[str, Any] = {
            "trades": 0,
            "wins": 0,
            "gross_profit": 0.0,   # net of fees, despite the name
            "gross_loss": 0.0,     # net of fees, despite the name
            "peak_balance": 10_000.0,
            "max_drawdown_pct": 0.0,
            "streak": 0,
            "last_close_balance": 10_000.0,
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
                "strategy": self._params.strategy,
                "timeframe": self._params.timeframe,
                "risk_pct": self._params.risk_pct,
                "balance": round(self._core.balance, 2),
                "trades": self._perf["trades"],
            },
        )

    def _params_changed(self, meta: dict[str, Any]) -> bool:
        """Did the saved session run with other strategy parameters?"""
        stored = meta.get("strategy_params")
        if stored is None:
            # Sessions from before parameters were recorded used the defaults
            return self._params != StrategyParams()
        return StrategyParams.from_dict(stored) != self._params

    # -- Callbacks (Headless Adapter) --------------------------------------

    def _on_position_updated(self, snapshot: PositionSnapshot) -> None:
        pass  # Only log trade events, not every tick update

    def _on_order_filled(self, result: TradeResult) -> None:
        # A SELL fill is logged as "order_filled"; "trade_close" (with PnL)
        # comes from _on_trade_closed, once per trade
        event = "trade_open" if result.side == "BUY" else "order_filled"
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
        # Persist every fill right away: a Codespace can be stopped without
        # a graceful SIGTERM, and the 5-min periodic save would lose trades.
        self.save()

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
        """
        O(1) incremental performance metric update, net of fees: each trade
        starts and ends flat, so the change in cash between consecutive
        closes is exactly its result after both commissions.
        """
        balance_after = trade.get("balance_after", self._core.balance)
        pnl = balance_after - self._perf["last_close_balance"]
        self._perf["last_close_balance"] = balance_after
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

        self._track_drawdown(balance_after)

    def _track_drawdown(self, equity: float) -> None:
        """Max drawdown from closed balances and periodic equity samples."""
        if equity > self._perf["peak_balance"]:
            self._perf["peak_balance"] = equity
        if self._perf["peak_balance"] > 0:
            dd = (self._perf["peak_balance"] - equity) / self._perf["peak_balance"]
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
        # Equity, not cash: cash drops by the position cost while in a trade
        ret_pct = (self._equity() - 10_000.0) / 10_000.0 * 100
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

    def _equity(self) -> float:
        """Cash + market value of the open position."""
        pos = self._core.position
        price = self._core._last_price or pos.entry_price
        equity = self._core.balance
        if pos.side == "LONG":
            equity += pos.size * price
        elif pos.side == "SHORT":
            equity -= pos.size * price
        return equity

    # -- Data Feed Callbacks -----------------------------------------------

    def _on_snapshot(self, df: pd.DataFrame) -> None:
        """Initialize indicators with historical snapshot (also on reconnect)."""
        # Exchanges return the still-forming bar as the last row. Indicators
        # must only see closed bars, exactly like the backtest.
        closed_df = df
        self._forming_bar = None
        tf_sec = self._cfg.default_timeframe.to_seconds()
        if not df.empty and df.index[-1].timestamp() + tf_sec > time.time():
            closed_df = df.iloc[:-1]
            # Seed with the forming bar: if the first poll already belongs to
            # the next bar, this one is closed with its snapshot values
            # instead of being skipped.
            forming = bars_from_dataframe(df.iloc[-1:])
            self._forming_bar = forming[0] if forming else None

        indicator_series = self._indicator_engine.initialize(closed_df)
        # Warm-up bars too, so run_comparison.py can replay from the same state
        self._snapshot_no += 1
        self._seg = f"{self._run_id}:{self._snapshot_no}"
        append_bars(bars_from_dataframe(closed_df), kind="warmup", seg=self._seg)
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
        - If whole bars were missed (poll outage), the last poll of the old
          bar is stale too: re-fetch the snapshot instead of trading on it.
        - A lagging response with an older candle is ignored (it would
          otherwise close the same bar twice and mark a stale price).
        """
        if self._forming_bar is not None and bar.timestamp < self._forming_bar.timestamp:
            return
        self._core.update_mark_price(bar)

        prev = self._forming_bar
        self._forming_bar = bar
        if prev is None or bar.timestamp <= prev.timestamp:
            return

        tf = timedelta(seconds=self._cfg.default_timeframe.to_seconds())
        if bar.timestamp - prev.timestamp > tf:
            log.warning(
                "bars_missed — resyncing snapshot",
                extra={
                    "component": "headless",
                    "reason": f"last bar {prev.timestamp.isoformat()}, now {bar.timestamp.isoformat()}",
                },
            )
            self._forming_bar = None
            if self._feed is not None:
                self._feed.request_resync()
            return

        self._on_bar_closed(prev)

    def _on_bar_closed(self, bar: BarData) -> None:
        """
        Process a closed bar. All O(1) operations:
            1. IndicatorEngine.update(bar)
            2. AlgoEngine.evaluate(...)
            3. PaperTradingCore.execute_order(order) — fills at the live price
        """
        self._bar_count += 1
        append_bars([bar], kind="live", seg=self._seg)

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
            meta={
                "strategy_params": self._params.to_dict(),
                "breaker": {
                    "baseline": self._algo_engine.initial_session_balance,
                    "halted": self._algo_engine.halted,
                },
            },
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
        """Periodic proof of life: price, bars, equity, breaker, data source."""
        equity = self._equity()
        self._track_drawdown(equity)
        log.info(
            "heartbeat",
            extra={
                "component": "headless",
                "price": round(self._core._last_price, 2),
                "bars": self._bar_count,
                "position": self._core.position.side,
                "balance": round(self._core.balance, 2),
                "equity": round(equity, 2),
                "halted": self._algo_engine.halted,
                "exchange": self._feed.source if self._feed is not None else None,
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


def _archive_run() -> Path:
    """Move session.json and live_bars.csv to archive/<UTC time>/."""
    dest = Path(__file__).resolve().parent / "archive" / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest.mkdir(parents=True, exist_ok=True)
    for path in (SESSION_PATH, BAR_LOG_PATH):
        if path.exists():
            path.replace(dest / path.name)
    return dest


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
    except InvalidParams as e:
        # Never trade with a fallback strategy: stop until strategy.json is fixed.
        # Exit code 2 = configuration error (systemd does not restart on it).
        log.error(
            "invalid_strategy_json — bot NOT started; fix strategy.json and restart",
            extra={"component": "headless", "error": str(e)},
        )
        sys.exit(2)
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt — exiting", extra={"component": "headless"})
    sys.exit(0)


if __name__ == "__main__":
    main()
