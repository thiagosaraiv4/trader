"""
web/data.py
-----------
Read-only access to everything the bots write, as plain JSON-ready dicts —
the single layer the web panel's API serves (and that a future multi-user
product would wrap). Standard library only: no pandas, so the panel adds
little memory next to the bots on a 1 GB VM.

Every function tolerates missing, half-written or corrupt files (the bots
may be writing them at the same moment) and returns empty data instead of
raising. Reads are bounded: big files are read from the end, in chunks.

Bots: the BTC bot keeps its files at the repository root (service
trading-bot, logs/bot.log); each extra coin from scripts/install_coin.sh has
a folder bots/<id>/ (service trading-bot-<id>, logs/bot-<id>.log).
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PARAMS_PATH = ROOT / "strategy.json"
LOG_DIR = ROOT / "logs"
BOTS_DIR = ROOT / "bots"
# Research tools, besides the trading bots
TOOL_LOGS = {"scanner": "scanner.log", "maker": "maker.log"}
TOOL_SERVICES = ("pair-scanner", "maker-bot")
_BOT_ID = re.compile(r"^[a-z0-9]{2,10}$")
INITIAL_BALANCE = 10_000.0
# Heartbeats are every 5 min; older than this means the bot is not alive
STALE_HEARTBEAT_SEC = 15 * 60

_TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600,
               "4h": 14400, "1d": 86400, "1w": 604800}
_CHUNK = 64 * 1024
_SCAN_CAP = 8 * 1024 * 1024     # most bytes read backwards per search
_BAR_KINDS = ("warmup", "live")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _num(x: Any) -> float | None:
    """JSON-safe float (NaN/inf → None)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _epoch(value: Any) -> float | None:
    """ISO string / datetime / epoch → epoch seconds (UTC)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _num(value)
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _read_json(path: Path) -> dict | None:
    """A JSON object from disk; anything else (missing, corrupt, a list) → None."""
    try:
        obj = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _dict(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def _trade_log(session: dict | None) -> list[dict]:
    log = _dict(session).get("trade_log")
    return [f for f in log if isinstance(f, dict)] if isinstance(log, list) else []


def tail_lines(path: Path, max_lines: int = 200, max_bytes: int = 512_000) -> list[str]:
    """Last lines of a (possibly huge) text file, reading only its end."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - max_bytes)
            fh.seek(start)
            chunk = fh.read()
    except OSError:
        return []
    lines = chunk.decode("utf-8", "replace").splitlines()
    if start > 0 and len(lines) > 1:
        lines = lines[1:]          # first line is probably cut
    elif start > 0 and lines:
        lines = ["…" + lines[0]]   # one huge line: show its end, not nothing
    return lines[-max_lines:]


def _reverse_lines(path: Path, min_offset: int = 0, max_bytes: int = _SCAN_CAP) -> Iterator[str]:
    """
    Lines of a text file from the end backwards, read in chunks. Stops at
    min_offset or after max_bytes; a line cut by either bound is skipped.
    """
    try:
        fh = open(path, "rb")
    except OSError:
        return
    with fh:
        pos = fh.seek(0, os.SEEK_END)
        floor = max(min_offset, pos - max_bytes, 0)
        rest = b""
        while pos > floor:
            step = min(_CHUNK, pos - floor)
            pos -= step
            fh.seek(pos)
            parts = (fh.read(step) + rest).split(b"\n")
            rest = parts.pop(0)                     # may continue further back
            for raw in reversed(parts):
                if raw.strip():
                    yield raw.decode("utf-8", "replace")
        if pos == 0 and rest.strip():
            yield rest.decode("utf-8", "replace")


# (path, event) -> (inode, size scanned, record): a later call only scans
# what was appended since, so finding an old event stays cheap
_event_cache: dict[tuple[str, str], tuple[int, int, dict | None]] = {}


