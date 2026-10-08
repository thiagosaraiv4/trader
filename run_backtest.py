"""
run_backtest.py
----------------
Backtest da Strategy V3 — a mesma do robô ao vivo, com os mesmos parâmetros —
em candles recentes de 1 minuto.

Uso:
    python run_backtest.py                             # últimos 90 dias, 1m, Binance
    python run_backtest.py --days 30
    python run_backtest.py --start 2026-07-01 --end 2026-10-01
    python run_backtest.py --exchange bybit --timeframe 5m

Datas em UTC. Sem --end, o período termina hoje às 00:00 UTC (assim rodar de
novo no mesmo dia reaproveita o cache em backtest_cache/).

Nunca usa dados sintéticos: se o download falhar o script para com erro.
A Binance e a Bybit bloqueiam servidores nos EUA (caso do Codespaces) —
rode do seu computador.

No Qt required — runs in headless environments.
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.backtest_engine import BacktestEngine, BacktestResult, fetch_ohlcv_history
from core.config import AppConfig
from core.types import BarData
from utils.logger import configure_logging

_CACHE_DIR = Path(__file__).resolve().parent / "backtest_cache"
_WARMUP_BARS = BacktestEngine._WARMUP_BARS


def _parse_date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise argparse.ArgumentTypeError(f"data inválida {value!r} (use AAAA-MM-DD)")


def _cache_path(exchange: str, symbol: str, timeframe: str, start: datetime, end: datetime) -> Path:
    name = f"{exchange}_{symbol.replace('/', '')}_{timeframe}_{start:%Y%m%d%H%M}_{end:%Y%m%d%H%M}.csv"
    return _CACHE_DIR / name


def _read_cache(path: Path) -> list[BarData]:
    bars: list[BarData] = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            bars.append(BarData(
                timestamp=datetime.fromtimestamp(int(row["ts_ms"]) / 1000, tz=timezone.utc),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            ))
    return bars


def _write_cache(path: Path, bars: list[BarData]) -> None:
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(("ts_ms", "open", "high", "low", "close", "volume"))
        for b in bars:
            writer.writerow((int(b.timestamp.timestamp() * 1000), b.open, b.high, b.low, b.close, b.volume))
    tmp.replace(path)


def _coverage_problem(bars: list[BarData], data_start: datetime, end: datetime, tf: timedelta) -> str | None:
    """Why the bars don't cover [data_start, end), or None if they do."""
    if not bars:
        return "nenhum candle recebido"
    tolerance = 5 * tf
    if bars[0].timestamp > data_start + tolerance or bars[-1].timestamp < end - tf - tolerance:
        return (f"recebidos só {bars[0].timestamp:%Y-%m-%d %H:%M} → "
                f"{bars[-1].timestamp:%Y-%m-%d %H:%M} UTC")
    return None


def _gap_summary(bars: list[BarData], tf: timedelta) -> str | None:
    """Missing candles inside the range (exchange maintenance etc.), if any."""
    missing = 0
    largest = timedelta(0)
    for prev, cur in zip(bars, bars[1:]):
        gap = cur.timestamp - prev.timestamp
        if gap > tf:
            missing += int(gap / tf) - 1
            largest = max(largest, gap - tf)
    if not missing:
        return None
    return f"{missing:,} candles faltando no período (maior buraco: {largest})"


def _load_bars(args: argparse.Namespace, data_start: datetime, end: datetime, tf: timedelta) -> list[BarData]:
    """
    Cached download of [data_start, end). Exits with an explanation if the
    exchange is unreachable or does not have the whole period. Only complete
    downloads are cached.
    """
    path = _cache_path(args.exchange, args.symbol, args.timeframe, data_start, end)
    if path.exists() and not args.no_cache:
        bars = _read_cache(path)
        if _coverage_problem(bars, data_start, end, tf) is None:
            print(f"[*] Usando cache: {path.name}")
            return bars
        print(f"[*] Cache incompleto ({path.name}) — baixando de novo")

    print(f"[*] Baixando {args.symbol} {args.timeframe} da {args.exchange} "
          f"({data_start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M} UTC)...")
    started = time.monotonic()
    try:
        bars = fetch_ohlcv_history(
            args.symbol, args.timeframe, data_start, end, args.exchange,
            on_progress=lambda n: print(f"\r    {n:,} candles", end="", flush=True),
        )
    except Exception as exc:
        print(f"\n[!] Falha ao baixar dados da {args.exchange}: {exc}")
        print("    Binance e Bybit bloqueiam servidores nos EUA (ex.: GitHub Codespaces).")
        print("    Rode do seu computador ou tente outra exchange com --exchange.")
        sys.exit(1)
    print(f"\r    {len(bars):,} candles em {time.monotonic() - started:.0f}s")

    problem = _coverage_problem(bars, data_start, end, tf)
    if problem is not None:
        print(f"[!] A {args.exchange} não tem o período inteiro: {problem}.")
        print("    Algumas exchanges (ex.: Kraken) só servem as últimas horas em 1m.")
        print("    Use outra exchange ou um --start/--end dentro do que ela tem.")
        sys.exit(1)

    _write_cache(path, bars)
    return bars


def _net_pnls(result: BacktestResult, initial: float) -> list[float]:
    """Per-trade PnL after fees: each trade starts and ends flat, so the
    change in cash between consecutive closes is exactly its net result."""
    pnls: list[float] = []
    prev = initial
    for t in result.trade_log:
        pnls.append(t.balance_after - prev)
        prev = t.balance_after
    return pnls


