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
    python run_backtest.py --optimize                  # procura parâmetros e valida
    python run_backtest.py --strategy trend_v4 --optimize   # tendência (Donchian) em 4h, 4 anos
    python run_backtest.py --optimize --save           # ...e grava em strategy.json

Usa os parâmetros de strategy.json (os mesmos do robô ao vivo), se existir.

--optimize testa combinações de parâmetros nos primeiros 2/3 do período
(treino) e confere a melhor nos últimos 1/3, que ela nunca viu (validação).
Só o resultado da validação diz algo sobre o futuro.

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
import itertools
import logging
import sys
import time
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.backtest_engine import BacktestEngine, BacktestResult, fetch_ohlcv_history
from core.config import AppConfig
from core.strategy_params import PARAMS_PATH, STRATEGIES, StrategyParams, load_params, save_params
from core.types import BarData
from utils.logger import configure_logging

_CACHE_DIR = Path(__file__).resolve().parent / "backtest_cache"
_WARMUP_BARS = BacktestEngine._WARMUP_BARS

# Parameter grids for --optimize. "trail" = (trail_activation_pct, trail_atr).
_GRIDS: dict[str, dict[str, list]] = {
    # From the original (cautious) V3 values to looser entries / roomier exits
    "ma_cross_v3": {
        "atr_filter_min": [1.05, 0.9, 0.0],
        "cross_lookback": [1, 3],
        "exit_atr": [0.3, 0.6, 1.0],
        "stop_atr": [1.5, 2.5],
        "trail": [(0.003, 1.0), (0.006, 1.5)],
    },
    # Faster/slower channels, with/without trend filter, tighter/looser stops
    "trend_v4": {
        "donchian_entry": [20, 40, 55],
        "donchian_exit": [10, 20],
        "trend_sma": [0, 100, 200],
        "stop_atr": [2.0, 3.0],
        "trail": [(0.0, 3.0), (0.0, 4.5)],
    },
}
_DEFAULT_DAYS = {"ma_cross_v3": 90, "trend_v4": 2190}
# Fewer trades than this is indistinguishable from luck. Trend following on
# 4h trades rarely by design, so it gets lower minimums and a longer history.
_MIN_TRADES = {"ma_cross_v3": (30, 20), "trend_v4": (25, 12)}  # (train, validation)


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


def _grid(base: StrategyParams) -> list[StrategyParams]:
    """All combinations of the strategy's grid applied on top of `base`."""
    grid = _GRIDS[base.strategy]
    combos = []
    keys = list(grid)
    for values in itertools.product(*(grid[k] for k in keys)):
        d = dict(zip(keys, values))
        activation, trail_atr = d.pop("trail")
        combos.append(replace(base, **d, trail_activation_pct=activation, trail_atr=trail_atr))
    return combos


def _evaluate(bars: list[BarData], params: StrategyParams, args: argparse.Namespace) -> dict:
    """
    Backtest one parameter set; metrics are net of fees. The 7% circuit
    breaker is off here: it would cap every losing set at the same -7% and
    hide which is better or worse.
    """
    result = BacktestEngine(
        symbol=args.symbol,
        timeframe=params.timeframe,
        initial_balance=args.balance,
        bars=bars,
        params=params,
        circuit_breaker=False,
    ).run()
    pnls = _net_pnls(result, args.balance)
    return {
        "trades": len(pnls),
        "net_ret": (result.final_balance - args.balance) / args.balance,
        "win": (sum(1 for p in pnls if p > 0) / len(pnls)) if pnls else 0.0,
        "dd": result.max_drawdown,
    }


def _describe(p: StrategyParams) -> str:
    if p.strategy == "trend_v4":
        trend = f"SMA{p.trend_sma}" if p.trend_sma else "sem filtro"
        return (f"entrada máx {p.donchian_entry:<2} saída mín {p.donchian_exit:<2} tendência {trend:<10} "
                f"stop {p.stop_atr}ATR trail {p.trail_atr}ATR")
    return (f"atr≥{p.atr_filter_min:<4} cruz≤{p.cross_lookback} saída {p.exit_atr:<3}ATR "
            f"stop {p.stop_atr}ATR trail {p.trail_activation_pct:.1%}/{p.trail_atr}ATR")