def _last_event(path: Path, event: str) -> dict | None:
    """Newest JSON log record with this event name."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), event)
    cached = _event_cache.get(key)
    floor = 0
    if cached and cached[0] == st.st_ino and cached[1] <= st.st_size:
        if cached[1] == st.st_size:
            return cached[2]
        floor = max(0, cached[1] - _CHUNK)          # overlap: a line may straddle
    found = None
    needle = f'"{event}"'
    for line in _reverse_lines(path, min_offset=floor):
        if needle not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("event") == event:
            found = rec
            break
    if found is None and floor and cached:
        found = cached[2]
    _event_cache[key] = (st.st_ino, st.st_size, found)
    return found


# ---------------------------------------------------------------------------
# bot instances
# ---------------------------------------------------------------------------

def _runner() -> dict:
    """Coins running together in the trading-bots service (headless_main
    --coins writes bots/runner.json when it starts), if any."""
    info = _read_json(BOTS_DIR / "runner.json") or {}
    coins = info.get("coins")
    return {"service": str(info.get("service") or "trading-bots"),
            "coins": {str(c) for c in coins} if isinstance(coins, list) else set()}


def bots() -> list[dict]:
    """Every trading bot: BTC at the root, then bots/<id>/ in name order.
    service = the systemd unit running it: trading-bots for the coins of the
    shared process, else its own unit (trading-bot, trading-bot-<id>)."""
    runner = _runner()

    def service(bot_id: str, own: str) -> str:
        return runner["service"] if bot_id in runner["coins"] else own

    out = [{"id": "btc", "dir": ROOT, "log": "bot", "service": service("btc", "trading-bot")}]
    try:
        dirs = sorted(d for d in BOTS_DIR.iterdir()
                      if d.is_dir() and _BOT_ID.match(d.name) and d.name != "btc")
    except OSError:
        dirs = []
    for d in dirs:
        out.append({"id": d.name, "dir": d, "log": f"bot-{d.name}",
                    "service": service(d.name, f"trading-bot-{d.name}")})
    return out


def get_bot(bot_id: str | None = None) -> dict:
    """One bot by id (default BTC); KeyError if there is no such bot."""
    for b in bots():
        if b["id"] == (bot_id or "btc"):
            return b
    raise KeyError(bot_id)


def log_files() -> dict[str, str]:
    """Readable logs: one per bot (bot, bot-eth, ...), the shared process's
    (bots: every coin's lines) and the tools'."""
    shared = {"bots": "bots.log"} if _runner()["coins"] else {}
    return {b["log"]: b["log"] + ".log" for b in bots()} | shared | TOOL_LOGS


def services() -> tuple[str, ...]:
    """Each systemd unit once, bots first."""
    return tuple(dict.fromkeys(b["service"] for b in bots())) + TOOL_SERVICES


def _session(b: dict) -> dict | None:
    return _read_json(b["dir"] / "session.json")


def _symbol(b: dict) -> str:
    """Coin of a bot: its session, else its last start, else the folder name."""
    sym = _dict(_dict(_session(b)).get("meta")).get("symbol")
    if not sym:
        sym = (_last_event(LOG_DIR / (b["log"] + ".log"), "bot_started") or {}).get("symbol")
    return str(sym or f"{b['id'].upper()}/USDT")


# ---------------------------------------------------------------------------
# strategy / session
# ---------------------------------------------------------------------------

def strategy_params(b: dict | None = None) -> dict:
    """Parameters of the live session (session meta), else the bot's own
    strategy.json, else the repository's."""
    b = b or get_bot()
    stored = _dict(_dict(_dict(_session(b)).get("meta")).get("strategy_params"))
    if stored:
        return stored
    return _read_json(b["dir"] / "strategy.json") or _read_json(PARAMS_PATH) or {}


def _pair_fills(trade_log: list[dict]) -> tuple[list[dict], dict | None]:
    """
    BUY → SELL pairs from the paper engine's fill log, plus the BUY of the
    position still open (None if flat). Net result of a trade = change in
    cash between consecutive closes (entry and exit fees included), the same
    definition run_comparison.py uses.
    """
    trips, entry, prev_cash = [], None, INITIAL_BALANCE
    for fill in trade_log:
        side = str(fill.get("side", "")).upper()
        if side == "BUY" and "closed_pnl" not in fill:
            entry = fill
        elif "closed_pnl" in fill:
            cash = _num(fill.get("balance_after"))
            if cash is None:
                continue
            net = cash - prev_cash
            trips.append({
                "entry_time": _epoch(entry.get("timestamp")) if entry else None,
                "entry_price": _num(entry.get("price")) if entry else None,
                "exit_time": _epoch(fill.get("timestamp")),
                "exit_price": _num(fill.get("price")),
                "qty": _num(fill.get("quantity")),
                "net": net,
                "net_pct": net / prev_cash if prev_cash else None,
                "balance_after": cash,
            })
            prev_cash, entry = cash, None
    pending = None
    if entry is not None:
        pending = {"entry_time": _epoch(entry.get("timestamp")),
                   "entry_price": _num(entry.get("price"))}
    return trips, pending


def round_trips(trade_log: list[dict]) -> list[dict]:
    """Closed BUY → SELL trades (see _pair_fills)."""
    return _pair_fills(trade_log)[0]


def _metrics(trips: list[dict]) -> dict:
    nets = [t["net"] for t in trips]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    peak, max_dd, equity = INITIAL_BALANCE, 0.0, INITIAL_BALANCE
    for t in trips:
        equity = t["balance_after"]
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak else 0.0)
    pf = (sum(wins) / abs(sum(losses))) if losses else None
    return {
        "trades": len(trips),
        "win_rate": len(wins) / len(trips) if trips else None,
        "profit_factor": _num(pf),
        "net": equity - INITIAL_BALANCE,
        "return_pct": (equity - INITIAL_BALANCE) / INITIAL_BALANCE,
        "max_drawdown": max_dd,
    }


