"""
run_stop_study.py
-----------------
Mais dois testes da trend_v4 nas moedas do robô (mesmos dados e custos do
run_portfolio_study.py):

3. Teto de 95% do saldo — quantos trades tiveram o tamanho limitado por ele
   (em vez de arriscar os 2% até o stop) e quanto risco correram de fato.

4. Distância do stop — quanto os trades vencedores chegaram a cair antes de
   dar lucro (excursão adversa máxima, em ATR do momento da entrada) e o
   backtest com outros stops iniciais e móveis, com custos realistas
   (escorregamento 0,05% a mercado e 0,2% nos stops). Todas as variações
   são mostradas, nas duas metades do período, para não escolher a melhor
   por sorte.

Uso:
    python run_stop_study.py
    python run_stop_study.py --csv-dir pasta/ --coins BTC,ETH
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import run_portfolio_study as rp
from core.strategy_params import StrategyParams
from utils.logger import configure_logging

STOP_GRID = (2.0, 2.5, 3.0, 3.5, 4.0)       # initial stop, trailing fixed at 4
TRAIL_GRID = (3.0, 4.0, 5.0, 6.0)          # trailing stop, initial fixed at 3
REALISTIC = rp.COST_SCENARIOS[1]           # slippage 0.05% / stop 0.2%


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    if not xs:
        return float("nan")
    i = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return xs[i]


def summarize(results: list[tuple[float, float, float]]) -> str:
    """(return, profit factor, max drawdown) per coin → one table cell."""
    rets = [r for r, _, _ in results]
    pfs = [p for _, p, _ in results if not math.isnan(p)]
    return (f"{sum(rets) / len(rets):+7.1%} {sum(r > 0 for r in rets):>3}/{len(rets)} "
            f"{sum(pfs) / len(pfs):5.2f} {max(d for _, _, d in results):6.1%}")


def run_set(coins, data, params, start, end):
    """Backtest each coin; per coin (return, PF, max DD) on the full period and on each half."""
    mid = start + (end - start) / 2
    full, h1, h2 = [], [], []
    for c in coins:
        for bucket, lo, hi in ((full, start, end), (h1, start, mid), (h2, mid, end)):
            bars = [b for b in data[c] if b.timestamp < hi]
            # warm-up bars before lo stay in; the engine trades only after them
            first = next(i for i, b in enumerate(bars) if b.timestamp >= lo)
            bars = bars[max(0, first - rp._WARMUP_BARS):]
            res, tr = rp.backtest(c, bars, params)
            ret = res.equity_curve[-1] / rp.INITIAL - 1 if res.equity_curve else 0.0
            wins = sum(t.net for t in tr if t.net > 0)
            losses = -sum(t.net for t in tr if t.net < 0)
            bucket.append((ret, wins / losses if losses else float("nan"), res.max_drawdown))
    return full, h1, h2


def main() -> None:
    ap = argparse.ArgumentParser(description="Teto de 95% e distância do stop na trend_v4.")
    ap.add_argument("--coins", default=rp.DEFAULT_COINS)
    ap.add_argument("--start", default="2020-12-01")
    ap.add_argument("--end", default="2026-10-14")
    ap.add_argument("--csv-dir", type=Path)
    args = ap.parse_args()
    configure_logging(level=logging.ERROR)
    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    base = StrategyParams.defaults_for("trend_v4")
    data = {c: rp.load_bars(c, start, end, args.csv_dir) for c in coins}

    print("=" * 96)
    print(f"TREND_V4 — {len(coins)} moedas, {start:%Y-%m-%d} → {end:%Y-%m-%d}, 4h")
    print("=" * 96)

    # ---- experiment 3: the 95% cap -----------------------------------------
    rp.set_costs(*rp.COST_SCENARIOS[0][1:])
    print("\nEXPERIMENTO 3 — QUANTAS VEZES O TETO DE 95% DO SALDO LIMITOU A POSIÇÃO\n")
    print(f"  {'moeda':6} {'trades':>6} {'limitados':>9} {'risco real médio':>16} {'stop médio':>10} "
          f"{'resultado médio: limitados':>27} {'livres':>8}")
    all_trades = {}
    tot_n = tot_cap = 0
    for c in coins:
        _, trades = rp.backtest(c, data[c], base)
        all_trades[c] = trades
        prev = rp.INITIAL
        capped, risks, stops, r_cap, r_free = 0, [], [], [], []
        for t in trades:
            want = base.risk_pct * prev
            risk_frac = t.risk_usd / prev
            stops.append(t.risk_usd / (t.qty * t.entry_price))
            risks.append(risk_frac)
            is_capped = t.risk_usd < 0.98 * want
            capped += is_capped
            (r_cap if is_capped else r_free).append(t.net / prev)
            prev += t.net
        tot_n += len(trades)
        tot_cap += capped
        avg = lambda xs: sum(xs) / len(xs) if xs else float("nan")  # noqa: E731
        print(f"  {c:6} {len(trades):6d} {capped:5d} ({capped / len(trades):3.0%}) {avg(risks):14.2%} "
              f"{avg(stops):10.1%} {avg(r_cap):+27.2%} {avg(r_free):+8.2%}")
    print(f"\n  Total: {tot_cap} de {tot_n} trades ({tot_cap / tot_n:.0%}) arriscaram menos que 2% por causa do teto.")
    print("  (stop médio = distância do stop inicial em % do preço; com stop menor que 2,1% o teto de 95% trava)")

    # ---- experiment 4a: adverse excursion ---------------------------------
    print("\nEXPERIMENTO 4a — QUANTO OS TRADES CAÍRAM ANTES DE SAIR (em ATR da entrada; stop inicial = 3 ATR)\n")
    print(f"  {'moeda':6} {'vencedores':>10} {'MAE p50':>8} {'p75':>6} {'p90':>6} {'máx':>6} "
          f"{'venc. que passaram de 2 ATR':>28} {'2,5 ATR':>8}")
    pooled_w = []
    for c in coins:
        lows = [(b.timestamp.timestamp(), b.low) for b in data[c]]
        maes = []
        for t in all_trades[c]:
            if t.net <= 0:
                continue
            atr = t.risk_usd / t.qty / base.stop_atr
            low = min((lo for ts, lo in lows if t.entry <= ts <= t.exit), default=t.entry_price)
            maes.append(max(0.0, (t.entry_price - low) / atr))
        pooled_w += maes
        over = lambda k: sum(m > k for m in maes) / len(maes) if maes else float("nan")  # noqa: E731
        print(f"  {c:6} {len(maes):10d} {pct(maes, .5):8.2f} {pct(maes, .75):6.2f} {pct(maes, .9):6.2f} "
              f"{max(maes, default=float('nan')):6.2f} {over(2.0):28.0%} {over(2.5):8.0%}")
    over = lambda k: sum(m > k for m in pooled_w) / len(pooled_w)  # noqa: E731
    print(f"  {'todas':6} {len(pooled_w):10d} {pct(pooled_w, .5):8.2f} {pct(pooled_w, .75):6.2f} "
          f"{pct(pooled_w, .9):6.2f} {max(pooled_w):6.2f} {over(2.0):28.0%} {over(2.5):8.0%}")
    print("  (MAE = maior queda abaixo da entrada antes da saída; inclui o candle de saída)")

    # ---- experiment 4b: stop grids with realistic costs ---------------------
    rp.set_costs(*REALISTIC[1:])
    print(f"\nEXPERIMENTO 4b — OUTROS STOPS, custos realistas ({REALISTIC[0]})\n")
    hdr = f"{'ret. médio':>10} {'moedas+':>7} {'PF':>5} {'pior DD':>7}"
    print(f"  {'variação':30} {'6 anos':^33}  {'1ª metade':^33}  {'2ª metade':^33}")
    print(f"  {'':30} {hdr}  {hdr}  {hdr}")
    variants = [(f"stop inicial {s:g} ATR, móvel 4" + (" (atual)" if s == 3.0 else ""),
                 replace(base, stop_atr=s)) for s in STOP_GRID]
    variants += [(f"stop inicial 3, móvel {tr:g} ATR" + (" (atual)" if tr == 4.0 else ""),
                  replace(base, trail_atr=tr)) for tr in TRAIL_GRID if tr != 4.0]
    for name, params in variants:
        full, h1, h2 = run_set(coins, data, params, start, end)
        print(f"  {name:30} {summarize(full)}  {summarize(h1)}  {summarize(h2)}")
    rp.set_costs(*rp.COST_SCENARIOS[0][1:])
    print("\nComo ler: só vale trocar se a variação ganhar do atual nas DUAS metades e no total;")
    print("ganhar só no total, com 9 variações testadas, pode ser sorte.")


if __name__ == "__main__":
    sys.exit(main())