def _buy_hold(bars: list[BarData]) -> float:
    """Return of buying at the first simulated open and holding to the end."""
    sim = bars[_WARMUP_BARS:]
    return float(sim[-1].close) / float(sim[0].open) - 1


def _buy_hold_dd(bars: list[BarData]) -> float:
    """Worst peak-to-trough fall (on closes) while holding."""
    peak, worst = 0.0, 0.0
    for b in bars[_WARMUP_BARS:]:
        c = float(b.close)
        peak = max(peak, c)
        worst = max(worst, (peak - c) / peak)
    return worst


def _fmt(m: dict) -> str:
    return f"{m['trades']:>4} trades {m['net_ret']:>+7.2%} win {m['win']:>4.0%} dd {m['dd']:>5.2%}"


def _optimize(bars: list[BarData], current: StrategyParams, args: argparse.Namespace) -> None:
    sim = len(bars) - _WARMUP_BARS
    cut = _WARMUP_BARS + (sim * 2) // 3
    train = bars[:cut]
    test = bars[cut - _WARMUP_BARS:]  # test keeps its own warm-up bars
    print(f"[*] Treino:    {train[_WARMUP_BARS].timestamp:%Y-%m-%d} → {train[-1].timestamp:%Y-%m-%d}")
    print(f"[*] Validação: {test[_WARMUP_BARS].timestamp:%Y-%m-%d} → {test[-1].timestamp:%Y-%m-%d}")

    candidates = _grid(current)
    # The engine warns about small samples on every run; the table already shows trade counts
    logging.getLogger().setLevel(logging.ERROR)
    print(f"[*] Testando {len(candidates)} combinações no treino...")
    scored = []
    for i, p in enumerate(candidates, 1):
        print(f"\r    {i}/{len(candidates)}", end="", flush=True)
        scored.append((p, _evaluate(train, p, args)))
    print()

    min_train, min_test = _MIN_TRADES[current.strategy]
    eligible = [(p, m) for p, m in scored if m["trades"] >= min_train]
    if not eligible:
        print(f"[!] Nenhuma combinação fez {min_train}+ trades no treino. Use um período maior (--days).")
        return
    eligible.sort(key=lambda pm: pm[1]["net_ret"], reverse=True)
    top = eligible[:5]

    print("\n" + "=" * 100)
    print("  Atual vs. 5 melhores NO TREINO (líquido de taxas) e como foram na VALIDAÇÃO")
    print("=" * 100)
    base_train, base_test = _evaluate(train, current, args), _evaluate(test, current, args)
    print(f"  COMPRAR E SEGURAR (referência, 100% do capital, sem taxas de giro)")
    print(f"         treino:            {_buy_hold(train):>+7.2%}          dd {_buy_hold_dd(train):>5.2%} | "
          f"validação:            {_buy_hold(test):>+7.2%}          dd {_buy_hold_dd(test):>5.2%}")
    print(f"  ATUAL  {_describe(current)}")
    print(f"         treino: {_fmt(base_train)} | validação: {_fmt(base_test)}")
    results = []
    for rank, (p, m_train) in enumerate(top, 1):
        m_test = _evaluate(test, p, args)
        results.append((p, m_train, m_test))
        print(f"  #{rank}     {_describe(p)}")
        print(f"         treino: {_fmt(m_train)} | validação: {_fmt(m_test)}")
    print("=" * 100)

    best, best_train, best_test = results[0]
    # Must make money in BOTH periods: a loser in training that "wins" in
    # validation is noise, not an edge
    validated = (
        best_train["net_ret"] > 0
        and best_test["net_ret"] > 0
        and best_test["trades"] >= min_test
    )
    beats_current = best_test["net_ret"] > base_test["net_ret"]
    if validated and beats_current:
        print("[✓] A melhor do treino também lucrou na validação e superou a atual.")
    elif validated:
        print("[~] A melhor do treino lucrou na validação, mas não superou a atual.")
    else:
        print(f"[✗] Não aprovada: precisa de lucro líquido no treino E na validação, "
              f"com {min_test}+ trades na validação. Não use.")
    hold_test = _buy_hold(test)
    if best_test["net_ret"] > hold_test:
        print(f"    Na validação a escolhida ({best_test['net_ret']:+.2%}) ganhou de comprar e segurar ({hold_test:+.2%}).")
    else:
        print(f"    Na validação a escolhida ({best_test['net_ret']:+.2%}) NÃO ganhou de comprar e segurar ({hold_test:+.2%}).")
    print("    (Circuit breaker de 7% desligado no otimizador; no robô ao vivo ele continua ativo.)")
    print("    Escolhida pelo treino; a validação é o número honesto. Escolher olhando a")
    print("    validação seria ajustar ao passado de novo.")

    if args.save:
        if validated and beats_current:
            save_params(best)
            print(f"[*] Gravado em {PARAMS_PATH.name}. No servidor: sudo systemctl restart trading-bot")
            print("    (o robô arquiva a sessão antiga e começa uma nova com esses parâmetros)")
        else:
            print(f"[*] --save ignorado: {PARAMS_PATH.name} não foi alterado.")


