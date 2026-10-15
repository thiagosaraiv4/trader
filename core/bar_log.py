"""
core/bar_log.py
---------------
Append-only CSV of the closed bars the live bot traded on (live_bars.csv).

run_comparison.py replays these bars through the BacktestEngine, so the
comparison uses the same prices the bot saw and does not depend on
downloading history (Binance blocks US servers such as Codespaces).

Each row records how the bot used the bar:
    kind = "warmup"  bar from a snapshot — only initialized indicators
    kind = "live"    bar the bot processed (indicators + strategy)
    seg  = "<run>:<n>"  run = bot process start, n = snapshot number in that
                        run (a feed reconnect takes a new snapshot)

Restarts and reconnects therefore become separate ReplaySegments, and bars
from while the bot was offline are never treated as traded.

Design:
    - append_bars never raises — a recording failure must not stop the bot
    - load_segments skips malformed rows (e.g. a line cut by a hard kill)
"""
from __future__ import annotations

import csv
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

import pandas as pd

from core.backtest_engine import ReplaySegment
from core.paths import DATA_DIR
from core.types import BarData
from utils.logger import get_logger

_log = get_logger(__name__, component="bar_log")

# Lives next to session.json (repository root unless TRADER_DATA_DIR)
BAR_LOG_PATH = DATA_DIR / "live_bars.csv"

_FIELDS = ("timestamp", "open", "high", "low", "close", "volume", "kind", "seg")

# Warm-up size of the live bot (headless_main.py snapshot) for legacy files
_LEGACY_WARMUP_BARS = 200

_checked_paths: set[Path] = set()


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


def new_run_id() -> str:
    """Identifier of one bot process (start time)."""
    return str(int(time.time()))


def _rotate_old_format(path: Path) -> None:
    """Move a file written with another column layout out of the way (once per process)."""
    if path in _checked_paths:
        return
    _checked_paths.add(path)
    try:
        with open(path, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), None)
    except OSError:
        return
    if header is not None and tuple(header) != _FIELDS:
        old = path.with_name(f"{path.stem}.old-{int(time.time())}{path.suffix}")
        path.replace(old)
        _log.warning(f"{path.name} had an old format — moved to {old.name}")


def append_bars(
    bars: Iterable[BarData],
    kind: str,
    seg: str,
    path: Path = BAR_LOG_PATH,
) -> None:
    """Append closed bars to the CSV (header written on first use)."""
    try:
        if path.exists():
            _rotate_old_format(path)
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
                    kind,
                    seg,
                ))
    except OSError as e:
        _log.error(f"Failed to record bars: {e}")


def load_segments(path: Path = BAR_LOG_PATH) -> list[ReplaySegment]:
    """
    Rebuild the live run as ReplaySegments, in time order (file order).

    Each segment = the warm-up rows of one snapshot + the live rows processed
    after it. Segments with no live rows (bot stopped before a bar closed)
    are dropped.
    """
    if not path.exists():
        return []

    warmup: dict[str, list[BarData]] = {}
    live: dict[str, list[BarData]] = {}
    order: list[str] = []
    skipped = 0

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        legacy = "kind" not in (reader.fieldnames or [])
        for i, row in enumerate(reader):
            try:
                ts = datetime.fromisoformat(row["timestamp"])
                bar = BarData(
                    timestamp=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                )
                if legacy:
                    # Single run: first snapshot = warm-up, rest = live
                    kind = "warmup" if i < _LEGACY_WARMUP_BARS else "live"
                    seg = "legacy:0"
                else:
                    kind, seg = row["kind"], row["seg"]
                    if kind not in ("warmup", "live") or not seg:
                        raise ValueError(f"bad kind/seg: {kind!r} {seg!r}")
            except (KeyError, TypeError, ValueError):
                skipped += 1
                continue

            if seg not in warmup:
                warmup[seg] = []
                live[seg] = []
                order.append(seg)
            (warmup if kind == "warmup" else live)[seg].append(bar)

    if skipped:
        _log.warning(f"Skipped {skipped} malformed rows in {path.name}")

    segments: list[ReplaySegment] = []
    prev_run: str | None = None
    last_live_ts: datetime | None = None
    for seg in order:
        bars = live[seg]
        if last_live_ts is not None:
            bars = [b for b in bars if b.timestamp > last_live_ts]
        if not bars or not warmup[seg]:
            continue
        run = seg.split(":", 1)[0]
        segments.append(ReplaySegment(
            warmup=warmup[seg],
            bars=bars,
            new_run=run != prev_run,
        ))
        prev_run = run
        last_live_ts = bars[-1].timestamp

    return segments
