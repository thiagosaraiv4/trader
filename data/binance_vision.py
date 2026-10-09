"""
data/binance_vision.py
----------------------
Binance public market-data archive (https://data.binance.vision): 1-second
SPOT klines, one zip per UTC day, verified against the published SHA-256 and
cached in backtest_cache/vision/.

The archive is plain files (no API key, no rate limit). A day's file shows up
some hours after the day ends, so the most recent day may still be missing.

Spot timestamps changed from milliseconds to microseconds in 2025; rows are
parsed by magnitude so both formats load the same.
"""
from __future__ import annotations

import hashlib
import io
import os
import urllib.error
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd

BASE_URL = "https://data.binance.vision/data/spot/daily/klines"
CACHE_DIR = Path(__file__).resolve().parent.parent / "backtest_cache" / "vision"

# Kline CSV columns (the files have no header row)
_COLUMNS = (
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore",
)
_TIMEOUT_SEC = 60


class VisionError(RuntimeError):
    """A day file exists but could not be downloaded or is corrupt."""


def day_url(symbol: str, day: date, interval: str = "1s") -> str:
    name = f"{symbol}-{interval}-{day:%Y-%m-%d}.zip"
    return f"{BASE_URL}/{symbol}/{interval}/{name}"


def _get(url: str) -> bytes | None:
    """GET the body, or None when the file does not exist (404)."""
    req = urllib.request.Request(url, headers={"User-Agent": "trader-study/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise VisionError(f"{url}: HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise VisionError(f"{url}: {e}") from e


def download_day(symbol: str, day: date, cache_dir: Path = CACHE_DIR) -> Path | None:
    """
    Cached zip of one day of 1-second klines; None if Binance has not
    published that day (yet). Raises VisionError on network errors or a
    checksum mismatch.
    """
    url = day_url(symbol, day)
    path = cache_dir / url.rsplit("/", 1)[1]
    if path.exists():
        return path
    body = _get(url)
    if body is None:
        return None
    checksum = _get(url + ".CHECKSUM")
    if checksum:
        expected = checksum.decode("ascii", "replace").split()[0].lower()
        actual = hashlib.sha256(body).hexdigest()
        if actual != expected:
            raise VisionError(f"{path.name}: checksum mismatch (download corrupted?)")
    try:
        zipfile.ZipFile(io.BytesIO(body)).testzip()
    except zipfile.BadZipFile as e:
        raise VisionError(f"{path.name}: invalid zip") from e
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(body)
    os.replace(tmp, path)
    return path


def read_day(path: Path) -> pd.DataFrame:
    """
    One day of 1-second klines: UTC DatetimeIndex (bar open), columns
    open, high, low, close, volume, trades. Seconds without trades may be
    absent from the file.
    """
    with zipfile.ZipFile(path) as zf:
        with zf.open(zf.namelist()[0]) as fh:
            df = pd.read_csv(fh, header=None, names=_COLUMNS, usecols=range(9))
    # Tolerate a header row, should a file ever carry one
    df = df[pd.to_numeric(df["open_time"], errors="coerce").notna()]
    ts = df["open_time"].astype("int64")
    # Microseconds since 2025 (spot); milliseconds before
    unit = "us" if ts.iloc[0] > 10**14 else "ms"
    out = df[["open", "high", "low", "close", "volume", "trades"]].astype(float)
    out.index = pd.to_datetime(ts, unit=unit, utc=True)
    out.index.name = "timestamp"
    return out[~out.index.duplicated()].sort_index()
