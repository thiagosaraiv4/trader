"""
core/indicators.py
-------------------
Incremental technical indicator engine.

Performance contract:
    - initialize(df) → O(n) batch computation, called ONCE per feed start.
    - update(bar)     → O(1) per tick, called on every throttled bar update.

    PROHIBITED:
    - pandas .rolling() in any update path
    - DataFrame recreation or pd.concat per tick
    - numpy array copies (np.append) per tick

SMA implementation:
    Sliding window via collections.deque(maxlen=period).
    Running sum maintained incrementally:
        on append: sum += new_value (deque auto-evicts oldest)
        before append: sum -= evicted value (if deque is full)
    Cost: O(1) per update, O(1) memory.

EMA implementation:
    Recursive formula: ema = α * close + (1 - α) * prev_ema
    α = 2 / (period + 1)
    State: single float (_prev_ema). No pandas types leak into state.
    Cost: O(1) per update, O(1) memory.

Extensibility:
    Adding a new indicator (RSI, Bollinger, MACD) requires ONLY:
    1. Implement IIndicator protocol
    2. Call engine.register(MyIndicator(...))
    Zero modifications to IndicatorEngine, Controller, or Chart.
"""
from __future__ import annotations

from collections import deque
from typing import Protocol, runtime_checkable

import numpy as np
import pandas as pd

from core.types import BarData
from utils.logger import get_logger

log = get_logger(__name__, component="indicators")


# ---------------------------------------------------------------------------
# Indicator Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class IIndicator(Protocol):
    """
    Structural contract for a technical indicator.

    Two-phase lifecycle:
        1. initialize(df) — batch on historical snapshot, returns full series
        2. update(bar)     — incremental on live tick, returns single value

    Implementors MUST maintain their own internal state for O(1) updates.
    """

    @property
    def name(self) -> str:
        """Unique display name (e.g. 'SMA_20', 'EMA_9')."""
        ...

    @property
    def period(self) -> int:
        """Lookback period in bars."""
        ...

    def initialize(self, df: pd.DataFrame) -> pd.Series:
        """
        Batch-compute the indicator over the full historical DataFrame.

        Called once per feed start. O(n) is acceptable here.
        Must populate internal state so subsequent update() calls are O(1).

        Returns:
            pd.Series with same index as df. NaN for bars before period is met.
        """
        ...

    def update(self, bar: BarData) -> float | None:
        """
        Incrementally update the indicator with a single new bar.

        Called on every throttled tick. MUST be O(1).

        Returns:
            The new indicator value, or None if period not yet satisfied.
        """
        ...

    def reset(self) -> None:
        """Clear internal state. Called on feed/symbol change."""
        ...


# ---------------------------------------------------------------------------
# SMA — Simple Moving Average (deque-based, O(1) per tick)
# ---------------------------------------------------------------------------

class SMAIndicator:
    """
    Simple Moving Average with O(1) incremental updates.

    Uses a fixed-size deque as a sliding window. A running sum is
    maintained to avoid re-summing the window on each tick.

    Memory: O(period) — only the window values are stored.
    """

    def __init__(self, period: int = 20) -> None:
        self._period: int = period
        self._name: str = f"SMA_{period}"
        self._window: deque[float] = deque(maxlen=period)
        self._running_sum: float = 0.0

    @property
    def name(self) -> str:
        return self._name

    @property
    def period(self) -> int:
        return self._period

    def initialize(self, df: pd.DataFrame) -> pd.Series:
        """
        Batch SMA via numpy cumsum — O(n), no pandas .rolling().

        Also populates the internal deque with the last `period` close
        values so subsequent update() calls are O(1).
        """
        closes: np.ndarray = df["close"].to_numpy(dtype=np.float64)
        n = len(closes)
        result = np.full(n, np.nan, dtype=np.float64)

        if n >= self._period:
            # Cumulative sum trick for SMA — pure numpy, O(n)
            cumsum = np.cumsum(closes)
            result[self._period - 1:] = (
                cumsum[self._period - 1:] - np.concatenate(([0.0], cumsum[:-self._period]))
            ) / self._period

        # Populate deque with last `period` values for incremental mode
        self._window.clear()
        tail = closes[-self._period:] if n >= self._period else closes
        for v in tail:
            self._window.append(v)
        self._running_sum = sum(self._window)

        log.debug(
            "SMA initialized",
            extra={"indicator": self._name, "bars": n},
        )
        return pd.Series(result, index=df.index, name=self._name)

    def update(self, bar: BarData) -> float | None:
        """
        O(1) incremental SMA update.

        If deque is full (len == period), the oldest value is auto-evicted
        by deque. We subtract it from running_sum before the eviction
        implicitly handled by maxlen, then add the new value.
        """
        new_close = float(bar.close)

        # If deque is full, the leftmost will be evicted on append
        if len(self._window) == self._period:
            evicted = self._window[0]  # Will be popped by maxlen
            self._running_sum -= evicted

        self._window.append(new_close)
        self._running_sum += new_close

        if len(self._window) < self._period:
            return None

        return self._running_sum / self._period

    def reset(self) -> None:
        """Clear state for feed/symbol change."""
        self._window.clear()
        self._running_sum = 0.0


# ---------------------------------------------------------------------------
# EMA — Exponential Moving Average (recursive formula, O(1) per tick)
# ---------------------------------------------------------------------------

