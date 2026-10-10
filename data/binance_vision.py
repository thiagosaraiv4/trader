"""
data/binance_vision.py
----------------------
Binance public market-data archive (https://data.binance.vision): 1-second
SPOT klines, one zip per UTC day, verified against the published SHA-256 and
cached in backtest_cache/vision/.

Format notes (checked on real files):
    - No header row; 12 columns (see _COLUMNS).
    - Timestamps in milliseconds up to 2024-12-31, microseconds from
      2025-01-01 on; parsed by magnitude.
    - Normally one row per second: a second without trades is a filler row
      with trades=0 and open=high=low=close=previous close. Exchange halt
      days can have gaps, duplicated and out-of-order rows.
    - A day's file appears around 01:30-02:30 UTC of the next day.

The archive is licensed for personal, non-production backtesting/research
(Binance Vision dataset terms) — not for driving live order generation.
"""
from __future__ import annotations

import hashlib
import http.client
import io
import os
import time
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
_ATTEMPTS = 3
# Answers that retrying will not change (geo-block, bad request)
_NO_RETRY = {400, 401, 403, 451}


class VisionError(RuntimeError):
    """
    A day file could not be downloaded or read. status = HTTP status when
    the server answered with an error (403/451 = blocked location).
    """

    def __init__(self, msg: str, status: int | None = None) -> None:
        super().__init__(msg)
        self.status = status


def day_url(symbol: str, day: date, interval: str = "1s") -> str:
    name = f"{symbol}-{interval}-{day:%Y-%m-%d}.zip"
    return f"{BASE_URL}/{symbol}/{interval}/{name}"


def _get(url: str) -> bytes | None:
    """GET the body, or None when the file does not exist (404). Transient
    failures (dropped connection, timeout, 5xx) are retried."""
    req = urllib.request.Request(url, headers={"User-Agent": "trader-study/1.0"})
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            err = VisionError(f"{url}: HTTP {e.code}", status=e.code)
            if e.code in _NO_RETRY:
                raise err from e
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
            err = VisionError(f"{url}: {e}")
        if attempt == _ATTEMPTS:
            raise err
        time.sleep(2 * attempt)
    raise AssertionError("unreachable")


def download_day(symbol: str, day: date, cache_dir: Path = CACHE_DIR) -> Path | None:
    """
    Cached zip of one day of 1-second klines; None if Binance has not
    published that day (yet). Raises VisionError on network errors or a
    corrupt download.
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
        if hashlib.sha256(body).hexdigest() != expected:
            raise VisionError(f"{path.name}: checksum mismatch (download corrupted?)")
    try:
        bad_member = zipfile.ZipFile(io.BytesIO(body)).testzip()
    except zipfile.BadZipFile as e:
        raise VisionError(f"{path.name}: invalid zip") from e
    if bad_member is not None:
        raise VisionError(f"{path.name}: corrupt zip ({bad_member})")
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as fh:
        fh.write(body)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def read_day(path: Path) -> pd.DataFrame:
    """
    One day of 1-second klines: UTC DatetimeIndex (bar open), sorted and
    without duplicates; columns open, high, low, close, volume, trades.
    Raises VisionError if the file is unreadable or empty.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            with zf.open(zf.namelist()[0]) as fh:
                df = pd.read_csv(fh, header=None, names=_COLUMNS, usecols=range(9))
        # Tolerate a header row, should a file ever carry one
        df = df[pd.to_numeric(df["open_time"], errors="coerce").notna()]
        if df.empty:
            raise VisionError(f"{path.name}: no rows")
        ts = df["open_time"].astype("int64")
        out = df[["open", "high", "low", "close", "volume", "trades"]].astype(float)
    except VisionError:
        raise
    except (zipfile.BadZipFile, IndexError, KeyError, ValueError, OSError) as e:
        raise VisionError(f"{path.name}: unreadable ({e})") from e
    # Microseconds since 2025 (spot); milliseconds before
    unit = "us" if ts.iloc[0] > 10**14 else "ms"
    out.index = pd.to_datetime(ts, unit=unit, utc=True)
    out.index.name = "timestamp"
    return out[~out.index.duplicated()].sort_index()


def load_day(symbol: str, day: date, cache_dir: Path = CACHE_DIR) -> pd.DataFrame | None:
    """
    download_day + read_day. A cached file that no longer reads (e.g. cut
    by a crash) is deleted and downloaded again once. None = not published.
    """
    for attempt in (1, 2):
        path = download_day(symbol, day, cache_dir)
        if path is None:
            return None
        try:
            return read_day(path)
        except VisionError:
            path.unlink(missing_ok=True)
            if attempt == 2:
                raise
    raise AssertionError("unreachable")