def _print_report(result: BacktestResult, bars: list[BarData], initial: float) -> None:
    print(result.summary())

    sim_bars = bars[_WARMUP_BARS:]
    gross = sum(t.profit for t in result.trade_log)
    net = result.final_balance - initial
    fees = gross - net
    buy_hold = float(sim_bars[-1].close) / float(sim_bars[0].open) - 1
    net_pnls = _net_pnls(result, initial)
    net_wins = [p for p in net_pnls if p > 0]
    net_losses = [p for p in net_pnls if p < 0]

    print(f"  Período:         {sim_bars[0].timestamp:%Y-%m-%d %H:%M} → "
          f"{sim_bars[-1].timestamp:%Y-%m-%d %H:%M} UTC ({len(sim_bars):,} candles)")
    print(f"  PnL bruto:       ${gross:,.2f}")
    print(f"  Taxas pagas:     ${fees:,.2f} ({fees / initial:.2%} do capital"
          + (f", ${fees / result.total_trades:,.2f}/trade)" if result.total_trades else ")"))
    print(f"  PnL líquido:     ${net:,.2f} ({net / initial:.2%})")
    if net_pnls:
        net_pf = (sum(net_wins) / abs(sum(net_losses))) if net_losses else float("inf")
        print(f"  Win rate líq.:   {len(net_wins) / len(net_pnls):.1%} (após taxas; o Win Rate acima é bruto)")
        print(f"  Profit factor líq.: {net_pf:.2f}")
    print(f"  Buy & hold BTC:  {buy_hold:.2%} no mesmo período")
    print("=" * 60)

    if not result.trade_log:
        return

    # Per-month breakdown: consistency matters more than the total
    by_month: dict[str, list] = defaultdict(list)
    for t, net_pnl in zip(result.trade_log, net_pnls):
        by_month[f"{t.exit_time:%Y-%m}"].append((t, net_pnl))

    print(f"  {'Mês':<8} {'Trades':>6} {'Win%líq':>7} {'PnL bruto':>11} {'Líquido':>9}")
    prev_balance = initial
    for month in sorted(by_month):
        entries = by_month[month]
        trades = [t for t, _ in entries]
        wins = sum(1 for _, net_pnl in entries if net_pnl > 0)
        month_gross = sum(t.profit for t in trades)
        end_balance = trades[-1].balance_after
        month_net = (end_balance - prev_balance) / prev_balance if prev_balance else 0.0
        print(f"  {month:<8} {len(trades):>6} {wins / len(trades):>7.0%} "
              f"{month_gross:>11,.2f} {month_net:>9.2%}")
        prev_balance = end_balance
    print("=" * 60)


def main() -> None:
    cfg = AppConfig()
    parser = argparse.ArgumentParser(description="Backtest da Strategy V3 em dados recentes.")
    parser.add_argument("--days", type=int, default=90, help="dias até --end (padrão: 90)")
    parser.add_argument("--start", type=_parse_date, help="início AAAA-MM-DD (UTC); ignora --days")
    parser.add_argument("--end", type=_parse_date, help="fim AAAA-MM-DD (UTC, exclusivo); padrão: hoje 00:00")
    parser.add_argument("--timeframe", default=cfg.default_timeframe.value, help="padrão: o do robô ao vivo")
    parser.add_argument("--symbol", default=str(cfg.default_symbol))
    parser.add_argument("--exchange", default="binance", help="id ccxt (padrão: binance)")
    parser.add_argument("--balance", type=float, default=10_000.0, help="capital inicial (padrão: 10000)")
    parser.add_argument("--no-cache", action="store_true", help="baixa de novo mesmo com cache")
    args = parser.parse_args()

    # Only warnings from the engine (it logs every signal at INFO)
    configure_logging(level=logging.WARNING)

    probe = BacktestEngine(timeframe=args.timeframe)
    tf = timedelta(minutes=probe._timeframe_to_minutes())

    now = datetime.now(timezone.utc)
    end = args.end or now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = args.start or end - timedelta(days=args.days)
    if start >= end:
        parser.error("o início precisa ser antes do fim")
    # An --end in the future stops at the last closed candle (also in the cache key)
    last_close = datetime.fromtimestamp(
        int(now.timestamp()) // int(tf.total_seconds()) * int(tf.total_seconds()), tz=timezone.utc,
    )
    if end > last_close:
        end = last_close
        print(f"[i] --end no futuro: usando até {end:%Y-%m-%d %H:%M} UTC")
        if start >= end:
            parser.error("o início está no futuro")
    data_start = start - _WARMUP_BARS * tf  # indicators warm up before the tested period

    print("=" * 60)
    print("BACKTEST — STRATEGY V3 (mesma do robô ao vivo)")
    print("=" * 60)

    bars = _load_bars(args, data_start, end, tf)
    if len(bars) < _WARMUP_BARS + 10:
        print(f"[!] Dados insuficientes: {len(bars)} candles (mínimo {_WARMUP_BARS + 10}).")
        sys.exit(1)
    gaps = _gap_summary(bars, tf)
    if gaps:
        print(f"[i] {gaps}")

    print("[*] Simulando...")
    engine = BacktestEngine(
        symbol=args.symbol,
        timeframe=args.timeframe,
        initial_balance=args.balance,
        bars=bars,
    )
    result = engine.run()
    result.data_source = f"ccxt/{args.exchange}"
    _print_report(result, bars, args.balance)


if __name__ == "__main__":
    main()