def bot_overview(b: dict | None = None) -> dict:
    """Session, position, trades, metrics and equity curve of one bot."""
    b = b or get_bot()
    session = _session(b)
    hb = _last_event(LOG_DIR / (b["log"] + ".log"), "heartbeat")
    params = strategy_params(b)
    if session is None:
        return {"available": False, "id": b["id"], "symbol": _symbol(b),
                "params": params, "heartbeat": hb}
    log = _trade_log(session)
    trades = round_trips(log)
    balance = _num(session.get("balance")) or 0.0
    pos = _dict(session.get("open_position"))
    last_price = _num(hb.get("price")) if hb else None
    position = None
    equity = balance
    if str(pos.get("side", "FLAT")).upper() == "LONG" and (_num(pos.get("size")) or 0) > 0:
        size = _num(pos.get("size")) or 0.0
        entry = _num(pos.get("entry_price")) or 0.0
        mark = last_price or entry
        equity = balance + size * mark
        position = {
            "side": "LONG", "size": size, "entry_price": entry, "mark_price": mark,
            "stop_price": _num(pos.get("trailing_stop_price")),
            "unrealized": size * (mark - entry),
            "unrealized_pct": (mark / entry - 1) if entry else None,
        }
    # Starts at the capital on the first fill, so an open first trade
    # already draws a line from the start to now
    curve = [{"time": _epoch(log[0].get("timestamp")), "value": INITIAL_BALANCE}] if log else []
    curve += [{"time": t["exit_time"], "value": t["balance_after"]} for t in trades]
    curve.append({"time": time.time(), "value": equity})
    curve = [p for p in curve if p["time"] is not None]
    return {
        "available": True,
        "id": b["id"],
        "symbol": _symbol(b),
        "params": params,
        "heartbeat": hb,
        "balance": balance,
        "equity": equity,
        "position": position,
        "trades": trades,
        "metrics": _metrics(trades),
        "equity_curve": curve,
        "halted": bool(_dict(_dict(session.get("meta")).get("breaker")).get("halted", False)),
    }


# ---------------------------------------------------------------------------
# candles + indicators
# ---------------------------------------------------------------------------

def _indicator_warmup(params: dict) -> int:
    """Bars needed before the first indicator value shown."""
    if params.get("strategy", "trend_v4") == "trend_v4":
        return max(_int(params.get("donchian_entry"), 20), _int(params.get("donchian_exit"), 10),
                   _int(params.get("trend_sma"), 200))
    return 200


