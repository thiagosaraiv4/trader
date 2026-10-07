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


def _load_bars(args: argparse.Namespace, data_start: datetime, end: datetime) -> list[BarData]:
    """Cached download. Exits with an explanation if the exchange is unreachable."""
    path = _cache_path(args.exchange, args.symbol, args.timeframe, data_start, end)
    if path.exists() and not args.no_cache:
        print(f"[*] Usando cache: {path.name}")
        return _read_cache(path)

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

    if bars:
        _write_cache(path, bars)
    return bars


def _print_report(result: BacktestResult, bars: list[BarData], initial: float) -> None:
    print(result.summary())

    sim_bars = bars[_WARMUP_BARS:]
    gross = sum(t.profit for t in result.trade_log)
    net = result.final_balance - initial
    fees = gross - net
    buy_hold = float(sim_bars[-1].close) / float(sim_bars[0].open) - 1

    print(f"  Período:         {sim_bars[0].timestamp:%Y-%m-%d %H:%M} → "
          f"{sim_bars[-1].timestamp:%Y-%m-%d %H:%M} UTC ({len(sim_bars):,} candles)")
    print(f"  PnL bruto:       ${gross:,.2f}")
    print(f"  Taxas pagas:     ${fees:,.2f} ({fees / initial:.2%} do capital"
          + (f", ${fees / result.total_trades:,.2f}/trade)" if result.total_trades else ")"))
    print(f"  PnL líquido:     ${net:,.2f} ({net / initial:.2%})")
    print(f"  Buy & hold BTC:  {buy_hold:.2%} no mesmo período")
    print("=" * 60)

    if not result.trade_log:
        return

    # Per-month breakdown: consistency matters more than the total
    by_month: dict[str, list] = defaultdict(list)
    for t in result.trade_log:
        by_month[f"{t.exit_time:%Y-%m}"].append(t)

    print(f"  {'Mês':<8} {'Trades':>6} {'Win%':>6} {'PnL bruto':>11} {'Líquido':>9}")
    prev_balance = initial
    for month in sorted(by_month):
        trades = by_month[month]
        wins = sum(1 for t in trades if t.profit > 0)
        month_gross = sum(t.profit for t in trades)
        end_balance = trades[-1].balance_after
        month_net = (end_balance - prev_balance) / prev_balance if prev_balance else 0.0
        print(f"  {month:<8} {len(trades):>6} {wins / len(trades):>6.0%} "
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

    end = args.end or datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = args.start or end - timedelta(days=args.days)
    if start >= end:
        parser.error("o início precisa ser antes do fim")

    probe = BacktestEngine(timeframe=args.timeframe)
    tf = timedelta(minutes=probe._timeframe_to_minutes())
    data_start = start - _WARMUP_BARS * tf  # indicators warm up before the tested period

    print("=" * 60)
    print("BACKTEST — STRATEGY V3 (mesma do robô ao vivo)")
    print("=" * 60)

    bars = _load_bars(args, data_start, end)
    if len(bars) < _WARMUP_BARS + 10:
        print(f"[!] Dados insuficientes: {len(bars)} candles (mínimo {_WARMUP_BARS + 10}).")
        sys.exit(1)

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
