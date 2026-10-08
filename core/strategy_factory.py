"""
core/strategy_factory.py
------------------------
Builds the strategy and the indicator set it needs from StrategyParams, so the
live bot (headless_main.py) and BacktestEngine always use the same pipeline.
"""
from __future__ import annotations

from core.algo_trading import TrendFilteredMACrossStrategyV3, TrendFollowStrategyV4
from core.indicators import (
    ATRIndicator,
    DonchianHighIndicator,
    DonchianLowIndicator,
    EMAIndicator,
    IndicatorEngine,
    SMAIndicator,
)
from core.strategy_params import StrategyParams


def build_indicator_engine(params: StrategyParams) -> IndicatorEngine:
    engine = IndicatorEngine()
    engine.register(ATRIndicator(period=14))
    if params.strategy == "trend_v4":
        engine.register(DonchianHighIndicator(period=params.donchian_entry))
        engine.register(DonchianLowIndicator(period=params.donchian_exit))
        if params.trend_sma > 0:
            engine.register(SMAIndicator(period=params.trend_sma))
    else:
        engine.register(SMAIndicator(period=200))
        engine.register(SMAIndicator(period=20))
        engine.register(EMAIndicator(period=9))
    return engine


def create_strategy(symbol: str, params: StrategyParams):
    if params.strategy == "trend_v4":
        return TrendFollowStrategyV4(symbol=symbol, params=params)
    if params.strategy == "ma_cross_v3":
        return TrendFilteredMACrossStrategyV3(symbol=symbol, params=params)
    raise ValueError(f"Unknown strategy {params.strategy!r}")
