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
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any

from utils.logger import get_logger

_log = get_logger(__name__, component="strategy_params")

PARAMS_PATH = Path(__file__).resolve().parent.parent / "strategy.json"


@dataclass(frozen=True)
class StrategyParams:
    # Candle size the live bot trades (ccxt string: 1m, 5m, 15m, ...)
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StrategyParams:
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            _log.warning(f"Ignoring unknown strategy params: {sorted(unknown)}")
        return replace(cls(), **{k: v for k, v in data.items() if k in known})


def load_params(path: Path = PARAMS_PATH) -> StrategyParams:
    """strategy.json if present (invalid file → defaults, with a warning)."""
    if not path.exists():
        return StrategyParams()
    try:
        return StrategyParams.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError) as e:
        _log.warning(f"Invalid {path.name} ({e}) — using default strategy params")
        return StrategyParams()


def save_params(params: StrategyParams, path: Path = PARAMS_PATH) -> None:
    path.write_text(json.dumps(params.to_dict(), indent=2) + "\n", encoding="utf-8")