class EMAIndicator:
    """
    Exponential Moving Average with O(1) incremental updates.

    Uses the standard recursive formula:
        ema_t = α * close_t + (1 - α) * ema_{t-1}
        α = 2 / (period + 1)

    State: single float (_prev_ema). No pandas types leak into state.
    Memory: O(1).
    """

    def __init__(self, period: int = 9) -> None:
        self._period: int = period
        self._name: str = f"EMA_{period}"
        self._alpha: float = 2.0 / (period + 1.0)
        self._prev_ema: float | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def period(self) -> int:
        return self._period

    def initialize(self, df: pd.DataFrame) -> pd.Series:
        """
        Batch EMA via pandas .ewm() — O(n), called once.

        This is the ONLY place pandas ewm is used. After initialization,
        the internal state is a plain float — no pandas types retained.
        """
        series: pd.Series = df["close"].ewm(
            span=self._period, adjust=False
        ).mean()

        # Critical: extract plain float, NOT pandas scalar
        self._prev_ema = float(series.iloc[-1])

        log.debug(
            "EMA initialized",
            extra={"indicator": self._name, "bars": len(df)},
        )
        return series.rename(self._name)

    def update(self, bar: BarData) -> float | None:
        """
        O(1) incremental EMA update via recursive formula.

        Returns None only if initialize() was never called.
        """
        if self._prev_ema is None:
            return None

        new_close = float(bar.close)
        ema = self._alpha * new_close + (1.0 - self._alpha) * self._prev_ema
        self._prev_ema = ema
        return ema

    def reset(self) -> None:
        """Clear state for feed/symbol change."""
        self._prev_ema = None


# ---------------------------------------------------------------------------
# ATR — Average True Range (Wilder's Smoothing, O(1) per tick)
# ---------------------------------------------------------------------------

class ATRIndicator:
    """
    Average True Range with O(1) incremental updates.

    Uses Wilder's smoothing formula:
        ATR_t = (ATR_{t-1} * (period - 1) + TR_t) / period
    """

    def __init__(self, period: int = 14) -> None:
        self._period: int = period
        self._name: str = f"ATR_{period}"
        self._prev_close: float | None = None
        self._atr: float | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def period(self) -> int:
        return self._period

    def initialize(self, df: pd.DataFrame) -> pd.Series:
        """
        Batch ATR via pandas. EWMA with alpha=1/period is equivalent
        to Wilder's smoothing.
        """
        high = df["high"]
        low = df["low"]
        close = df["close"]

        # 1. Calculate True Range
        tr0 = high - low
        tr1 = (high - close.shift()).abs()
        tr2 = (low - close.shift()).abs()
        tr = pd.DataFrame({'tr0': tr0, 'tr1': tr1, 'tr2': tr2}).max(axis=1)

        # 2. Wilder's Smoothing <=> EWM with alpha = 1 / period
        atr_series = tr.ewm(alpha=1.0/self._period, adjust=False).mean()

        # Update initial internal state
        self._prev_close = float(close.iloc[-1])
        self._atr = float(atr_series.iloc[-1])

        log.debug(
            "ATR initialized",
            extra={"indicator": self._name, "bars": len(df)},
        )
        return atr_series.rename(self._name)

    def update(self, bar: BarData) -> float | None:
        """
        O(1) incremental ATR update via recursive formula.
        """
        if self._prev_close is None or self._atr is None:
            # Need initialized history
            return None

        high = float(bar.high)
        low = float(bar.low)
        prev_close = self._prev_close

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )

        new_atr = (self._atr * (self._period - 1.0) + tr) / self._period

        self._prev_close = float(bar.close)  # For next tick
        self._atr = new_atr

        return new_atr

    def reset(self) -> None:
        """Clear state for feed/symbol change."""
        self._prev_close = None
        self._atr = None


# ---------------------------------------------------------------------------
# IndicatorEngine — manages multiple indicators
# ---------------------------------------------------------------------------

class IndicatorEngine:
    """
    Registry and executor for multiple technical indicators.

    Responsibilities:
    - Register indicators (plug-and-play, no code changes needed)
    - Batch-initialize all on snapshot (returns full series per indicator)
    - Incrementally update all on tick (returns point values per indicator)

    Thread-safety: single-threaded by design (qasync event loop).
    """

    def __init__(self) -> None:
        self._indicators: list[IIndicator] = []
        self._log = get_logger(__name__, component="indicator_engine")

    def register(self, indicator: IIndicator) -> None:
        """Add an indicator to the engine. Call before initialize()."""
        self._indicators.append(indicator)
        self._log.info(
            "Indicator registered",
            extra={"indicator": indicator.name},
        )

    def initialize(self, df: pd.DataFrame) -> dict[str, pd.Series]:
        """
        Batch-initialize all registered indicators.

        Called once per snapshot. Returns a dict of full series
        keyed by indicator name.

        Args:
            df: Historical OHLCV DataFrame with DatetimeIndex.

        Returns:
            {indicator_name: pd.Series} with same index as df.
        """
        results: dict[str, pd.Series] = {}
        for ind in self._indicators:
            try:
                series = ind.initialize(df)
                results[ind.name] = series
            except Exception:
                self._log.exception(
                    "Indicator initialize failed",
                    extra={"indicator": ind.name},
                )
        self._log.info(
            "All indicators initialized",
            extra={"count": len(results)},
        )
        return results

    def update(self, bar: BarData) -> dict[str, float | None]:
        """
        Incrementally update all indicators with a single bar.

        Called on every throttled tick. Each indicator.update() is O(1).

        Returns:
            {indicator_name: value_or_None}
        """
        results: dict[str, float | None] = {}
        for ind in self._indicators:
            try:
                value = ind.update(bar)
                results[ind.name] = value
            except Exception:
                self._log.exception(
                    "Indicator update failed",
                    extra={"indicator": ind.name},
                )
                results[ind.name] = None
        return results

    def reset(self) -> None:
        """Reset all indicators. Called on symbol change."""
        for ind in self._indicators:
            ind.reset()
        self._log.info("All indicators reset")