def _int(x: Any, default: int) -> int:
    try:
        return max(0, int(x))
    except (TypeError, ValueError):
        return default


def bots_summary() -> dict:
    """One line per bot for the coin selector, plus the combined account."""
    rows = []
    for b in bots():
        o = bot_overview(b)
        hb = o.get("heartbeat")
        ts = _epoch(hb.get("timestamp")) if hb else None
        equity = o.get("equity") if o.get("available") else None
        rows.append({
            "id": b["id"], "symbol": o["symbol"], "service": b["service"], "log": b["log"],
            "equity": equity,
            "return_pct": (equity / INITIAL_BALANCE - 1) if equity is not None else None,
            "position": (o.get("position") or {}).get("side") if o.get("available") else None,
            "trades": (o.get("metrics") or {}).get("trades", 0),
            "fresh": ts is not None and time.time() - ts < STALE_HEARTBEAT_SEC,
        })
    live = [r for r in rows if r["equity"] is not None]
    total = sum(r["equity"] for r in live)
    return {"bots": rows,
            "total": {"equity": total if live else None, "accounts": len(live),
                      "return_pct": total / (INITIAL_BALANCE * len(live)) - 1 if live else None}}


def _bar_fields(path: Path) -> int:
    """8 columns (with kind/seg); legacy files from before that have 6."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            header = fh.readline(4096)
    except OSError:
        return 8
    return 6 if header.startswith("timestamp") and "kind" not in header else 8


def _parse_bar(line: str, fields: int) -> dict | None:
    rec = line.replace("\0", "").strip().split(",")
    # A row cut by a hard kill or read mid-write has fewer columns
    if len(rec) != fields or (fields == 8 and (rec[6] not in _BAR_KINDS or not rec[7])):
        return None
    t = _epoch(rec[0])
    vals = [_num(x) for x in rec[1:5]]
    if t is None or None in vals:
        return None
    o, h, lo, c = vals
    if not lo <= min(o, c) <= max(o, c) <= h:
        return None
    return {"time": t, "open": o, "high": h, "low": lo, "close": c}


def candles(limit: int = 500, b: dict | None = None) -> dict:
    """
    Closed bars the live bot traded on (live_bars.csv), oldest first, with
    the strategy's indicators and the trade markers.
    """
    b = b or get_bot()
    bars_path = b["dir"] / "live_bars.csv"
    limit = max(10, min(int(limit), 2000))
    params = strategy_params(b)
    tf = str(params.get("timeframe", "4h"))
    want = limit + _indicator_warmup(params)
    fields = _bar_fields(bars_path)
    # Read backwards until enough distinct bars: every restart re-appends a
    # 200-bar warm-up snapshot, so a fixed line count would come up short.
    # The newest row of a bar wins.
    bars: dict[float, dict] = {}
    for line in _reverse_lines(bars_path):
        if line.startswith("timestamp"):
            continue
        bar = _parse_bar(line, fields)
        if bar is not None and bar["time"] not in bars:
            bars[bar["time"]] = bar
            if len(bars) >= want:
                break
    series = [bars[k] for k in sorted(bars)]
    indicators = _indicators(series, params)
    series = series[-limit:]
    first = series[0]["time"] if series else 0
    for name in indicators:
        indicators[name] = [p for p in indicators[name] if p["time"] >= first]
    trips, pending = _pair_fills(_trade_log(_session(b)))
    return {
        "id": b["id"],
        "symbol": _symbol(b),
        "strategy": params.get("strategy", "trend_v4"),
        "timeframe": tf,
        "timeframe_sec": _TF_SECONDS.get(tf),
        "bars": series,
        "indicators": indicators,
        "markers": _markers(trips, pending, first),
    }


def _indicators(bars: list[dict], params: dict) -> dict[str, list[dict]]:
    """The lines the strategy trades on, computed like the live indicators."""
    out: dict[str, list[dict]] = {}
    if params.get("strategy", "trend_v4") == "trend_v4":
        # Donchian channels over the PREVIOUS N bars, like the live indicator
        n_in = max(1, _int(params.get("donchian_entry"), 20))
        n_out = max(1, _int(params.get("donchian_exit"), 10))
        n_sma = _int(params.get("trend_sma"), 200)
        out[f"Máx {n_in}"] = [
            {"time": bars[i]["time"], "value": max(b["high"] for b in bars[i - n_in:i])}
            for i in range(n_in, len(bars))]
        out[f"Mín {n_out}"] = [
            {"time": bars[i]["time"], "value": min(b["low"] for b in bars[i - n_out:i])}
            for i in range(n_out, len(bars))]
        if n_sma:
            out[f"MM {n_sma}"] = _sma(bars, n_sma)
    else:
        # ma_cross_v3: EMA 9 crossing SMA 20, filtered by SMA 200
        out["MME 9"] = _ema(bars, 9)
        out["MM 20"] = _sma(bars, 20)
        out["MM 200"] = _sma(bars, 200)
    return out


def _sma(bars: list[dict], n: int) -> list[dict]:
    res, acc = [], 0.0
    for i, b in enumerate(bars):
        acc += b["close"]
        if i >= n:
            acc -= bars[i - n]["close"]
        if i >= n - 1:
            res.append({"time": b["time"], "value": acc / n})
    return res


def _ema(bars: list[dict], n: int) -> list[dict]:
    """Seeded with the first close, like pandas ewm(adjust=False) in
    core/indicators.EMAIndicator; the first n values are left out."""
    alpha, ema, res = 2.0 / (n + 1.0), None, []
    for i, b in enumerate(bars):
        ema = b["close"] if ema is None else alpha * b["close"] + (1 - alpha) * ema
        if i >= n:
            res.append({"time": b["time"], "value": ema})
    return res


def _markers(trips: list[dict], pending: dict | None, first: float) -> list[dict]:
    marks = []
    for t in trips + ([pending] if pending else []):
        if t["entry_time"] and t["entry_time"] >= first:
            marks.append({"time": t["entry_time"], "kind": "entry", "price": t["entry_price"]})
        if t.get("exit_time") and t["exit_time"] >= first:
            marks.append({"time": t["exit_time"], "kind": "exit", "price": t["exit_price"],
                          "net": t["net"]})
    return sorted(marks, key=lambda m: m["time"])


# ---------------------------------------------------------------------------
# services / heartbeats / system
# ---------------------------------------------------------------------------

def heartbeats() -> dict:
    """Last heartbeat of every bot and tool, keyed by log name."""
    now = time.time()
    out = {}
    for name, file in log_files().items():
        hb = _last_event(LOG_DIR / file, "heartbeat")
        ts = _epoch(hb.get("timestamp")) if hb else None
        age = now - ts if ts is not None else None
        out[name] = {"heartbeat": hb, "age_sec": age,
                     "fresh": age is not None and age < STALE_HEARTBEAT_SEC}
    return out


def system_info() -> dict:
    info: dict[str, Any] = {}
    try:
        mem = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, val = line.split(":", 1)
            mem[key] = int(val.split()[0]) * 1024
        info["mem_total"] = mem.get("MemTotal")
        info["mem_available"] = mem.get("MemAvailable")
    except (OSError, ValueError, IndexError):
        pass
    try:
        info["load_1m"] = os.getloadavg()[0]
    except OSError:
        pass
    try:
        info["uptime_sec"] = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    return info


def log_tail(name: str, lines: int = 100) -> list[str]:
    """Last lines of one bot's log (only the known log files can be read)."""
    file = log_files().get(name)
    if file is None:
        raise KeyError(name)
    return tail_lines(LOG_DIR / file, max_lines=max(1, min(int(lines), 1000)))


