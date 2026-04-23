"""
data/feed.py
------------
Data feed infrastructure: providers and feed manager.

Provider pattern:
    IDataProvider defines the contract for any market data source.
    Implementations (SimulatedProvider, CcxtProvider) are pluggable
    at runtime via ProviderID in FeedConfig.

DataFeedManager:
    Orchestrates the feed lifecycle: IDLE → CONNECTING → STREAMING ⇄ ERROR → STOPPED.
    Uses asyncio.Event for cooperative shutdown (no task.cancel() brute force).
    Retry with exponential backoff and jitter on transient errors.
"""
from __future__ import annotations

import asyncio
import math
import random
import time
from datetime import datetime, timezone
from typing import Any, Callable, Protocol, runtime_checkable

import numpy as np
import pandas as pd

from core.types import (
    BarData,
    FeedConfig,
    FeedState,
    FeedStateChange,
    ProviderID,
    Timeframe,
)
from utils.logger import get_logger


# ---------------------------------------------------------------------------
# Provider Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class IDataProvider(Protocol):
    """Structural contract for market data sources."""

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
    ) -> pd.DataFrame:
        """Fetch historical OHLCV data. Returns standardized DataFrame."""
        ...

    async def fetch_latest_bar(
        self,
        symbol: str,
        timeframe: str,
    ) -> BarData:
        """Fetch the most recent (possibly forming) bar."""
        ...

    async def close(self) -> None:
        """Release any held resources (connections, sessions)."""
        ...


# ---------------------------------------------------------------------------
# Simulated Provider
# ---------------------------------------------------------------------------