def main() -> None:
    cfg = AppConfig()
    parser = argparse.ArgumentParser(description="Backtest da Strategy V3 em dados recentes.")
    parser.add_argument("--days", type=int, help="dias até --end (padrão: 90; 1460 na trend_v4)")
    parser.add_argument("--start", type=_parse_date, help="início AAAA-MM-DD (UTC); ignora --days")
    parser.add_argument("--end", type=_parse_date, help="fim AAAA-MM-DD (UTC, exclusivo); padrão: hoje 00:00")
    parser.add_argument("--timeframe", help="padrão: o de strategy.json (o do robô ao vivo)")
    parser.add_argument("--strategy", choices=STRATEGIES,
                        help="padrão: a de strategy.json; outra estratégia começa dos valores padrão dela")
    parser.add_argument("--symbol", default=str(cfg.default_symbol))
    parser.add_argument("--exchange", default="binance", help="id ccxt (padrão: binance)")
    parser.add_argument("--balance", type=float, default=10_000.0, help="capital inicial (padrão: 10000)")
    parser.add_argument("--no-cache", action="store_true", help="baixa de novo mesmo com cache")
    parser.add_argument("--optimize", action="store_true",
                        help="testa combinações no treino (2/3) e valida no resto (1/3)")
    parser.add_argument("--save", action="store_true",
                        help="com --optimize: grava a melhor em strategy.json se ela se validar")
    args = parser.parse_args()

    params = load_params()
    if args.strategy and args.strategy != params.strategy:
        params = StrategyParams.defaults_for(args.strategy)
    if args.days is None:
        args.days = _DEFAULT_DAYS[params.strategy]
    if args.timeframe:
        params = replace(params, timeframe=args.timeframe)
    args.timeframe = params.timeframe

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
    print(f"BACKTEST — {params.strategy} (mesmo pipeline do robô ao vivo)")
    print("=" * 60)
    print(f"[*] Parâmetros: {_describe(params)} | {params.timeframe}")

    bars = _load_bars(args, data_start, end, tf)
    if len(bars) < _WARMUP_BARS + 10:
        print(f"[!] Dados insuficientes: {len(bars)} candles (mínimo {_WARMUP_BARS + 10}).")
        sys.exit(1)
    gaps = _gap_summary(bars, tf)
    if gaps:
        print(f"[i] {gaps}")

    if args.optimize:
        _optimize(bars, params, args)
        return

    print("[*] Simulando...")
    engine = BacktestEngine(
        symbol=args.symbol,
        timeframe=args.timeframe,
        initial_balance=args.balance,
        bars=bars,
        params=params,
    )
    result = engine.run()
    result.data_source = f"ccxt/{args.exchange}"
    _print_report(result, bars, args.balance)


if __name__ == "__main__":
    main()
