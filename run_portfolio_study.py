"""
run_portfolio_study.py
----------------------
Dois testes de robustez da trend_v4 nas moedas do robô:

1. Custos realistas — o mesmo backtest de cada moeda com taxa maior e
   escorregamento (slippage): compras e saídas a mercado pagam s_mkt, e os
   stops (executados no meio do candle) saem s_stop abaixo do nível.

2. Uma conta só — hoje cada moeda tem sua conta de US$ 10.000. Aqui as 9
   dividem uma conta de US$ 90.000: cada trade arrisca r% do patrimônio
   total até o stop inicial, com um teto para a soma do risco aberto (as
   moedas sobem e caem juntas) e sem alavancagem (soma das posições ≤
   patrimônio). Os trades são os do backtest de cada moeda, reescalados.

Uso:
    python run_portfolio_study.py                       # baixa/usa o cache do run_backtest
    python run_portfolio_study.py --csv-dir pasta/      # CSVs locais SIMBOLO.csv (ts_ms,open,...)
    python run_portfolio_study.py --coins BTC,ETH,SOL --start 2020-12-01 --end 2026-10-14
"""
from __future__ import annotations

import argparse
import csv
import logging
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import paper_trading
from core.backtest_engine import BacktestEngine
from core.paper_trading import PaperTradingCore
from core.strategy_params import StrategyParams
from core.types import BarData
from utils.logger import configure_logging

DEFAULT_COINS = "BTC,ETH,SOL,BNB,XLM,TRX,NEAR,DOGE,AVAX"
INITIAL = 10_000.0
_WARMUP_BARS = BacktestEngine._WARMUP_BARS   # indicator warm-up before --start
_BAR_SEC = 4 * 3600

# Custos: (nome, taxa por lado, slippage a mercado, slippage no stop)
COST_SCENARIOS = [
    ("base (taxa 0,1%, sem slippage)", 0.001, 0.0, 0.0),
    ("slippage 0,05% / stop 0,2%", 0.001, 0.0005, 0.002),
    ("slippage 0,1% / stop 0,5%", 0.001, 0.001, 0.005),
    ("taxa 0,2% + slip 0,05/0,2%", 0.002, 0.0005, 0.002),
    ("estresse: taxa 0,2% + slip 0,1/0,5%", 0.002, 0.001, 0.005),
]
RISKS = (0.005, 0.01, 0.02)
CAPS = (None, 0.08, 0.06, 0.04)


# ---------------------------------------------------------------------------
# costs: patch the paper engine (the backtest runs the live engine)
# ---------------------------------------------------------------------------

_orig_fill = PaperTradingCore._fill_order
_orig_mark = PaperTradingCore.update_mark_price
_slip = {"mkt": 0.0, "stop": 0.0}


def _mark(self, bar):  # stop exits happen inside update_mark_price
    self._in_mark = True
    try:
        return _orig_mark(self, bar)
    finally:
        self._in_mark = False


def _fill(self, request, fill_price):
    if request.side == "BUY":
        fill_price *= 1 + _slip["mkt"]
    else:
        fill_price *= 1 - (_slip["stop"] if getattr(self, "_in_mark", False) else _slip["mkt"])
    return _orig_fill(self, request, fill_price)


PaperTradingCore.update_mark_price = _mark
PaperTradingCore._fill_order = _fill


def set_costs(fee: float, s_mkt: float, s_stop: float) -> None:
    paper_trading._COMMISSION_RATE = fee
    _slip["mkt"], _slip["stop"] = s_mkt, s_stop


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------

