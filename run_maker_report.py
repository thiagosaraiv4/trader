"""
run_maker_report.py
-------------------
Resumo do robô maker de papel (maker_bot.py) a partir de maker_trades.csv,
uma linha por versão — todas rodaram sobre os mesmos dados ao vivo:

    base          compra no melhor preço de compra; stop 0,2% / 5 min a mercado
    filtro        só compra com fila de compra >= fila de venda e preço sem
                  cair nos últimos 10 s
    saida_maker   sai com ordem limitada (sem taxa), baixando a venda até o
                  melhor preço de venda após 30 s; stop a mercado só em 0,5%
    filtro+maker  as duas mudanças juntas

Uso:
    python run_maker_report.py
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from core.maker_sim import summarize
from maker_bot import TRADES_PATH, VARIANTS, load_trips

_MIN_TRADES = 30


def _verdict(pnls: list[float]) -> str:
    n = len(pnls)
    if n < _MIN_TRADES:
        return verdict_from(n, 0.0, 0.0)
    avg = sum(pnls) / n
    sd = math.sqrt(sum((p - avg) ** 2 for p in pnls) / (n - 1))
    return verdict_from(n, avg, sd)


def verdict_from(n: int, avg: float, sd: float) -> str:
    """Verdict from trade count, mean and sample std of the PnL per trade
    (the web panel computes these while streaming the CSV)."""
    if n < _MIN_TRADES:
        return f"amostra pequena (<{_MIN_TRADES})"
    noise = 3 * sd / math.sqrt(n)
    if avg <= 0:
        return "PERDE"
    if avg <= noise:
        return "lucro dentro do ruído"
    return "LUCRA acima do ruído"


def main() -> None:
    trips = load_trips()
    print("=" * 96)
    print("ROBÔ MAKER DE PAPEL — 4 VERSÕES NOS MESMOS DADOS")
    print("=" * 96)
    if not trips:
        print(f"[!] Nenhum trade em {TRADES_PATH.name} ainda. Deixe o maker_bot rodar.")
        return
    start = datetime.fromtimestamp(min(t.entry_time for t in trips), tz=timezone.utc)
    end = datetime.fromtimestamp(max(t.exit_time for t in trips), tz=timezone.utc)
    days = max((end - start).total_seconds() / 86400, 1 / 24)
    print(f"  Período: {start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M} UTC ({days:.1f} dias)\n")
    print(f"  {'versão':<13} {'trades':>6} {'resultado':>10} {'por trade':>10} {'acerto':>7} "
          f"{'taxas':>7} {'a mercado':>9} {'+1s':>8} {'+30s':>8}  conclusão")
    for name in VARIANTS:
        vt = [t for t in trips if t.variant == name]
        if not vt:
            print(f"  {name:<13} {0:>6}  (sem trades ainda)")
            continue
        s = summarize(vt)
        forced = sum(1 for t in vt if t.exit_kind != "maker")
        m1, m30 = s["markouts"][1], s["markouts"][30]
        fmt = lambda v: "—" if math.isnan(v) else f"{v:+.4%}"
        notional = sum(t.entry_price * t.qty for t in vt) / len(vt)
        print(f"  {name:<13} {s['trades']:>6} {s['pnl']:>+10.2f} {s['avg_pnl'] / notional:>+10.4%} "
              f"{s['win_rate']:>7.1%} {s['fees']:>7.2f} {forced:>9} {fmt(m1):>8} {fmt(m30):>8}  "
              f"{_verdict([t.pnl for t in vt])}")
    print()
    print("  resultado/taxas em FDUSD (ordens de ~1000); 'por trade' em % do valor da ordem;")
    print("  'a mercado' = saídas que pagaram taxa taker (stop, tempo, reinício);")
    print("  +1s/+30s = preço médio depois de cada compra (negativo = seleção adversa).")
    print("=" * 96)
    print("  Compare as versões entre si: todas viram exatamente o mesmo mercado. Uma versão")
    print("  só vale um teste real se LUCRAR acima do ruído por 1-2 semanas seguidas.")
    print("  Simulação: fila e latência são estimadas; confirme a taxa maker 0% do par antes.")
    print("=" * 96)


if __name__ == "__main__":
    main()
