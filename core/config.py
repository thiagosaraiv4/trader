"""
core/config.py
--------------
Application-level configuration constants.

All runtime-tunable defaults live here. In production, these would be
overridden via a config file (TOML/YAML) or environment variables.
No business logic — pure data.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.types import ProviderID, Symbol, Timeframe


@dataclass(frozen=True)
class AppConfig:
    """
    Top-level application configuration.

    Instantiated once in main.py and passed to subsystems that need it.
    Frozen to prevent accidental mutation at runtime.
    """
    default_symbol: Symbol           = Symbol("BTC", "USDT")
    default_timeframe: Timeframe     = Timeframe.M1
    default_provider: ProviderID     = ProviderID.BINANCE
    snapshot_limit: int              = 200
    poll_interval_sec: float         = 1.0

    # UI
    window_title: str                = "Trading Platform — Alpha"
    window_min_width: int            = 1280
    window_min_height: int           = 720
    window_default_width: int        = 1600
    window_default_height: int       = 900

    # Performance
    bar_throttle_ms: int             = 100   # max UI update rate: 10 fps
    shutdown_timeout_sec: float      = 8.0   # feed stop timeout before cancel