def load_bars(coin: str, start: datetime, end: datetime, csv_dir: Path | None) -> list[BarData]:
    data_start = start - timedelta(seconds=_WARMUP_BARS * _BAR_SEC)
    if csv_dir is None:
        import run_backtest
        args = argparse.Namespace(exchange="binance", symbol=f"{coin}/USDT", timeframe="4h", no_cache=False)
        return run_backtest._load_bars(args, data_start, end, timedelta(seconds=_BAR_SEC))
    bars = []
    with open(csv_dir / f"{coin}USDT.csv", newline="") as fh:
        for r in csv.DictReader(fh):
            t = datetime.fromtimestamp(int(r["ts_ms"]) / 1000, tz=timezone.utc)
            if data_start <= t < end:
                bars.append(BarData(timestamp=t, open=float(r["open"]), high=float(r["high"]),
                                    low=float(r["low"]), close=float(r["close"]), volume=float(r["volume"])))
    return bars


# ---------------------------------------------------------------------------
# per-coin backtests
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    coin: str
    entry: float           # epoch
    exit: float
    entry_price: float
    qty: float
    risk_usd: float        # qty × (entry - initial stop)
    net: float             # result after fees and slippage
    exit_reason: str


def backtest(coin: str, bars: list[BarData], params: StrategyParams):
    engine = BacktestEngine(symbol=f"{coin}/USDT", timeframe="4h", initial_balance=INITIAL,
                            bars=bars, params=params)
    res = engine.run()
    trades, prev = [], INITIAL
    for t in res.trade_log:
        net = t.balance_after - prev          # cash change between closes = net result
        prev = t.balance_after
        risk = t.quantity * (t.entry_price - t.initial_stop)
        if risk > 0:
            trades.append(Trade(coin, t.entry_time.timestamp(), t.exit_time.timestamp(), t.entry_price,
                                t.quantity, risk, net, t.exit_reason))
    return res, trades


# ---------------------------------------------------------------------------
# shared-account simulation
# ---------------------------------------------------------------------------

def simulate(trades: list[Trade], closes: dict[str, dict[float, float]], times: list[float],
             risk: float, cap: float | None, capital: float) -> dict:
    """
    One account for every coin. Each trade risks `risk` × equity (closed
    equity, the usual practice) to its initial stop, scaled down to fit the
    open-risk cap and the no-leverage limit; skipped if less than a quarter
    of the wanted size fits.
    """
    by_entry: dict[float, list[Trade]] = {}
    for tr in trades:
        by_entry.setdefault(tr.entry, []).append(tr)
    closed = capital
    open_pos: list[tuple[Trade, float]] = []          # (trade, scale k)
    curve, scaled, skipped, max_open, max_open_risk = [], 0, 0, 0, 0.0
    for t in times:
        # exits first (a stop fills before a new entry on the same bar's open)
        still = []
        for tr, k in open_pos:
            if tr.exit <= t:
                closed += k * tr.net
            else:
                still.append((tr, k))
        open_pos = still
        for tr in by_entry.get(t, []):
            want = risk * closed / tr.risk_usd
            k = want
            if cap is not None:
                room = cap * closed - sum(o.risk_usd * kk for o, kk in open_pos)
                k = min(k, max(room, 0.0) / tr.risk_usd)
            notional_room = closed - sum(o.qty * o.entry_price * kk for o, kk in open_pos)
            k = min(k, max(notional_room, 0.0) * 0.98 / (tr.qty * tr.entry_price),
                    0.95 * closed / (tr.qty * tr.entry_price))
            if k < 0.25 * want:
                skipped += 1
                continue
            scaled += k < want * 0.999
            open_pos.append((tr, k))
        max_open = max(max_open, len(open_pos))
        if closed > 0:
            max_open_risk = max(max_open_risk, sum(o.risk_usd * kk for o, kk in open_pos) / closed)
        mtm = closed + sum(kk * o.qty * (closes[o.coin].get(t, o.entry_price) - o.entry_price)
                           for o, kk in open_pos)
        curve.append((t, mtm))
    return {"curve": curve, "scaled": scaled, "skipped": skipped, "taken": len(trades) - skipped,
            "max_open": max_open, "max_open_risk": max_open_risk}


