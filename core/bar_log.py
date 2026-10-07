"""
core/bar_log.py
---------------
Append-only CSV of the closed bars the live bot traded on (live_bars.csv).

run_comparison.py replays exactly these bars through the BacktestEngine, so
the comparison uses the same prices the bot saw and does not depend on
downloading history (Binance blocks US servers such as Codespaces).

Design:
    - append_bars never raises — a recording failure must not stop the bot
    - load_bars skips malformed rows (e.g. a line cut by a hard kill) and
      de-duplicates by timestamp (snapshots overlap previous runs)
"""
from __future__ import annotations

import csv
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

import pandas as pd

from core.types import BarData
from utils.logger import get_logger

_log = get_logger(__name__, component="bar_log")

# Lives next to session.json at the application root
BAR_LOG_PATH = Path(__file__).resolve().parent.parent / "live_bars.csv"

_FIELDS = ("timestamp", "open", "high", "low", "close", "volume")


def bars_from_dataframe(df: pd.DataFrame) -> list[BarData]:
    """Convert an OHLCV DataFrame (DatetimeIndex) to BarData, skipping invalid rows."""
    bars: list[BarData] = []
    for ts, row in zip(df.index, df.itertuples(index=False)):
        try:
            bars.append(BarData(
                timestamp=pd.Timestamp(ts).to_pydatetime(),
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=float(row.volume),
            ))
        except ValueError:
            continue
    return bars


def append_bars(bars: Iterable[BarData], path: Path = BAR_LOG_PATH) -> None:
    """Append closed bars to the CSV (header written on first use)."""
    try:
        new_file = not path.exists() or path.stat().st_size == 0
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if new_file:
                writer.writerow(_FIELDS)
            for bar in bars:
                writer.writerow((
                    bar.timestamp.isoformat(),
                    float(bar.open),
                    float(bar.high),
                    float(bar.low),
                    float(bar.close),
                    float(bar.volume),
                ))
    except OSError as e:
        _log.error(f"Failed to record bars: {e}")


def load_bars(path: Path = BAR_LOG_PATH) -> list[BarData]:
    """Load recorded bars sorted by time. Later rows win on duplicate timestamps."""
    if not path.exists():
        return []

    by_ts: dict[datetime, BarData] = {}
    skipped = 0
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                ts = datetime.fromisoformat(row["timestamp"])
                by_ts[ts] = BarData(
                    timestamp=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                )
            except (KeyError, TypeError, ValueError):
                skipped += 1

    if skipped:
        _log.warning(f"Skipped {skipped} malformed rows in {path.name}")
    return [by_ts[ts] for ts in sorted(by_ts)]