class SimulatedProvider:
    """
    Generates synthetic market data for development/testing.

    Uses geometric Brownian motion for realistic price paths.
    No network I/O — fully deterministic given the same seed.
    """

    def __init__(self) -> None:
        self._log = get_logger(__name__, component="simulated_provider")
        self._base_price: float = 50_000.0
        self._price: float = self._base_price
        self._volatility: float = 0.002
        self._last_bar_time: float = 0.0

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
    ) -> pd.DataFrame:
        """Generate synthetic historical OHLCV bars."""
        tf_seconds = Timeframe(timeframe).to_seconds()
        now = time.time()
        # Align to timeframe boundary
        start_ts = (now // tf_seconds - limit) * tf_seconds

        rows: list[dict[str, Any]] = []
        price = self._base_price

        for i in range(limit):
            ts = start_ts + i * tf_seconds
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)

            # Geometric Brownian motion
            ret = random.gauss(0, self._volatility)
            price *= math.exp(ret)

            o = price
            h = o * (1 + abs(random.gauss(0, self._volatility * 0.5)))
            l = o * (1 - abs(random.gauss(0, self._volatility * 0.5)))
            # Ensure high >= open and low <= open
            h = max(h, o)
            l = min(l, o)
            c = l + random.random() * (h - l)
            v = random.uniform(0.1, 10.0)

            rows.append({
                "timestamp": dt,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": v,
            })
            price = c

        self._price = price
        self._last_bar_time = start_ts + limit * tf_seconds

        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df.set_index("timestamp", inplace=True)
        df = df.astype(np.float64)

        self._log.debug("Generated snapshot", extra={"bars": len(df)})
        return df

    async def fetch_latest_bar(
        self,
        symbol: str,
        timeframe: str,
    ) -> BarData:
        """Generate a synthetic live bar with random walk."""
        tf_seconds = Timeframe(timeframe).to_seconds()
        now = time.time()
        bar_ts = (now // tf_seconds) * tf_seconds
        dt = datetime.fromtimestamp(bar_ts, tz=timezone.utc)

        is_new_bar = bar_ts > self._last_bar_time

        # Random walk
        ret = random.gauss(0, self._volatility * 0.3)
        self._price *= math.exp(ret)

        o = self._price * (1 + random.gauss(0, self._volatility * 0.1))
        h = max(o, self._price) * (1 + abs(random.gauss(0, self._volatility * 0.2)))
        l = min(o, self._price) * (1 - abs(random.gauss(0, self._volatility * 0.2)))
        c = self._price
        v = random.uniform(0.01, 2.0)

        # Ensure OHLC invariant
        h = max(h, o, c)
        l = min(l, o, c)

        is_closed = is_new_bar
        if is_new_bar:
            self._last_bar_time = bar_ts

        return BarData(
            timestamp=dt,
            open=round(o, 2),
            high=round(h, 2),
            low=round(l, 2),
            close=round(c, 2),
            volume=round(v, 6),
            is_closed=is_closed,
        )

    async def close(self) -> None:
        """No-op for simulated provider."""


# ---------------------------------------------------------------------------
# Ccxt (Binance) Provider
# ---------------------------------------------------------------------------

class CcxtProvider:
    """
    Market data provider backed by ccxt with automatic exchange fallback.

    Tries exchanges in order: binance → bybit → kraken.
    If one fails (e.g. geo-restriction HTTP 451 from US-based servers),
    automatically falls back to the next available exchange.

    Lazy-initializes the exchange instance on first use.
    """

    # Fallback chain: try in order until one works
    _EXCHANGE_CHAIN: tuple[str, ...] = ("binance", "bybit", "kraken")

    def __init__(self) -> None:
        self._log = get_logger(__name__, component="ccxt_provider")
        self._exchange: Any = None
        self._exchange_id: str = ""

    def _ensure_exchange(self) -> Any:
        """Lazy-init the ccxt exchange instance with fallback chain."""
        if self._exchange is not None:
            return self._exchange

        try:
            import ccxt  # type: ignore[import-untyped]
        except ImportError as e:
            raise ImportError(
                "ccxt is required for market data: pip install ccxt"
            ) from e

        last_error: Exception | None = None
        for exchange_id in self._EXCHANGE_CHAIN:
            try:
                exchange_class = getattr(ccxt, exchange_id, None)
                if exchange_class is None:
                    continue

                exchange = exchange_class({
                    "enableRateLimit": True,
                    "options": {"defaultType": "spot"},
                })

                # Probe connectivity by loading markets
                exchange.load_markets()

                self._exchange = exchange
                self._exchange_id = exchange_id
                self._log.info(
                    f"{exchange_id.capitalize()} exchange initialized (active)",
                )
                return self._exchange

            except Exception as exc:
                last_error = exc
                self._log.warning(
                    f"{exchange_id} unavailable — trying next fallback",
                    extra={"error": str(exc)[:120]},
                )
                # Clean up failed instance
                try:
                    exchange.close()
                except Exception:
                    pass

        raise RuntimeError(
            f"All exchanges failed. Last error: {last_error}"
        )

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
    ) -> pd.DataFrame:
        """Fetch OHLCV via ccxt (auto-selected exchange)."""
        exchange = self._ensure_exchange()

        # ccxt returns list of [timestamp_ms, o, h, l, c, v]
        loop = asyncio.get_running_loop()
        try:
            raw = await loop.run_in_executor(
                None,
                lambda: exchange.fetch_ohlcv(symbol, timeframe, limit=limit),
            )
        except Exception as exc:
            # Reset cached exchange so fallback chain re-runs on next call
            self._log.warning(
                f"{self._exchange_id} fetch failed — resetting for fallback",
                extra={"error": str(exc)[:120]},
            )
            self._exchange = None
            self._exchange_id = ""
            raise

        if not raw:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        df = pd.DataFrame(
            raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        df = df.astype(np.float64)

        self._log.debug("Fetched OHLCV", extra={"bars": len(df)})
        return df

    async def fetch_latest_bar(
        self,
        symbol: str,
        timeframe: str,
    ) -> BarData:
        """Fetch the latest (forming) bar."""
        exchange = self._ensure_exchange()

        loop = asyncio.get_running_loop()
        try:
            raw = await loop.run_in_executor(
                None,
                lambda: exchange.fetch_ohlcv(symbol, timeframe, limit=2),
            )
        except Exception as exc:
            # Reset cached exchange so fallback chain re-runs on next call
            self._log.warning(
                f"{self._exchange_id} fetch failed — resetting for fallback",
                extra={"error": str(exc)[:120]},
            )
            self._exchange = None
            self._exchange_id = ""
            raise

        if not raw or len(raw) < 2:
            raise RuntimeError("Insufficient data from exchange")

        # The last entry is the currently forming bar
        forming = raw[-1]

        dt = datetime.fromtimestamp(forming[0] / 1000.0, tz=timezone.utc)
        return BarData(
            timestamp=dt,
            open=float(forming[1]),
            high=float(forming[2]),
            low=float(forming[3]),
            close=float(forming[4]),
            volume=float(forming[5]),
            is_closed=False,
        )

    async def close(self) -> None:
        """Close the ccxt exchange instance."""
        if self._exchange is not None:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._exchange.close)
            except Exception:
                pass
            self._exchange = None
            self._log.info("Exchange connection closed")


# ---------------------------------------------------------------------------
# Provider Factory
# ---------------------------------------------------------------------------

def create_provider(provider_id: ProviderID) -> IDataProvider:
    """Instantiate a data provider by ID."""
    _registry: dict[ProviderID, type] = {
        ProviderID.SIMULATED: SimulatedProvider,
        ProviderID.BINANCE: CcxtProvider,
    }
    cls = _registry.get(provider_id)
    if cls is None:
        raise ValueError(f"Unknown provider: {provider_id!r}")
    return cls()  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# DataFeedManager
# ---------------------------------------------------------------------------