def stats(curve: list[tuple[float, float]], capital: float) -> dict:
    vals = [v for _, v in curve]
    years = (curve[-1][0] - curve[0][0]) / (365.25 * 86400)
    cagr = (vals[-1] / capital) ** (1 / years) - 1 if vals[-1] > 0 and years > 0 else float("nan")
    peak, dd = capital, 0.0
    for v in vals:
        peak = max(peak, v)
        dd = max(dd, (peak - v) / peak)
    # daily returns (6 bars of 4h) → annualized Sharpe; worst calendar month
    daily = vals[::6]
    rets = [b / a - 1 for a, b in zip(daily, daily[1:]) if a > 0]
    mu = sum(rets) / len(rets) if rets else 0.0
    sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) if len(rets) > 1 else 0.0
    month_end: dict[str, float] = {}
    for t, v in curve:
        month_end[datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m")] = v
    months = list(month_end.values())
    mrets = [b / a - 1 for a, b in zip([capital] + months, months)]
    return {"final": vals[-1], "ret": vals[-1] / capital - 1, "cagr": cagr, "dd": dd,
            "sharpe": mu / sd * math.sqrt(365) if sd else float("nan"),
            "worst_month": min(mrets) if mrets else float("nan"),
            "calmar": cagr / dd if dd > 0 else float("nan")}


def separate_accounts(results: dict, times: list[float], capital_each: float) -> list[tuple[float, float]]:
    """Today's setup: the sum of the coins' own equity curves."""
    curve = []
    per = {}
    for coin, (res, bars_times) in results.items():
        per[coin] = dict(zip(bars_times, res.equity_curve))
    last = {c: capital_each for c in per}
    for t in times:
        for c in per:
            if t in per[c]:
                last[c] = per[c][t]
        curve.append((t, sum(last.values())))
    return curve


# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Custos realistas e conta única para a trend_v4.")
    ap.add_argument("--coins", default=DEFAULT_COINS)
    ap.add_argument("--start", default="2020-12-01")
    ap.add_argument("--end", default="2026-10-14")
    ap.add_argument("--csv-dir", type=Path, help="CSVs locais SIMBOLOUSDT.csv (ts_ms,open,high,low,close,volume)")
    args = ap.parse_args()
    configure_logging(level=logging.ERROR)
    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    params = StrategyParams.defaults_for("trend_v4")
    capital = INITIAL * len(coins)

    data = {c: load_bars(c, start, end, args.csv_dir) for c in coins}
    closes = {c: {b.timestamp.timestamp(): b.close for b in bars} for c, bars in data.items()}
    t0 = start.timestamp()
    times = sorted({t for c in closes for t in closes[c] if t >= t0})

    print("=" * 100)
    print(f"TREND_V4 — {len(coins)} moedas, {start:%Y-%m-%d} → {end:%Y-%m-%d}, 4h")
    print("=" * 100)

    # ---- experiment 2: costs -------------------------------------------------
    print("\nEXPERIMENTO 2 — CUSTOS REALISTAS (9 contas separadas de US$ 10.000, como hoje)\n")
    print(f"  {'cenário':38} {'carteira':>9} {'moedas +':>8} {'PF médio':>8} {'pior DD':>8}   por moeda")
    trades_by_scenario: dict[str, list[Trade]] = {}
    base_results = None
    for name, fee, s_mkt, s_stop in COST_SCENARIOS:
        set_costs(fee, s_mkt, s_stop)
        results, trades, per_coin = {}, [], []
        for c in coins:
            res, tr = backtest(c, data[c], params)
            # the equity curve covers the bars after the engine's warm-up
            stamps = [b.timestamp.timestamp() for b in data[c]]
            results[c] = (res, stamps[-len(res.equity_curve):] if res.equity_curve else [])
            trades += tr
            ret = (res.equity_curve[-1] / INITIAL - 1) if res.equity_curve else 0.0
            wins = sum(t.net for t in tr if t.net > 0)
            losses = -sum(t.net for t in tr if t.net < 0)
            per_coin.append((c, ret, wins / losses if losses else float("nan"), res.max_drawdown))
        trades_by_scenario[name] = trades
        if base_results is None:
            base_results = results
        tot = sum(r for _, r, _, _ in per_coin) / len(per_coin)
        pos = sum(r > 0 for _, r, _, _ in per_coin)
        pf = sum(p for _, _, p, _ in per_coin if not math.isnan(p)) / len(per_coin)
        dd = max(d for _, _, _, d in per_coin)
        detail = " ".join(f"{c} {r:+.0%}" for c, r, _, _ in per_coin)
        print(f"  {name:38} {tot:+9.1%} {pos:>5}/{len(coins)} {pf:8.2f} {dd:8.1%}   {detail}")
    set_costs(*COST_SCENARIOS[0][1:])

    # ---- experiment 1: shared account --------------------------------------
    sep = stats(separate_accounts(base_results, times, INITIAL), capital)
    for label, scen in (("custos base", COST_SCENARIOS[0][0]), ("custos de estresse", COST_SCENARIOS[-1][0])):
        trades = trades_by_scenario[scen]
        print(f"\nEXPERIMENTO 1 — UMA CONTA DE US$ {capital:,.0f} PARA AS {len(coins)} MOEDAS ({label})\n")
        print(f"  {'configuração':34} {'retorno':>8} {'a.a.':>7} {'queda máx':>9} {'retorno/queda':>13} "
              f"{'Sharpe':>6} {'pior mês':>8} {'risco aberto máx':>16} {'reduz.':>6} {'pulados':>7}")
        if label == "custos base":
            print(f"  {'hoje: 9 contas separadas, 2% cada':34} {sep['ret']:+8.1%} {sep['cagr']:+7.1%} "
                  f"{sep['dd']:9.1%} {sep['calmar']:13.2f} {sep['sharpe']:6.2f} {sep['worst_month']:+8.1%} "
                  f"{'2,0% do total':>16} {'—':>6} {'—':>7}")
        for r in RISKS:
            for cap in CAPS:
                sim = simulate(trades, closes, times, r, cap, capital)
                st = stats(sim["curve"], capital)
                name = f"risco {r:.1%} por trade, teto " + ("—" if cap is None else f"{cap:.0%}")
                print(f"  {name:34} {st['ret']:+8.1%} {st['cagr']:+7.1%} {st['dd']:9.1%} {st['calmar']:13.2f} "
                      f"{st['sharpe']:6.2f} {st['worst_month']:+8.1%} {sim['max_open_risk']:16.1%} "
                      f"{sim['scaled']:6d} {sim['skipped']:7d}")

    # ---- stability: same table on each half ---------------------------------
    mid = t0 + (times[-1] - t0) / 2
    print("\nESTABILIDADE — risco 1% por trade, custos base, cada metade do período")
    for lo, hi, tag in ((t0, mid, "1ª metade"), (mid, times[-1] + 1, "2ª metade")):
        sub = [t for t in trades_by_scenario[COST_SCENARIOS[0][0]] if lo <= t.entry < hi]
        sub_times = [t for t in times if lo <= t < hi]
        row = []
        for cap in CAPS:
            st = stats(simulate(sub, closes, sub_times, 0.01, cap, capital)["curve"], capital)
            row.append(f"teto {'—' if cap is None else f'{cap:.0%}'}: {st['ret']:+.0%} / queda {st['dd']:.0%}")
        print(f"  {tag}: " + " | ".join(row))
    print("\nNotas: risco aberto medido nos stops iniciais (conservador: o stop móvel o reduz com o tempo).")
    print("A conta única reescala os trades do backtest de cada moeda; não muda entradas nem saídas.")


if __name__ == "__main__":
    sys.exit(main())