# ---------------------------------------------------------------------------
# scanner / maker research
# ---------------------------------------------------------------------------

def scanner_report() -> dict:
    from pair_scanner import STATS_PATH
    from run_scanner_report import scanner_rows

    data = _read_json(STATS_PATH)
    if not data:
        return {"available": False, "rows": []}
    try:
        rows = []
        for r in scanner_rows(data):
            rows.append({k: (_num(v) if isinstance(v, float) else v)
                         for k, v in r.items() if k != "touch"}
                        | {"touch": {str(h): _num(v) for h, v in r["touch"].items()}})
    except (AttributeError, KeyError, TypeError, ValueError, ZeroDivisionError):
        return {"available": False, "rows": [], "error": "scanner_stats.json ilegível"}
    return {"available": True, "updated": data.get("updated"), "rows": rows}


class _VariantStats:
    """Running totals of one maker variant (no per-trade objects kept)."""

    def __init__(self) -> None:
        self.n = 0
        self.mean = 0.0
        self.m2 = 0.0          # Welford: sum of squared deviations
        self.wins = 0
        self.fees = 0.0
        self.forced = 0
        self.notional = 0.0
        self.marks = {s: [0.0, 0] for s in (1, 30)}

    def add(self, pnl: float, fees: float, notional: float, exit_kind: str,
            marks: dict[int, float]) -> None:
        self.n += 1
        delta = pnl - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (pnl - self.mean)
        self.wins += pnl > 0
        self.fees += fees
        self.forced += exit_kind != "maker"
        self.notional += notional
        for s, v in marks.items():
            if s in self.marks:
                self.marks[s][0] += v
                self.marks[s][1] += 1

    def row(self, name: str) -> dict:
        from run_maker_report import verdict_from

        if not self.n:
            return {"variant": name, "trades": 0, "pnl": None, "per_trade_pct": None,
                    "win_rate": None, "fees": None, "forced_exits": 0,
                    "markout_1s": None, "markout_30s": None, "verdict": "sem trades"}
        sd = math.sqrt(self.m2 / (self.n - 1)) if self.n > 1 else 0.0
        avg_notional = self.notional / self.n
        mk = {s: (tot / cnt if cnt else None) for s, (tot, cnt) in self.marks.items()}
        return {
            "variant": name, "trades": self.n, "pnl": _num(self.mean * self.n),
            "per_trade_pct": _num(self.mean / avg_notional) if avg_notional else None,
            "win_rate": self.wins / self.n, "fees": _num(self.fees),
            "forced_exits": self.forced,
            "markout_1s": _num(mk[1]), "markout_30s": _num(mk[30]),
            "verdict": verdict_from(self.n, self.mean, sd),
        }


