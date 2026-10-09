"""
core/strategy_params.py
-----------------------
Tunable parameters of Strategy V3 and its risk/exit rules, shared by the live
bot (headless_main.py), BacktestEngine, run_backtest.py and run_comparison.py.

Loaded from strategy.json at the application root when present; the defaults
reproduce the original hard-coded V3 behaviour.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any

from core.types import Timeframe
from utils.logger import get_logger

_log = get_logger(__name__, component="strategy_params")

PARAMS_PATH = Path(__file__).resolve().parent.parent / "strategy.json"


STRATEGIES = ("ma_cross_v3", "trend_v4")

# The live bot warms up on a 200-closed-bar snapshot (headless_main.py), so no
# indicator may need more history than that.
MAX_INDICATOR_PERIOD = 200

# Default circuit breaker per strategy when halt_drawdown_pct is not set.
# trend_v4 has normal drawdowns well above 7% (its per-trade risk is already
# capped by the stop), so the breaker would end the test early.
_DEFAULT_HALT = {"ma_cross_v3": 0.07, "trend_v4": 0.0}


class InvalidParams(ValueError):
    """strategy.json / parameters that the bot must not run with."""


@dataclass(frozen=True)
class StrategyParams:
    # "ma_cross_v3": EMA9 × SMA20 cross with filters (original)
    # "trend_v4":    Donchian breakout trend following
    strategy: str = "ma_cross_v3"
    # Candle size the live bot trades (ccxt string: 1m, 5m, 15m, 4h, ...)
    timeframe: str = "1m"

    # --- Entry -----------------------------------------------------------
    # ATR must be within [atr_filter_min, atr_filter_max] × its 14-bar mean.
    # atr_filter_min <= 0 disables the "volatility expansion" requirement.
    atr_filter_min: float = 1.05
    atr_filter_max: float = 3.0
    # Enter if EMA9 crossed above SMA20 within the last N closed bars
    # (1 = only on the crossing bar) and is still above it.
    cross_lookback: int = 1
    # Only buy above the SMA 200
    trend_filter: bool = True

    # --- Exit / risk -----------------------------------------------------
    # Momentum exit when SMA20 - EMA9 > exit_atr × ATR
    exit_atr: float = 0.3
    # Initial hard stop = entry - stop_atr × ATR
    stop_atr: float = 1.5
    # Trailing stop starts after +trail_activation_pct and trails at
    # max(trail_atr × ATR, trail_min_pct × price)
    trail_activation_pct: float = 0.003
    trail_atr: float = 1.0
    trail_min_pct: float = 0.005
    # Position size cap as a fraction of balance
    max_position_pct: float = 0.20
    # Size by the real stop distance (loss at stop ≈ risk_pct of balance);
    # False = legacy V3 sizing (always the max_position_pct cap)
    risk_sizing: bool = False

    # --- trend_v4 (Donchian) ---------------------------------------------
    # Buy when the close breaks the highest high of the previous N bars;
    # sell when it breaks the lowest low of the previous M bars.
    donchian_entry: int = 20
    donchian_exit: int = 10
    # Only buy above SMA(trend_sma); 0 disables the filter
    trend_sma: int = 200

    # --- Circuit breaker -------------------------------------------------
    # Stop opening positions once equity falls this fraction below the
    # session start (0 = off). None = strategy default (see _DEFAULT_HALT).
    halt_drawdown_pct: float | None = None

    def __post_init__(self) -> None:
        self.validate()

    @property
    def halt_pct(self) -> float:
        """Effective circuit-breaker drawdown (0 = disabled)."""
        if self.halt_drawdown_pct is None:
            return _DEFAULT_HALT.get(self.strategy, 0.07)
        return self.halt_drawdown_pct

    def validate(self) -> None:
        """Raise InvalidParams for values the bot can't run with."""
        def fail(msg: str) -> None:
            raise InvalidParams(msg)

        if self.strategy not in STRATEGIES:
            fail(f"strategy {self.strategy!r} not in {STRATEGIES}")
        valid_tf = [t.value for t in Timeframe]
        if self.timeframe not in valid_tf:
            fail(f"timeframe {self.timeframe!r} not in {valid_tf}")
        for name in ("trend_filter", "risk_sizing"):
            if not isinstance(getattr(self, name), bool):
                fail(f"{name} must be true/false")
        for name in ("cross_lookback", "donchian_entry", "donchian_exit", "trend_sma"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int):
                fail(f"{name} must be an integer")
            low = 0 if name == "trend_sma" else 1
            if not low <= v <= MAX_INDICATOR_PERIOD:
                fail(f"{name}={v} must be between {low} and {MAX_INDICATOR_PERIOD}")
        for name in ("atr_filter_min", "atr_filter_max", "exit_atr", "stop_atr",
                     "trail_activation_pct", "trail_atr", "trail_min_pct", "max_position_pct"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                fail(f"{name} must be a number >= 0")
        if self.stop_atr <= 0:
            fail("stop_atr must be > 0")
        if not 0 < self.max_position_pct < 1:
            fail("max_position_pct must be between 0 and 1 (exclusive; fees need cash)")
        if self.halt_drawdown_pct is not None and (
            isinstance(self.halt_drawdown_pct, bool)
            or not isinstance(self.halt_drawdown_pct, (int, float))
            or not 0 <= self.halt_drawdown_pct < 1
        ):
            fail("halt_drawdown_pct must be null or a number in [0, 1)")

    @classmethod
    def defaults_for(cls, strategy: str) -> StrategyParams:
        """Sensible starting point for each strategy."""
        if strategy == "trend_v4":
            return cls(
                strategy="trend_v4",
                timeframe="4h",
                stop_atr=3.0,
                trail_activation_pct=0.0,
                trail_atr=4.0,
                trail_min_pct=0.0,
                max_position_pct=0.95,
                risk_sizing=True,
            )
        if strategy == "ma_cross_v3":
            return cls()
        raise ValueError(f"Unknown strategy {strategy!r} (options: {', '.join(STRATEGIES)})")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StrategyParams:
        if not isinstance(data, dict):
            raise InvalidParams("expected a JSON object")
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            _log.warning(f"Ignoring unknown strategy params: {sorted(unknown)}")
        # Missing keys take the defaults of the chosen strategy
        base = cls.defaults_for(data.get("strategy", cls().strategy)) \
            if data.get("strategy", cls().strategy) in STRATEGIES else cls()
        try:
            return replace(base, **{k: v for k, v in data.items() if k in known})
        except TypeError as e:
            raise InvalidParams(str(e)) from e


def load_params(path: Path = PARAMS_PATH) -> StrategyParams:
    """
    strategy.json if present, else the defaults.

    An unreadable or invalid file raises InvalidParams: the live bot must not
    silently fall back to another strategy (it would archive the running
    session and trade something else).
    """
    if not path.exists():
        return StrategyParams()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise InvalidParams(f"{path.name}: {e}") from e
    try:
        return StrategyParams.from_dict(data)
    except InvalidParams as e:
        raise InvalidParams(f"{path.name}: {e}") from e


def save_params(params: StrategyParams, path: Path = PARAMS_PATH) -> None:
    """Atomic write (a crash mid-write must not leave a half file behind)."""
    params.validate()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(params.to_dict(), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