class DataFeedManager:
    """
    Manages the lifecycle of a single data feed.

    Lifecycle: IDLE → CONNECTING → STREAMING ⇄ ERROR → STOPPED

    Uses asyncio.Event for cooperative shutdown — the polling loop
    checks the event on every iteration, enabling clean exit without
    task.cancel() side effects.

    Retry strategy: exponential backoff with jitter (base=1s, max=30s).
    """

    def __init__(
        self,
        config: FeedConfig,
        on_snapshot: Callable[[pd.DataFrame], None],
        on_bar_update: Callable[[BarData], None],
        on_state_change: Callable[[FeedStateChange], None],
    ) -> None:
        self._config = config
        self._on_snapshot = on_snapshot
        self._on_bar_update = on_bar_update
        self._on_state_change = on_state_change

        self._provider: IDataProvider = create_provider(config.provider_id)
        self._state: FeedState = FeedState.IDLE
        self._stop_event: asyncio.Event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

        self._log = get_logger(
            __name__,
            component="feed_manager",
            symbol=str(config.symbol),
        )

    @property
    def state(self) -> FeedState:
        return self._state

    async def start(self) -> None:
        """Start the feed polling loop as a background task."""
        if self._task is not None and not self._task.done():
            self._log.warning("Feed already running")
            return

        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())
        self._log.info("Feed started")

    async def stop(self) -> None:
        """Signal the feed to stop and wait for the task to complete."""
        self._log.info("Stop requested")
        self._stop_event.set()

        if self._task is not None and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except asyncio.TimeoutError:
                self._log.warning("Feed stop timed out — cancelling task")
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass

        await self._provider.close()
        self._set_state(FeedState.STOPPED, reason="Feed stopped")
        self._log.info("Feed stopped")

    # ------------------------------------------------------------------
    # Internal: main loop
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        """Main polling loop with retry and backoff."""
        retry_count = 0
        max_backoff = 30.0

        while not self._stop_event.is_set():
            try:
                # Phase 1: Connect and fetch snapshot
                self._set_state(FeedState.CONNECTING, reason="Fetching snapshot")

                symbol_str = str(self._config.symbol)
                tf_str = self._config.timeframe.value

                df = await self._provider.fetch_ohlcv(
                    symbol=symbol_str,
                    timeframe=tf_str,
                    limit=self._config.snapshot_limit,
                )

                if self._stop_event.is_set():
                    break

                self._on_snapshot(df)
                self._set_state(FeedState.STREAMING, reason="Live data streaming")
                retry_count = 0

                # Phase 2: Poll loop
                consecutive_errors = 0
                while not self._stop_event.is_set():
                    try:
                        bar = await self._provider.fetch_latest_bar(
                            symbol=symbol_str,
                            timeframe=tf_str,
                        )

                        if self._stop_event.is_set():
                            break

                        self._on_bar_update(bar)
                        consecutive_errors = 0  # Reset on success

                    except Exception as poll_err:
                        consecutive_errors += 1
                        # Exponential backoff on errors: 2s, 4s, 8s... max 60s
                        error_backoff = min(2.0 * (2 ** (consecutive_errors - 1)), 60.0)
                        self._log.warning(
                            "Poll error (retrying after backoff)",
                            extra={
                                "consecutive_errors": consecutive_errors,
                                "backoff_sec": error_backoff,
                            },
                            exc_info=poll_err,
                        )

                        # If too many consecutive errors, break to reconnect
                        if consecutive_errors >= 10:
                            self._log.error(
                                "Too many consecutive poll errors — reconnecting",
                            )
                            break

                        # Wait the error backoff before retrying
                        try:
                            await asyncio.wait_for(
                                self._stop_event.wait(),
                                timeout=error_backoff,
                            )
                            break  # stop_event was set
                        except asyncio.TimeoutError:
                            pass  # Normal: backoff elapsed, retry
                        continue  # Skip the normal poll interval

                    # Wait for next poll or stop signal
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(),
                            timeout=self._config.poll_interval_sec,
                        )
                        break  # stop_event was set
                    except asyncio.TimeoutError:
                        pass  # Normal: poll interval elapsed

            except Exception as e:
                retry_count += 1
                backoff = min(
                    (2 ** retry_count) + random.uniform(0, 1),
                    max_backoff,
                )
                self._set_state(
                    FeedState.ERROR,
                    reason=f"Error: {e!r} — retry #{retry_count} in {backoff:.1f}s",
                )
                self._log.error(
                    "Feed error",
                    extra={"retry": retry_count, "backoff_sec": round(backoff, 1)},
                    exc_info=e,
                )

                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=backoff,
                    )
                    break  # stop_event was set during backoff
                except asyncio.TimeoutError:
                    pass  # Retry

    # ------------------------------------------------------------------
    # Internal: state management
    # ------------------------------------------------------------------

    def _set_state(self, new_state: FeedState, reason: str | None = None) -> None:
        """Transition state and notify listener."""
        if self._stop_event.is_set() and new_state not in (
            FeedState.STOPPED, FeedState.ERROR
        ):
            return  # Don't emit non-terminal states during shutdown

        old_state = self._state
        self._state = new_state
        change = FeedStateChange(
            previous=old_state,
            current=new_state,
            reason=reason,
        )
        self._on_state_change(change)
