"""
core/backtest_engine.py
------------------------
Backtesting engine for strategy validation using historical data.

Principle: ZERO duplicated logic.
    Reuses the exact same production components:
    - IndicatorEngine (SMA, EMA with O(1) incremental updates)
    - AlgoEngine (Strategy → RiskManager → Order)
    - PaperTradingCore (execution, position tracking, PnL)

The BacktestEngine only simulates the flow of market data.

Lookahead bias prevention:
    At any bar t, the engine has only seen bars ≤ t.
    - Indicators are initialized with warmup bars [0, W)
    - Simulation processes bars [W, N) sequentially
    - Each bar is fed to IndicatorEngine.update() BEFORE strategy evaluation
    - The strategy's on_tick() receives ONLY current price and current indicator values
    - No future data is ever accessible to the decision pipeline
    - Orders signalled at the close of bar t fill at the open of bar t+1
      (same timing as the live bot), never at an earlier price

Overfitting mitigation:
    - Use out-of-sample testing: split data 70/30 (train/test)
    - Compare in-sample vs out-of-sample metrics
    - If performance degrades significantly → strategy is overfit

Walk-forward analysis evolution:
    - Split data into rolling windows [train | test]
    - Optimize on train, validate on test
    - Slide window forward, repeat
    - Aggregate out-of-sample results for robust performance estimate

Performance:
    - Pure synchronous loop (no asyncio, no sleep)
    - No pandas inside the main loop (only in warmup initialization)
    - No DataFrame recreation per tick
    - Processes thousands of bars in seconds
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd

from core.algo_trading import AlgoEngine, TrendFilteredMACrossStrategyV3
from core.indicators import EMAIndicator, IndicatorEngine, SMAIndicator, ATRIndicator
from core.paper_trading import OrderRequest, PaperTradingCore
from core.types import BarData
from utils.logger import get_logger

log = get_logger(__name__, component="backtest")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TradeLog:
    """Immutable record of a completed round-trip trade."""
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    profit: float
    side: str
    exit_reason: str = "unknown"
    position_usd: float = 0.0
    balance_after: float = 0.0  # cash after the closing fill (net of fees)


@dataclass(frozen=True, slots=True)
class ReplaySegment:
    """
    One stretch of live operation between two snapshots of the live bot.

    warmup:  bars only used to (re)initialize indicators — the bot did not
             trade them (e.g. bars from while it was offline).
    bars:    bars the bot processed live, in order.
    new_run: True when the bot process restarted (fresh strategy and
             AlgoEngine state); False for a feed reconnect in the same run.
    """
    warmup: list[BarData]
    bars: list[BarData]
    new_run: bool = True


@dataclass(slots=True)
class BacktestResult:
    """Aggregated backtest performance metrics."""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    initial_balance: float = 0.0
    final_balance: float = 0.0
    total_return_pct: float = 0.0
    total_profit: float = 0.0
    total_loss: float = 0.0
    profit_per_trade_pct: float = 0.0
    avg_winner_pct: float = 0.0
    avg_loser_pct: float = 0.0
    sharpe_ratio: float = 0.0
    calmar_ratio: float = 0.0
    recovery_factor: float = 0.0
    max_consecutive_losses: int = 0
    stops_hit_count: int = 0
    momentum_exits_count: int = 0
    trailing_exits_count: int = 0
    avg_position_usd: float = 0.0
    max_position_usd: float = 0.0
    trades_per_month_avg: float = 0.0
    low_sample_warning: bool = False
    data_source: str = ""  # "preloaded" | "ccxt/binance" | "synthetic"
    equity_curve: list[float] = field(default_factory=list)
    trade_log: list[TradeLog] = field(default_factory=list)

    def summary(self) -> str:
        """Multi-line summary for logging."""
        lines = [
            "=" * 60,
            "BACKTEST RESULTS",
            "=" * 60,
            f"  Total Trades:    {self.total_trades}",
            f"  Winning:         {self.winning_trades}",
            f"  Losing:          {self.losing_trades}",
            f"  Win Rate:        {self.win_rate:.1%}",
            f"  Stops Hit:       {self.stops_hit_count}",
            f"  Trailing Exits:  {self.trailing_exits_count}",
            f"  Momentum Exits:  {self.momentum_exits_count}",
            f"  Profit Factor:   {self.profit_factor:.2f}",
            f"  Sharpe Ratio:    {self.sharpe_ratio:.2f}",
            f"  Calmar Ratio:    {self.calmar_ratio:.2f}",
            f"  Recovery Factor: {self.recovery_factor:.2f}",
            f"  Max Cons. Loses: {self.max_consecutive_losses}",
            f"  Max Drawdown:    {self.max_drawdown:.2%}",
            f"  Trades/Month:    {self.trades_per_month_avg:.1f}",
            f"  Sample Warning:  {'YES' if self.low_sample_warning else 'NO'}",
            f"  Data Source:     {self.data_source}",
            f"  Initial Balance: ${self.initial_balance:,.2f}",
            f"  Final Balance:   ${self.final_balance:,.2f}",
            f"  Total Return:    {self.total_return_pct:.2%}",
            f"  Total Profit:    ${self.total_profit:,.2f}",
            f"  Total Loss:      ${self.total_loss:,.2f}",
            f"  Avg Winner:      {self.avg_winner_pct:.2%}",
            f"  Avg Loser:       {self.avg_loser_pct:.2%}",
            f"  Avg Trade PnL:   {self.profit_per_trade_pct:.2%}",
            f"  Avg Pos. USD:    ${self.avg_position_usd:,.2f}",
            f"  Max Pos. USD:    ${self.max_position_usd:,.2f}",
            "=" * 60,
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Simulated clock for backtesting
# ---------------------------------------------------------------------------

class _SimulatedClock:
    """
    Injectable clock that tracks simulated market time.

    In production, strategy uses time.monotonic() for cooldown.
    In backtest, this clock returns the current bar's timestamp
    so cooldown works in simulated time (not wall-clock time).
    """

    def __init__(self) -> None:
        self._time: float = 0.0

    def __call__(self) -> float:
        return self._time

    def set(self, t: float) -> None:
        self._time = t


# ---------------------------------------------------------------------------
# Data generation (synthetic fallback)
# ---------------------------------------------------------------------------

def _generate_synthetic_bars(
    symbol: str,
    start_date: datetime,
    end_date: datetime,
    timeframe_minutes: int = 1,
    initial_price: float = 50_000.0,
) -> list[BarData]:
    """
    Generate synthetic OHLCV bars with realistic random walk.

    Used as fallback when ccxt/Binance is unavailable.
    Produces reasonable price action with trends, mean-reversion,
    and volume variation — NOT a flat random walk.
    """
    bars: list[BarData] = []
    price = initial_price
    dt = start_date
    delta = timedelta(minutes=timeframe_minutes)

    # Random walk with slight drift and momentum
    momentum: float = 0.0

    while dt < end_date:
        # Price movement with momentum (creates trends)
        drift = random.gauss(0.0, 0.001)  # ~0.1% per bar
        momentum = 0.7 * momentum + 0.3 * drift  # Smoothed
        change = 1.0 + momentum

        o = price
        # Intra-bar volatility
        h = o * (1.0 + abs(random.gauss(0.0, 0.003)))
        l = o * (1.0 - abs(random.gauss(0.0, 0.003)))
        c = o * change
        # Ensure high/low contain open/close
        h = max(h, o, c)
        l = min(l, o, c)
        v = random.uniform(10.0, 500.0)

        bars.append(BarData(
            timestamp=dt,
            open=Decimal(str(round(o, 2))),
            high=Decimal(str(round(h, 2))),
            low=Decimal(str(round(l, 2))),
            close=Decimal(str(round(c, 2))),
            volume=Decimal(str(round(v, 4))),
        ))

        price = c
        dt += delta

    return bars


def _load_data_ccxt(
    symbol: str,
    timeframe: str,
    start_date: datetime,
    end_date: datetime,
    timeframe_minutes: int = 1,
) -> tuple[list[BarData], str]:
    """
    Load historical OHLCV via ccxt (synchronous) with pagination.

    Falls back to synthetic data if ccxt/Binance is unavailable.
    Returns (bars, source) where source is "ccxt/binance" or "synthetic".

    Pagination: Binance allows max 1000 candles per request.
    Rate limiting: ccxt handles this internally with enableRateLimit.
    """
    try:
        import ccxt as ccxt_sync

        exchange = ccxt_sync.binance({"enableRateLimit": True})

        since_ms = int(start_date.timestamp() * 1000)
        until_ms = int(end_date.timestamp() * 1000)

        all_ohlcv: list[list] = []
        current_ms = since_ms

        log.info(
            "Loading historical data via ccxt",
            extra={
                "symbol": symbol,
                "timeframe": timeframe,
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
            },
        )

        while current_ms < until_ms:
            ohlcv = exchange.fetch_ohlcv(
                symbol, timeframe, since=current_ms, limit=1000,
            )
            if not ohlcv:
                break

            # Filter out bars beyond end_date
            for row in ohlcv:
                if row[0] <= until_ms:
                    all_ohlcv.append(row)

            # Advance to next page
            current_ms = ohlcv[-1][0] + 1

            if len(ohlcv) < 1000:
                break  # No more data

            log.debug(
                "Fetched page",
                extra={"bars": len(ohlcv), "total": len(all_ohlcv)},
            )

        exchange.close()

        if not all_ohlcv:
            log.warning("No data from ccxt — falling back to synthetic data")
            return (
                _generate_synthetic_bars(symbol, start_date, end_date, timeframe_minutes),
                "synthetic",
            )

        # Convert to BarData
        bars: list[BarData] = []
        for row in all_ohlcv:
            ts = datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc)
            bars.append(BarData(
                timestamp=ts,
                open=Decimal(str(row[1])),
                high=Decimal(str(row[2])),
                low=Decimal(str(row[3])),
                close=Decimal(str(row[4])),
                volume=Decimal(str(row[5])),
            ))

        log.info(
            "Historical data loaded",
            extra={"bars": len(bars), "source": "ccxt/binance"},
        )
        return bars, "ccxt/binance"

    except Exception as exc:
        log.warning(
            "ccxt fetch failed — using synthetic data",
            extra={"error": str(exc)},
        )
        return (
            _generate_synthetic_bars(symbol, start_date, end_date, timeframe_minutes),
            "synthetic",
        )


# ---------------------------------------------------------------------------
# DataFrame conversion (used ONLY for indicator warmup, NOT in main loop)
# ---------------------------------------------------------------------------

def _bars_to_dataframe(bars: list[BarData]) -> pd.DataFrame:
    """
    Convert BarData list to OHLCV DataFrame for indicator initialization.

    Called ONCE before the simulation loop. No pandas inside the main loop.
    """
    data = {
        "open":   [float(b.open) for b in bars],
        "high":   [float(b.high) for b in bars],
        "low":    [float(b.low) for b in bars],
        "close":  [float(b.close) for b in bars],
        "volume": [float(b.volume) for b in bars],
    }
    index = pd.DatetimeIndex([b.timestamp for b in bars], name="timestamp")
    return pd.DataFrame(data, index=index)


# ---------------------------------------------------------------------------
# BacktestEngine
# ---------------------------------------------------------------------------

class BacktestEngine:
    """
    Backtesting engine that simulates market flow through production components.

    Reuses EXACTLY:
        - IndicatorEngine (incremental O(1) updates)
        - AlgoEngine (strategy + risk manager)
        - PaperTradingCore (execution + position tracking)

    The engine only controls the flow of bars — it does not contain
    any trading logic. All decisions are made by the same code path
    that runs in production.

    Usage::

        engine = BacktestEngine(
            symbol="BTC/USDT",
            timeframe="1h",
            start_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
            end_date=datetime(2024, 12, 31, tzinfo=timezone.utc),
        )
        result = engine.run()
        print(result.summary())
    """

    # Number of bars for indicator warmup (must >= max indicator period)
    _WARMUP_BARS: int = 210

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "1h",
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        initial_balance: float = 10_000.0,
        trade_size: float = 0.01,
        cooldown_sec: float = 180.0,
        risk_pct: float = 0.02,
        use_ccxt: bool = True,
        bars: list[BarData] | None = None,
        warmup_bars: int | None = None,
        segments: list[ReplaySegment] | None = None,
    ) -> None:
        """
        Args:
            bars: Pre-loaded bars. When given, no data is downloaded and
                start_date/end_date are ignored.
            warmup_bars: Bars used only to initialize indicators
                (default _WARMUP_BARS). The simulation starts after them.
            segments: Replay of a live run recorded by the bot
                (core/bar_log.load_segments). Takes precedence over bars.
        """
        self._symbol = symbol
        self._timeframe = timeframe
        self._start_date = start_date or datetime(2024, 1, 1, tzinfo=timezone.utc)
        self._end_date = end_date or datetime(2024, 7, 1, tzinfo=timezone.utc)
        self._initial_balance = initial_balance
        self._trade_size = trade_size
        self._cooldown_sec = cooldown_sec
        self._risk_pct = risk_pct
        self._use_ccxt = use_ccxt
        self._preloaded_bars = bars
        self._segments = segments
        self._warmup_bars = warmup_bars if warmup_bars is not None else self._WARMUP_BARS

    def run(self) -> BacktestResult:
        """
        Execute the full backtest pipeline.

        1. Load historical data (ccxt or synthetic fallback)
        2. Split into warmup + simulation periods
        3. Initialize components (reusing production code)
        4. Run synchronous simulation loop
        5. Calculate and return metrics

        Returns:
            BacktestResult with all metrics, equity curve, and trade log.
        """
        log.info(
            "Backtest starting",
            extra={
                "symbol": self._symbol,
                "timeframe": self._timeframe,
                "start": self._start_date.isoformat(),
                "end": self._end_date.isoformat(),
                "balance": self._initial_balance,
            },
        )

        # 1. Load data. Warm-up bars are fetched BEFORE start_date so the
        #    simulated (traded) period is exactly [start_date, end_date].
        tf_minutes = self._timeframe_to_minutes()
        if self._segments is not None:
            segments = [seg for seg in self._segments if seg.warmup and seg.bars]
            if not segments:
                log.error("No replayable segments (need warm-up and live bars)")
                return BacktestResult(
                    initial_balance=self._initial_balance,
                    data_source="live_replay",
                )
            result = self._simulate_segments(segments)
            result.data_source = "live_replay"
            self._log_results(result)
            return result

        if self._preloaded_bars is not None:
            bars = list(self._preloaded_bars)
            data_source = "preloaded"
        else:
            data_start = self._start_date - timedelta(minutes=tf_minutes * self._warmup_bars)
            if self._use_ccxt:
                bars, data_source = _load_data_ccxt(
                    self._symbol, self._timeframe,
                    data_start, self._end_date,
                    timeframe_minutes=tf_minutes,
                )
            else:
                bars = _generate_synthetic_bars(
                    self._symbol, data_start, self._end_date,
                    timeframe_minutes=tf_minutes,
                )
                data_source = "synthetic"

        if len(bars) < self._warmup_bars + 10:
            log.error(
                "Insufficient data for backtest",
                extra={"bars": len(bars), "minimum": self._warmup_bars + 10},
            )
            return BacktestResult(
                initial_balance=self._initial_balance,
                data_source=data_source,
            )

        log.info(
            "Data loaded",
            extra={"total_bars": len(bars), "warmup": self._warmup_bars, "source": data_source},
        )

        # 2. Run simulation
        result = self._simulate(bars)
        result.data_source = data_source

        # 3. Log results
        self._log_results(result)

        return result

    # ------------------------------------------------------------------
    # Simulation loop (synchronous, no asyncio, no sleep, no pandas)
    # ------------------------------------------------------------------

    def _simulate(self, bars: list[BarData]) -> BacktestResult:
        """Classic backtest: one warm-up block followed by the simulated bars."""
        return self._simulate_segments([ReplaySegment(
            warmup=bars[:self._warmup_bars],
            bars=bars[self._warmup_bars:],
            new_run=True,
        )])

    def _simulate_segments(self, segments: list[ReplaySegment]) -> BacktestResult:
        """
        Core simulation loop. Pure synchronous execution.

        No pandas inside this loop. No sleep(). No asyncio.
        All trading decisions are made by the same production components.

        Each segment mirrors a live (re)start: indicators are re-initialized
        from its warm-up bars (never traded), and on a new run the strategy
        and AlgoEngine are rebuilt like a fresh process. Balance and the open
        position carry over, as they do through session.json.
        """
        # Simulated clock for strategy cooldown (uses bar timestamps)
        sim_clock = _SimulatedClock()

        # Same indicator engine as production
        indicator_engine = IndicatorEngine()
        indicator_engine.register(ATRIndicator(period=14))
        indicator_engine.register(SMAIndicator(period=200))
        indicator_engine.register(SMAIndicator(period=20))
        indicator_engine.register(EMAIndicator(period=9))

        # Same paper trading engine as production
        paper_engine = PaperTradingCore(initial_balance=self._initial_balance)
        algo_engine: AlgoEngine | None = None

        # --- Simulation state ---
        trade_log: list[TradeLog] = []
        equity_curve: list[float] = [self._initial_balance]
        current_entry: dict | None = None
        peak_equity: float = self._initial_balance
        max_drawdown: float = 0.0

        def track_position_change(
            prev_side: str,
            prev_realized: float,
            was_trailing_active: bool,
            bar: BarData,
            algo_closed: bool,
        ) -> None:
            """Record an opened/closed position after a fill or a mark-price update."""
            nonlocal current_entry
            pos = paper_engine.position

            if prev_side == "FLAT" and pos.side != "FLAT":
                current_entry = {
                    "time": bar.timestamp,
                    "price": pos.entry_price,
                    "side": pos.side,
                    "position_usd": pos.size * pos.entry_price,
                }
            elif prev_side != "FLAT" and pos.side == "FLAT" and current_entry is not None:
                if algo_closed:
                    exit_reason = "momentum"
                elif was_trailing_active:
                    exit_reason = "trailing"
                else:
                    exit_reason = "hard_stop"

                trade_log.append(TradeLog(
                    entry_time=current_entry["time"],
                    exit_time=bar.timestamp,
                    entry_price=current_entry["price"],
                    exit_price=float(paper_engine.trade_log[-1]["price"]),
                    profit=pos.realized_pnl - prev_realized,
                    side=current_entry["side"],
                    exit_reason=exit_reason,
                    position_usd=current_entry["position_usd"],
                    balance_after=paper_engine.balance,
                ))
                current_entry = None

        def mark(bar: BarData) -> None:
            """Mark price → PnL, trailing activation, hard/trailing stops."""
            prev_side = paper_engine.position.side
            prev_realized = paper_engine.position.realized_pnl
            was_trailing_active = paper_engine.position.trailing_active
            paper_engine.update_mark_price(bar)
            track_position_change(
                prev_side, prev_realized, was_trailing_active, bar,
                algo_closed=False,
            )

        def close_bar(bar: BarData, fill_price: float, fill_bar: BarData, allow_entry: bool = True) -> None:
            """
            Bar t has closed: indicators + strategy on it, and the order (if any)
            fills right away at fill_price — the open of bar t+1, like the live
            bot, which does this on the first poll of the next bar.
            """
            assert algo_engine is not None
            indicators = indicator_engine.update(bar)
            order = algo_engine.evaluate(
                price=float(bar.close),
                indicators=indicators,
                position=paper_engine.position,
                balance=paper_engine.balance,
                timestamp=bar.timestamp.timestamp(),
            )
            if order is None or (order.side == "BUY" and not allow_entry):
                return
            prev_side = paper_engine.position.side
            prev_realized = paper_engine.position.realized_pnl
            paper_engine._last_price = fill_price
            paper_engine.execute_order(order)
            track_position_change(
                prev_side, prev_realized, False, fill_bar,
                algo_closed=order.side == "SELL",
            )

        def open_only(bar: BarData) -> BarData:
            """The first poll of a bar: only its open price is known."""
            o = bar.open
            return BarData(timestamp=bar.timestamp, open=o, high=o, low=o, close=o, volume=0.0)

        tf = timedelta(minutes=self._timeframe_to_minutes())
        unclosed: BarData | None = None  # last bar seen; closes when the next one opens

        for segment in segments:
            # The previous segment's last bar was closed by the first poll of
            # the next bar, before the bot stopped/reconnected.
            if unclosed is not None:
                nxt = next(
                    (b for b in segment.warmup + segment.bars
                     if b.timestamp == unclosed.timestamp + tf),
                    None,
                )
                if nxt is not None:
                    mark(open_only(nxt))
                    close_bar(unclosed, float(nxt.open), nxt)
                else:
                    close_bar(unclosed, float(unclosed.close), unclosed)
                unclosed = None

            # --- (Re)initialize like the live bot does on (re)start ---
            indicator_engine.initialize(_bars_to_dataframe(segment.warmup))
            if segment.new_run or algo_engine is None:
                # Fresh process: new strategy/AlgoEngine; circuit-breaker
                # baseline = equity (cash + restored position at entry price)
                pos = paper_engine.position
                start_equity = paper_engine.balance
                if pos.side == "LONG":
                    start_equity += pos.size * pos.entry_price
                elif pos.side == "SHORT":
                    start_equity -= pos.size * pos.entry_price

                algo_engine = AlgoEngine(
                    risk_pct=self._risk_pct,
                    cooldown_sec=self._cooldown_sec,
                    initial_session_balance=start_equity,
                )
                algo_engine.set_strategy(TrendFilteredMACrossStrategyV3(symbol=self._symbol))
                algo_engine.enable()

            # Set price reference in paper engine (live: last snapshot close)
            paper_engine._last_price = float(segment.warmup[-1].close)

            # --- Main loop (NO pandas, NO sleep, NO asyncio) ---
            # Same order of events as the live bot (headless_main.py), whose
            # first poll of bar t+1 checks stops at the open, then closes bar t
            # (signal) and fills at that price; its last poll ≈ the close.
            # Never fills at a price from before the signal existed.
            for bar in segment.bars:
                # Advance simulated clock to bar timestamp
                sim_clock.set(bar.timestamp.timestamp())

                # Step 1: first poll — stops at the open
                mark(open_only(bar))

                # Step 2: previous bar closes → strategy → fill at this open
                if unclosed is not None:
                    close_bar(unclosed, float(bar.open), bar)

                # Step 3: last poll — stops / trailing at the close
                mark(bar)
                unclosed = bar

                # Step 4: Track equity (cash + market value of the open position)
                pos = paper_engine.position
                equity = paper_engine.balance
                if pos.side == "LONG":
                    equity += pos.size * float(bar.close)
                elif pos.side == "SHORT":
                    equity -= pos.size * float(bar.close)
                equity_curve.append(equity)

                # Step 5: Track max drawdown
                if equity > peak_equity:
                    peak_equity = equity
                if peak_equity > 0:
                    dd = (peak_equity - equity) / peak_equity
                    if dd > max_drawdown:
                        max_drawdown = dd

        # The last bar's signal would fill at the next open, which is not in
        # the data. Exits fill at the last close (≈ next open); entries are
        # dropped (they could not be evaluated afterwards anyway).
        last_bar = unclosed
        if unclosed is not None:
            close_bar(unclosed, float(unclosed.close), unclosed, allow_entry=False)

        # --- Force-close any open position at end of simulation ---
        if last_bar is not None and paper_engine.position.side != "FLAT" and paper_engine.position.size > 0:
            close_order = OrderRequest(
                symbol=self._symbol,
                side="SELL",
                quantity=paper_engine.position.size,
                price=None,
                order_type="MARKET",
            )
            prev_realized = paper_engine.position.realized_pnl
            paper_engine.execute_order(close_order)

            if current_entry is not None:
                profit = paper_engine.position.realized_pnl - prev_realized
                trade_log.append(TradeLog(
                    entry_time=current_entry["time"],
                    exit_time=last_bar.timestamp,
                    entry_price=current_entry["price"],
                    exit_price=float(last_bar.close),
                    profit=profit,
                    side=current_entry["side"],
                    exit_reason="end_of_data",
                    position_usd=current_entry["position_usd"],
                    balance_after=paper_engine.balance,
                ))

        # --- Calculate metrics ---
        return self._calculate_metrics(
            trade_log=trade_log,
            equity_curve=equity_curve,
            max_drawdown=max_drawdown,
            final_balance=paper_engine.balance,
        )

    # ------------------------------------------------------------------
    # Metrics calculation
    # ------------------------------------------------------------------

    def _calculate_metrics(
        self,
        trade_log: list[TradeLog],
        equity_curve: list[float],
        max_drawdown: float,
        final_balance: float,
    ) -> BacktestResult:
        """Calculate all performance metrics from trade log."""
        total = len(trade_log)

        if total == 0:
            return BacktestResult(
                initial_balance=self._initial_balance,
                final_balance=final_balance,
                total_return_pct=(final_balance - self._initial_balance) / self._initial_balance,
                equity_curve=equity_curve,
                trade_log=trade_log,
                max_drawdown=max_drawdown,
            )

        winners = [t for t in trade_log if t.profit > 0]
        losers = [t for t in trade_log if t.profit <= 0]

        total_profit = sum(t.profit for t in winners)
        total_loss = abs(sum(t.profit for t in losers))

        stops_hit = sum(1 for t in trade_log if t.exit_reason == "hard_stop")
        momentum_exits = sum(1 for t in trade_log if t.exit_reason == "momentum")
        trailing_exits = sum(1 for t in trade_log if t.exit_reason == "trailing")

        win_rate = len(winners) / total if total > 0 else 0.0
        profit_factor = total_profit / total_loss if total_loss > 0 else float("inf")

        total_return_pct = (final_balance - self._initial_balance) / self._initial_balance if self._initial_balance > 0 else 0.0

        profit_per_trade_pct = (total_profit - total_loss) / (self._initial_balance * total) if total > 0 else 0.0
        avg_winner_pct = total_profit / (self._initial_balance * len(winners)) if winners else 0.0
        avg_loser_pct = total_loss / (self._initial_balance * len(losers)) if losers else 0.0

        # Calmar Ratio
        calmar_ratio = total_return_pct / max_drawdown if max_drawdown > 0 else float("inf")

        # Absolute max drawdown in USD & Recovery Factor
        peak = self._initial_balance
        max_dd_usd = 0.0
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            dd_usd = peak - eq
            if dd_usd > max_dd_usd:
                max_dd_usd = dd_usd
                
        recovery_factor = total_profit / max_dd_usd if max_dd_usd > 0 else float("inf")

        # Max consecutive losses
        current_losses = 0
        max_losses = 0
        mean_profit = 0.0
        for t in trade_log:
            mean_profit += t.profit
            if t.profit <= 0:
                current_losses += 1
                if current_losses > max_losses:
                    max_losses = current_losses
            else:
                current_losses = 0
        mean_profit /= total

        # Correct Sharpe ratio using percentage returns grouped DAILY
        daily_pnl: dict[str, float] = {}
        for t in trade_log:
            dt = t.exit_time.strftime("%Y-%m-%d")
            if dt not in daily_pnl:
                daily_pnl[dt] = 0.0
            daily_pnl[dt] += t.profit

        daily_returns: list[float] = []
        import logging
        lg = logging.getLogger(__name__)

        if len(trade_log) > 0:
            first_date = trade_log[0].entry_time.date()
            last_date = trade_log[-1].exit_time.date()
            days_total = (last_date - first_date).days
            
            # Fill all calendar days with 0.0 if no trades closed
            curr = first_date
            while curr <= last_date:
                dt_str = curr.strftime("%Y-%m-%d")
                profit = daily_pnl.get(dt_str, 0.0)
                daily_returns.append(profit / self._initial_balance)
                curr += timedelta(days=1)
                
            months = days_total / 30.44 if days_total > 0 else 0.01
            trades_per_month_avg = total / months
        else:
            days_total = 0
            trades_per_month_avg = 0.0

        if daily_returns:
            mean_ret = sum(daily_returns) / len(daily_returns)
            variance_ret = sum((r - mean_ret) ** 2 for r in daily_returns) / len(daily_returns)
            stdev_ret = math.sqrt(variance_ret)
        else:
            mean_ret, stdev_ret = 0.0, 0.0

        sqrt_factor = math.sqrt(252) # Constante financeira clara para annualização de dias úteis reais
        
        lg.info(f"SHARPE DEBUG -> Mean Ret: {mean_ret:.6f} | Stdev Ret: {stdev_ret:.6f} | Sqrt Factor: {sqrt_factor:.2f}")

        if stdev_ret < 0.0001:
            sharpe_ratio = 0.0
            lg.warning(f"Sharpe could not be calculated: stdev {stdev_ret:.6f} < 0.0001")
        else:
            sharpe_ratio = (mean_ret / stdev_ret) * sqrt_factor
            
        low_sample_warning = total < 30
        if low_sample_warning:
            lg.warning(f"LOW SAMPLE WARNING: Only {total} total trades in period (less than 30). Reliability is low.")
            
        avg_position_usd = sum(t.position_usd for t in trade_log) / total if total > 0 else 0.0
        max_position_usd = max((t.position_usd for t in trade_log), default=0.0)

        return BacktestResult(
            total_trades=total,
            winning_trades=len(winners),
            losing_trades=len(losers),
            win_rate=win_rate,
            profit_factor=profit_factor,
            initial_balance=self._initial_balance,
            final_balance=final_balance,
            total_return_pct=total_return_pct,
            total_profit=total_profit,
            total_loss=total_loss,
            profit_per_trade_pct=profit_per_trade_pct,
            avg_winner_pct=avg_winner_pct,
            avg_loser_pct=avg_loser_pct,
            sharpe_ratio=sharpe_ratio,
            calmar_ratio=calmar_ratio,
            recovery_factor=recovery_factor,
            max_consecutive_losses=max_losses,
            stops_hit_count=stops_hit,
            momentum_exits_count=momentum_exits,
            trailing_exits_count=trailing_exits,
            avg_position_usd=avg_position_usd,
            max_position_usd=max_position_usd,
            trades_per_month_avg=trades_per_month_avg,
            low_sample_warning=low_sample_warning,
            equity_curve=equity_curve,
            trade_log=trade_log,
            max_drawdown=max_drawdown,
        )


    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log_results(self, result: BacktestResult) -> None:
        """Log backtest results via structured logging."""
        log.info("\n" + result.summary())

        if result.trade_log:
            log.info(
                "Trade log (last 10 trades)",
            )
            for trade in result.trade_log[-10:]:
                prefix = "✅" if trade.profit > 0 else "❌"
                log.info(
                    f"  {prefix} {trade.side} "
                    f"{trade.entry_time:%Y-%m-%d %H:%M} → {trade.exit_time:%Y-%m-%d %H:%M} | "
                    f"Entry: {trade.entry_price:,.2f} → Exit: {trade.exit_price:,.2f} | "
                    f"PnL: {'+'if trade.profit>=0 else ''}{trade.profit:.4f}",
                )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _timeframe_to_minutes(self) -> int:
        """Convert timeframe string to minutes for synthetic data generation."""
        mapping = {
            "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
            "1h": 60, "2h": 120, "4h": 240, "6h": 360,
            "8h": 480, "12h": 720, "1d": 1440,
        }
        return mapping.get(self._timeframe, 60)
