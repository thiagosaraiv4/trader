"""
run_backtest.py
----------------
Standalone backtest entry point.

Usage:
    python run_backtest.py

Runs the Trend Filtered MA Cross Strategy (EMA9/SMA20 + SMA200 filter) against historical data
and prints performance metrics. Uses ccxt/Binance for real data,
falls back to synthetic data if unavailable.

Requires a QCoreApplication for PaperTradingEngine (QObject subclass).
This is lightweight — no GUI is created.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from PySide6.QtCore import QCoreApplication

from core.backtest_engine import BacktestEngine
from utils.logger import configure_logging, get_logger


def main() -> None:
    """Run the backtest with default parameters."""
    import logging
    configure_logging(level=logging.INFO)
    log = get_logger(__name__, component="run_backtest")

    # QCoreApplication needed for PaperTradingEngine (QObject)
    QCoreApplication.instance() or QCoreApplication(sys.argv)

    log.info("=" * 60)
    log.info("BACKTESTING ENGINE — V3 OUT OF SAMPLE VALIDATION (2022/2023)")
    log.info("=" * 60)
    
    # 2022: Bear Market
    engine1 = BacktestEngine(
        symbol="BTC/USDT",
        timeframe="1h",
        start_date=datetime(2022, 1, 1, tzinfo=timezone.utc),
        end_date=datetime(2023, 1, 1, tzinfo=timezone.utc),
        initial_balance=10_000.0,
        trade_size=0.01,
        cooldown_sec=3600.0,
        risk_pct=0.02,
        use_ccxt=True,
    )
    
    # 2023: Recovery Market
    engine2 = BacktestEngine(
        symbol="BTC/USDT",
        timeframe="1h",
        start_date=datetime(2023, 1, 1, tzinfo=timezone.utc),
        end_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
        initial_balance=10_000.0,
        trade_size=0.01,
        cooldown_sec=3600.0,
        risk_pct=0.02,
        use_ccxt=True,
    )

    log.info("▶️ Running 2022 (Bear Market)...")
    result1 = engine1.run()
    
    log.info("▶️ Running 2023 (Recovery Market)...")
    result2 = engine2.run()

    log.info("\n=== 2022 BEAR MARKET ===")
    print(result1.summary())
    
    log.info("\n=== 2023 RECOVERY MARKET ===")
    print(result2.summary())

    sys.exit(0)


if __name__ == "__main__":
    main()