# (inode, mtime, size) -> result: the Maker tab polls every 30 s
_maker_cache: dict[str, Any] = {}


def maker_report() -> dict:
    """Per-variant summary of maker_trades.csv, streamed in constant memory."""
    from maker_bot import TRADES_PATH, VARIANTS

    try:
        st = TRADES_PATH.stat()
    except OSError:
        return {"available": False, "variants": []}
    sig = (st.st_ino, st.st_mtime_ns, st.st_size)
    if _maker_cache.get("sig") == sig:
        return _maker_cache["out"]
    stats = {name: _VariantStats() for name in VARIANTS}
    try:
        with open(TRADES_PATH, newline="", encoding="utf-8", errors="replace") as fh:
            for row in csv.DictReader(line.replace("\0", "") for line in fh):
                try:
                    # Same validation as maker_bot.load_trips
                    datetime.fromisoformat(row["entry_time"])
                    datetime.fromisoformat(row["exit_time"])
                    price, qty = float(row["entry_price"]), float(row["qty"])
                    pnl, fees = float(row["pnl"]), float(row["fees"])
                    marks = {s: float(row[f"m{s}"]) for s in (1, 30) if row.get(f"m{s}")}
                    exit_kind = row["exit_kind"]
                except (KeyError, ValueError, TypeError):
                    continue
                vs = stats.get(row.get("variant") or "base")
                if vs is not None:
                    vs.add(pnl, fees, price * qty, exit_kind, marks)
    except (OSError, csv.Error) as e:
        return {"available": False, "variants": [], "error": f"maker_trades.csv ilegível: {e}"}
    out = {"available": True, "variants": [stats[name].row(name) for name in VARIANTS]}
    _maker_cache.update(sig=sig, out=out)
    return out
