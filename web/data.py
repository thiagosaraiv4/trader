"""
web/data.py
-----------
Read-only access to everything the bots write, as plain JSON-ready dicts —
the single layer the web panel's API serves (and that a future multi-user
product would wrap). Standard library only: no pandas, so the panel adds
little memory next to the bots on a 1 GB VM.

Every function tolerates missing or half-written files (the bots may be
writing them at the same moment) and returns empty data instead of raising.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SESSION_PATH = ROOT / "session.json"
BARS_PATH = ROOT / "live_bars.csv"
PARAMS_PATH = ROOT / "strategy.json"
LOG_DIR = ROOT / "logs"
LOGS = {"bot": "bot.log", "scanner": "scanner.log", "maker": "maker.log"}
SERVICES = ("trading-bot", "pair-scanner", "maker-bot")
INITIAL_BALANCE = 10_000.0
# Heartbeats are every 5 min; older than this means the bot is not alive
STALE_HEARTBEAT_SEC = 15 * 60

_TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600,
               "4h": 14400, "1d": 86400, "1w": 604800}


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
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
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
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


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
    if start > 0 and lines:
        lines = lines[1:]          # first line is probably cut
    return lines[-max_lines:]


def _last_event(path: Path, event: str) -> dict | None:
    for line in reversed(tail_lines(path, max_lines=2000)):
        if f'"{event}"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("event") == event:
            return rec
    return None


# ---------------------------------------------------------------------------
# strategy / session
# ---------------------------------------------------------------------------

def strategy_params() -> dict:
    """Parameters of the live session (session meta), else strategy.json."""
    session = _read_json(SESSION_PATH) or {}
    stored = (session.get("meta") or {}).get("strategy_params")
    if stored:
        return stored
    return _read_json(PARAMS_PATH) or {}


def round_trips(trade_log: list[dict]) -> list[dict]:
    """
    BUY → SELL pairs from the paper engine's fill log. Net result of a trade
    = change in cash between consecutive closes (entry and exit fees
    included), the same definition run_comparison.py uses.
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
    return trips


def _metrics(trips: list[dict]) -> dict:
    nets = [t["net"] for t in trips]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    peak, max_dd, equity = INITIAL_BALANCE, 0.0, INITIAL_BALANCE
    for t in trips:
        equity = t["balance_after"]
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
    pf = (sum(wins) / abs(sum(losses))) if losses else None
    return {
        "trades": len(trips),
        "win_rate": len(wins) / len(trips) if trips else None,
        "profit_factor": _num(pf),
        "net": equity - INITIAL_BALANCE,
        "return_pct": (equity - INITIAL_BALANCE) / INITIAL_BALANCE,
        "max_drawdown": max_dd,
    }


def bot_overview() -> dict:
    """Session, position, trades, metrics and equity curve of the main bot."""
    session = _read_json(SESSION_PATH)
    hb = _last_event(LOG_DIR / LOGS["bot"], "heartbeat")
    params = strategy_params()
    if session is None:
        return {"available": False, "params": params, "heartbeat": hb}
    trades = round_trips(session.get("trade_log") or [])
    balance = _num(session.get("balance")) or 0.0
    pos = session.get("open_position") or None
    last_price = _num(hb.get("price")) if hb else None
    position = None
    equity = balance
    if pos and str(pos.get("side", "FLAT")).upper() == "LONG" and (_num(pos.get("size")) or 0) > 0:
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
    curve = [{"time": _epoch(session.get("trade_log", [{}])[0].get("timestamp")) if trades else None,
              "value": INITIAL_BALANCE}] if trades else []
    curve += [{"time": t["exit_time"], "value": t["balance_after"]} for t in trades]
    curve.append({"time": time.time(), "value": equity})
    curve = [p for p in curve if p["time"] is not None]
    return {
        "available": True,
        "params": params,
        "heartbeat": hb,
        "balance": balance,
        "equity": equity,
        "position": position,
        "trades": trades,
        "metrics": _metrics(trades),
        "equity_curve": curve,
        "halted": bool(((session.get("meta") or {}).get("breaker") or {}).get("halted", False)),
    }


# ---------------------------------------------------------------------------
# candles + indicators
# ---------------------------------------------------------------------------

