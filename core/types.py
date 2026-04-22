"""
core/types.py
-------------
Domain value objects and enumerations.

All types are frozen dataclasses with __slots__ for:
- Thread-safety (immutable by construction)
- Memory efficiency (slots eliminate __dict__)
- Hashability (frozen enables use as dict keys / set members)
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class ProviderID(enum.Enum):
    """Identifies a market data source implementation."""
    SIMULATED = "simulated"
    BINANCE = "binance"


class Timeframe(enum.Enum):
    """OHLCV bar period. Values are ccxt-compatible strings."""
    M1  = "1m"
    M5  = "5m"
    M15 = "15m"
    M30 = "30m"
    H1  = "1h"
    H4  = "4h"
    D1  = "1d"
    W1  = "1w"

    def to_seconds(self) -> int:
        """Convert timeframe to duration in seconds."""
        _map: dict[str, int] = {
            "1m": 60,
            "5m": 300,
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
            "4h": 14400,
            "1d": 86400,
            "1w": 604800,
        }
        return _map[self.value]


class FeedState(enum.Enum):
    """Lifecycle state of a DataFeedManager instance."""
    IDLE = "idle"
    CONNECTING = "connecting"
    STREAMING = "streaming"
    PAUSED = "paused"
    ERROR = "error"
    STOPPED = "stopped"


# ---------------------------------------------------------------------------
# Value Objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Symbol:
    """
    Trading pair representation.

    Supports construction from components or string parsing:
        Symbol("BTC", "USDT")
        Symbol.from_string("BTC/USDT")
    """
    base: str
    quote: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "base", self.base.upper())
        object.__setattr__(self, "quote", self.quote.upper())

    def __str__(self) -> str:
        return f"{self.base}/{self.quote}"

    @classmethod
    def from_string(cls, s: str) -> Symbol:
        """Parse 'BTC/USDT', 'BTCUSDT', or 'BTC-USDT' formats."""
        for sep in ("/", "-"):
            if sep in s:
                parts = s.split(sep, 1)
                return cls(parts[0].strip(), parts[1].strip())
        # No separator — heuristic: last 3-4 chars are quote
        s_upper = s.upper()
        for quote_len in (4, 3):
            if len(s_upper) > quote_len:
                return cls(s_upper[:-quote_len], s_upper[-quote_len:])
        raise ValueError(f"Cannot parse symbol: {s!r}")


@dataclass(frozen=True, slots=True)
class BarData:
    """
    Single OHLCV bar (candle).

    Invariant: low <= open <= high AND low <= close <= high.
    Validated in __post_init__ to catch data corruption early.
    """
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool = True

    def __post_init__(self) -> None:
        if self.high < self.low:
            raise ValueError(
                f"BarData invariant violated: high ({self.high}) < low ({self.low})"
            )
        if self.open < self.low or self.open > self.high:
            raise ValueError(
                f"BarData invariant violated: open ({self.open}) "
                f"outside [low={self.low}, high={self.high}]"
            )
        if self.close < self.low or self.close > self.high:
            raise ValueError(
                f"BarData invariant violated: close ({self.close}) "
                f"outside [low={self.low}, high={self.high}]"
            )


@dataclass(frozen=True, slots=True)
class FeedStateChange:
    """Represents a transition between two FeedStates."""
    previous: FeedState
    current: FeedState
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class FeedConfig:
    """
    Immutable configuration for a DataFeedManager instance.

    Frozen to support hot-reload patterns: create a new FeedConfig
    and compare with the running one to decide if restart is needed.
    """
    symbol: Symbol
    timeframe: Timeframe
    provider_id: ProviderID
    snapshot_limit: int = 200
    poll_interval_sec: float = 1.0


# ---------------------------------------------------------------------------
# Order Book types (Phase 3)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class OrderBookLevel:
    """Single price level in the order book (one row in DOM)."""
    price: float
    quantity: float


@dataclass(frozen=True, slots=True)
class OrderBookSnapshot:
    """
    Immutable snapshot of the full order book at a point in time.

    Uses tuples (not lists) to guarantee immutability across
    signal/slot boundaries — prevents accidental mutation by receivers.
    """
    bids: tuple[OrderBookLevel, ...]
    asks: tuple[OrderBookLevel, ...]
    spread: float
