"""
run_maker_report.py
-------------------
Resumo do robô maker de papel (maker_bot.py) a partir de maker_trades.csv:
resultado líquido, de onde vieram as saídas e a "seleção adversa" — quanto
o preço anda logo depois que uma ordem de compra é executada.

Uso:
    python run_maker_report.py
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timezone

from core.maker_sim import MARKOUTS, summarize
from maker_bot import TRADES_PATH, load_trips

_MIN_TRADES = 30
_KINDS = {
    "maker": "vendeu com ordem limitada (maker)",
    "stop": "stop (venda a mercado, paga taxa taker)",
    "timeout": "tempo máximo (venda a mercado)",
    "shutdown": "robô parou/reconectou (venda a mercado)",
}


def main() -> None:
    trips = load_trips()
    print("=" * 66)
    print("ROBÔ MAKER DE PAPEL — RELATÓRIO")
    print("=" * 66)
    if not trips:
        print(f"[!] Nenhum trade em {TRADES_PATH.name} ainda. Deixe o maker_bot rodar.")
        return
    s = summarize(trips)
    start = datetime.fromtimestamp(trips[0].entry_time, tz=timezone.utc)
    end = datetime.fromtimestamp(trips[-1].exit_time, tz=timezone.utc)
    days = max((end - start).total_seconds() / 86400, 1 / 24)
    notional = sum(t.entry_price * t.qty for t in trips) / len(trips)
    pnls = [t.pnl for t in trips]
    sd = math.sqrt(sum((p - s["avg_pnl"]) ** 2 for p in pnls) / (len(pnls) - 1)) if len(pnls) > 1 else math.inf
    noise = 3 * sd / math.sqrt(len(pnls))

    print(f"  Período:        {start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M} UTC ({days:.1f} dias)")
    print(f"  Idas e voltas:  {s['trades']} ({s['trades'] / days:.0f}/dia), ~{notional:,.0f} por ordem")
    print(f"  Resultado:      {s['pnl']:+,.2f} líquido ({s['avg_pnl']:+.4f}/trade = "
          f"{s['avg_pnl'] / notional:+.4%} do valor da ordem)")
    print(f"  Taxas pagas:    {s['fees']:,.2f}")
    print(f"  Acerto:         {s['win_rate']:.1%} dos trades com lucro líquido")
    print(f"  Tempo médio:    {s['avg_hold_sec']:.0f} s posicionado")
    print("  Saídas:")
    for kind, n in Counter(t.exit_kind for t in trips).most_common():
        print(f"    {n:>6}  {_KINDS.get(kind, kind)}")
    print("  Preço médio depois de cada COMPRA executada (seleção adversa):")
    for sec in MARKOUTS:
        v = s["markouts"][sec]
        print(f"    +{sec:>2} s: {'—' if math.isnan(v) else f'{v:+.4%}'}")
    print("    (negativo = as compras acontecem logo antes de o preço cair)")

    print("=" * 66)
    if s["trades"] < _MIN_TRADES:
        verdict = f"amostra pequena (menos de {_MIN_TRADES} trades) — deixe rodar mais"
    elif s["avg_pnl"] <= 0:
        verdict = "NÃO lucra: perde em média por trade"
    elif s["avg_pnl"] <= noise:
        verdict = "lucro médio dentro do ruído estatístico — pode ser sorte"
    else:
        verdict = "lucro acima do ruído — candidato a teste real com valor mínimo"
    print(f"  CONCLUSÃO: {verdict}.")
    print("  Simulação: fila e latência são estimadas; ordens reais podem executar menos")
    print("  (e pior). Confirme que a taxa maker do par continua 0% antes de qualquer teste.")
    print("=" * 66)


if __name__ == "__main__":
    main()