def candles(limit: int = 500) -> dict:
    """
    Closed bars the live bot traded on (live_bars.csv), oldest first, with
    the strategy's indicators and the trade markers.
    """
    limit = max(10, min(int(limit), 2000))
    params = strategy_params()
    tf = params.get("timeframe", "4h")
    # Read only the end of the file; ~70 bytes per row
    rows = tail_lines(BARS_PATH, max_lines=limit * 3 + 400, max_bytes=(limit * 3 + 400) * 120)
    bars: dict[float, dict] = {}
    for line in rows:
        if line.startswith("timestamp"):
            continue
        try:
            rec = next(csv.reader(io.StringIO(line)))
            t = _epoch(rec[0])
            o, h, lo, c = (float(x) for x in rec[1:5])
        except (StopIteration, ValueError, IndexError):
            continue
        if t is None:
            continue
        # The same bar can appear in several snapshots: keep the latest row
        bars[t] = {"time": t, "open": o, "high": h, "low": lo, "close": c}
    series = [bars[k] for k in sorted(bars)]
    indicators = _indicators(series, params)
    series = series[-limit:]
    first = series[0]["time"] if series else 0
    for name in indicators:
        indicators[name] = [p for p in indicators[name] if p["time"] >= first]
    trips = round_trips((_read_json(SESSION_PATH) or {}).get("trade_log") or [])
    started = _last_event(LOG_DIR / LOGS["bot"], "bot_started") or {}
    return {
        "symbol": started.get("symbol", "BTC/USDT"),
        "timeframe": tf,
        "timeframe_sec": _TF_SECONDS.get(tf),
        "bars": series,
        "indicators": indicators,
        "markers": _markers(trips, first),
    }


def _indicators(bars: list[dict], params: dict) -> dict[str, list[dict]]:
    """Donchian channels (previous N bars, like the live indicator) and SMA."""
    out: dict[str, list[dict]] = {}
    if params.get("strategy", "trend_v4") == "trend_v4":
        n_in = int(params.get("donchian_entry", 20))
        n_out = int(params.get("donchian_exit", 10))
        n_sma = int(params.get("trend_sma", 200))
        out[f"Máx {n_in}"] = [
            {"time": bars[i]["time"], "value": max(b["high"] for b in bars[i - n_in:i])}
            for i in range(n_in, len(bars))]
        out[f"Mín {n_out}"] = [
            {"time": bars[i]["time"], "value": min(b["low"] for b in bars[i - n_out:i])}
            for i in range(n_out, len(bars))]
        if n_sma:
            out[f"MM {n_sma}"] = _sma(bars, n_sma)
    else:
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


def _markers(trips: list[dict], first: float) -> list[dict]:
    marks = []
    for t in trips:
        if t["entry_time"] and t["entry_time"] >= first:
            marks.append({"time": t["entry_time"], "kind": "entry", "price": t["entry_price"]})
        if t["exit_time"] and t["exit_time"] >= first:
            marks.append({"time": t["exit_time"], "kind": "exit", "price": t["exit_price"],
                          "net": t["net"]})
    return sorted(marks, key=lambda m: m["time"])


# ---------------------------------------------------------------------------
# services / heartbeats / system
# ---------------------------------------------------------------------------

def heartbeats() -> dict:
    now = time.time()
    out = {}
    for name, file in LOGS.items():
        hb = _last_event(LOG_DIR / file, "heartbeat")
        age = now - (_epoch(hb.get("timestamp")) or 0) if hb else None
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
    except (OSError, ValueError):
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
    file = LOGS.get(name)
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
    rows = []
    for r in scanner_rows(data):
        rows.append({k: (_num(v) if isinstance(v, float) else v) for k, v in r.items() if k != "touch"}
                    | {"touch": {str(h): _num(v) for h, v in r["touch"].items()}})
    return {"available": True, "updated": data.get("updated"), "rows": rows}


def maker_report() -> dict:
    from core.maker_sim import summarize
    from maker_bot import TRADES_PATH, VARIANTS, load_trips
    from run_maker_report import _verdict

    if not TRADES_PATH.exists():
        return {"available": False, "variants": []}
    trips = load_trips()
    out = []
    for name in VARIANTS:
        vt = [t for t in trips if t.variant == name]
        s = summarize(vt)
        notional = sum(t.entry_price * t.qty for t in vt) / len(vt) if vt else None
        out.append({
            "variant": name, "trades": s["trades"], "pnl": _num(s.get("pnl")),
            "per_trade_pct": _num(s["avg_pnl"] / notional) if vt and notional else None,
            "win_rate": _num(s.get("win_rate")), "fees": _num(s.get("fees")),
            "forced_exits": sum(1 for t in vt if t.exit_kind != "maker"),
            "markout_1s": _num(s["markouts"][1]) if vt else None,
            "markout_30s": _num(s["markouts"][30]) if vt else None,
            "verdict": _verdict([t.pnl for t in vt]) if vt else "sem trades",
        })
    return {"available": True, "variants": out}
