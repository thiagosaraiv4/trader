"""
run_scanner_report.py
---------------------
Tabela do pair_scanner.py (scanner_stats.json): para cada par, o spread e
quanto uma ordem passiva teria ganhado ou perdido depois de executada.

Uso:
    python run_scanner_report.py
    python run_scanner_report.py --fee BTC/BRL=0.1     # taxa maker (% por lado) de um par

Taxa maker suposta: 0% nos pares FDUSD (promoção da Binance, confira) e 0,1%
nos demais (taxa normal). Leitura das colunas no rodapé da tabela.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone

from pair_scanner import HORIZONS, STATS_PATH, PairStats

_MIN_HOURS = 6
_MIN_EVENTS = 100


def _maker_fee_bps(symbol: str, overrides: dict[str, float]) -> float:
    if symbol in overrides:
        return overrides[symbol] * 100
    return 0.0 if symbol.endswith("/FDUSD") else 10.0


def _mean_se(m: list) -> tuple[float, float]:
    n, s, ss = m
    if n < 2:
        return math.nan, math.inf
    mean = s / n
    var = max(ss / n - mean * mean, 0.0) * n / (n - 1)
    return mean, math.sqrt(var / n)


def _median_ticks(st: PairStats) -> float:
    total = sum(st.spread_ticks.values())
    if not total:
        return math.nan
    acc = 0
    for k in sorted(st.spread_ticks):
        acc += st.spread_ticks[k]
        if acc * 2 >= total:
            return k
    return math.nan


def scanner_rows(data: dict, overrides: dict[str, float] | None = None) -> list[dict]:
    """One dict per pair from scanner_stats.json data (also used by the web panel)."""
    overrides = overrides or {}
    rows = []
    for sym, d in sorted(data.get("pairs", {}).items()):
        st = PairStats.from_dict(d)
        hours = st.seconds / 3600
        t = {h: _mean_se(st.marks["touch"][h])[0] for h in HORIZONS}
        thr, thr_se = _mean_se(st.marks["through"][5])
        fee = _maker_fee_bps(sym, overrides)
        net = thr - fee
        n_thr = st.marks["through"][5][0]
        if hours < _MIN_HOURS or n_thr < _MIN_EVENTS:
            verdict = f"pouco dado (<{_MIN_HOURS}h ou <{_MIN_EVENTS} exec.)"
        elif net > 3 * thr_se and net > 0:
            verdict = "CANDIDATO: testar no maker"
        elif t[5] - fee > 0:
            verdict = "só ganha na frente da fila (inviável a 260 ms)"
        else:
            verdict = "sem vantagem"
        rows.append({
            "symbol": sym, "hours": hours, "spread_ticks": _median_ticks(st),
            "spread_bp": st.spread_bps_sum / st.seconds if st.seconds else math.nan,
            "exec_per_hour": st.events.get("touch", 0) / hours if hours else 0.0,
            "touch": t, "through5": thr, "n": n_thr, "fee_bp": fee, "net_bp": net,
            "verdict": verdict,
        })
    return rows


def main() -> None:
    p = argparse.ArgumentParser(description="Relatório do scanner de pares.")
    p.add_argument("--fee", action="append", default=[], metavar="PAR=PCT",
                   help="taxa maker em %% por lado para um par (ex.: BTC/BRL=0.1)")
    args = p.parse_args()
    overrides = {}
    for item in args.fee:
        try:
            sym, pct = item.split("=")
            overrides[sym.strip().upper()] = float(pct.replace(",", "."))
        except ValueError:
            p.error(f"--fee inválido: {item!r} (use PAR=PCT)")

    print("=" * 112)
    print("SCANNER DE PARES — uma ordem passiva no topo do livro ganharia?")
    print("=" * 112)
    if not STATS_PATH.exists():
        print(f"[!] {STATS_PATH.name} ainda não existe. Deixe o pair_scanner rodar (salva a cada 5 min).")
        return
    data = json.loads(STATS_PATH.read_text(encoding="utf-8"))
    updated = datetime.fromtimestamp(data.get("updated", 0), tz=timezone.utc)
    print(f"  Atualizado: {updated:%Y-%m-%d %H:%M} UTC. Valores em bp (0,01%); markout positivo = ganhou.\n")
    hdr = (f"  {'par':<11} {'horas':>5} {'spread':>6} {'spread':>7} {'exec/h':>7} | "
           f"{'topo +1s':>8} {'+5s':>6} {'+30s':>6} | {'atrav. +5s':>10} {'n':>5} {'taxa':>5} {'líquido':>8} | veredito")
    print(hdr)
    print(f"  {'':<11} {'':>5} {'ticks':>6} {'bp':>7} {'':>7} | {'(otimista)':>22} | {'(pessimista)':>10} "
          f"{'':>5} {'maker':>5} {'':>8} |")
    rows = scanner_rows(data, overrides)
    fmt = lambda v, w: f"{'—':>{w}}" if math.isnan(v) else f"{v:>+{w}.2f}"
    for r in rows:
        t = r["touch"]
        print(f"  {r['symbol']:<11} {r['hours']:>5.1f} {r['spread_ticks']:>6.0f} {r['spread_bp']:>7.2f} "
              f"{r['exec_per_hour']:>7.0f} | {fmt(t[1], 8)} {fmt(t[5], 6)} {fmt(t[30], 6)} | "
              f"{fmt(r['through5'], 10)} {r['n']:>5} {r['fee_bp']:>5.1f} {fmt(r['net_bp'], 8)} | {r['verdict']}")
    print()
    print("  spread: mediano em ticks e médio em bp (ponderados pelo tempo). exec/h: negócios por hora no")
    print("  preço do topo. n: execuções pessimistas medidas (o veredito exige 100).")
    print("  topo (otimista): resultado de uma ordem passiva supondo que estávamos na frente da fila.")
    print("  atravessou (pessimista): só quando o negócio passou do nível — o que uma ordem no fim da")
    print("  fila consegue a ~260 ms. 'líquido' = pessimista +5 s menos a taxa maker. Só vale testar um")
    print("  par com líquido positivo acima do ruído (3 erros-padrão) depois de pelo menos 1 dia.")
    print("=" * 112)
    ranked = [(r["net_bp"], r["symbol"]) for r in rows if not math.isnan(r["net_bp"])]
    if ranked:
        best = max(ranked)
        print(f"  Melhor líquido pessimista até agora: {best[1]} ({best[0]:+.2f} bp por execução).")
        print("=" * 112)


if __name__ == "__main__":
    main()
