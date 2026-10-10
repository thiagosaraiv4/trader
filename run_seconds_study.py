"""
run_seconds_study.py
--------------------
Dá para ganhar dinheiro operando BTC em SEGUNDOS? Antes de escrever um robô,
mede com dados reais de 1 segundo da Binance (data.binance.vision) o "muro
das taxas":

  1. Quanto o preço anda em 1 s, 5 s, 30 s, 1 min, 15 min...
  2. Que fração desses movimentos cobre a taxa de ida+volta
  3. Que taxa de acerto de direção seria preciso para empatar
  4. Quanto rendem os sinais mais simples (momento / reversão), com o lado
     escolhido na 1ª metade do período e medido na 2ª (fora da amostra)

Uso (rode na VM — a Binance bloqueia servidores nos EUA):
    python run_seconds_study.py                      # últimos 7 dias, BTCUSDT
    python run_seconds_study.py --days 3 --end 2026-10-01
    python run_seconds_study.py --fees 0.1,0.075,0   # taxas por lado, em %

Baixa um arquivo por dia (~2-3 MB) e guarda em backtest_cache/vision/.
Não negocia nada; só mede (os dados da Binance Vision são licenciados para
estudo/backtest pessoal, não para gerar ordens ao vivo).
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from core.config import AppConfig
from data.binance_vision import CACHE_DIR, VisionError, load_day

_DEFAULT_FEES = (0.1, 0.075, 0.02, 0.0)        # % per side
_DEFAULT_HORIZONS = (1, 2, 5, 10, 30, 60, 300, 900)
_MAX_DAYS = 14                                  # ~190 MB peak: leaves room for the live bot on a 1 GB VM
_W = 11                                         # width of each fee column
_MIN_TRADES = 30                                # fewer independent trades: no noise estimate
_MAX_IDLE = 0.30                                # above this share of no-trade seconds, stale
                                                # prices fake reversal edges


def _parse_list(value: str, kind: type) -> tuple:
    try:
        items = tuple(kind(v.strip().replace(",", ".")) for v in value.split(";" if ";" in value else ","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"lista inválida {value!r}")
    if not items:
        raise argparse.ArgumentTypeError("lista vazia")
    return items


def _parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"data inválida {value!r} (use AAAA-MM-DD)")


def _vision_symbol(value: str) -> str:
    """'BTC/USDT', 'btc-usdt', 'BTC/USDT:USDT' → 'BTCUSDT' (archive format)."""
    return re.sub(r"[^A-Z0-9]", "", value.split(":")[0].upper())


def _load(symbol: str, days: list[date]) -> list[tuple[date, pd.DataFrame]]:
    """Close and trade count of every second, per available day."""
    loaded = []
    for i, day in enumerate(days, 1):
        print(f"\r[*] Baixando {symbol} 1s {day:%Y-%m-%d} ({i}/{len(days)})", end="", flush=True)
        df = load_day(symbol, day)
        if df is None:
            print(f"\n[i] {day:%Y-%m-%d} não disponível na Binance (ainda não publicado?) — pulado")
            continue
        loaded.append((day, df[["close", "trades"]]))
    print()
    return loaded


def _blocks(loaded: list[tuple[date, pd.DataFrame]]) -> tuple[list[np.ndarray], int, int]:
    """
    Closes on a full 1-second grid, one array per run of consecutive days
    (a missing day must not become a flat stretch). Missing seconds repeat
    the last close. Returns (blocks, seconds, seconds without trades).
    """
    runs: list[list[pd.DataFrame]] = []
    prev: date | None = None
    for day, df in loaded:
        if prev is None or day != prev + timedelta(days=1):
            runs.append([])
        runs[-1].append(df)
        prev = day
    blocks, total, idle = [], 0, 0
    for run in runs:
        df = pd.concat(run)
        df = df[~df.index.duplicated()]
        grid = pd.date_range(df.index[0], df.index[-1], freq="1s")
        df = df.reindex(grid)
        blocks.append(df["close"].ffill().to_numpy())
        total += len(grid)
        # Filler rows (trades=0) and seconds absent from the file
        idle += int((df["trades"].fillna(0) == 0).sum())
    return blocks, total, idle


def _horizon(blocks: list[np.ndarray], h: int, rts: list[float]) -> dict | None:
    """
    Stats of h-second returns, share of them above each round-trip fee in
    `rts`, and the simplest signal over that horizon: follow (momentum) or
    invert (reversal) the move of the last h seconds. The side is picked on
    the first half of the pairs and measured on the second half.
    """
    fut_all, past, fut, fut_late = [], [], [], []
    for c in blocks:
        if len(c) <= 2 * h + 1:
            continue
        r = c[h:] / c[:-h] - 1          # r[t]: return from second t to t+h
        fut_all.append(r)
        past.append(r[:-h - 1])        # move of the last h seconds, ending at t
        fut.append(r[h:-1])            # move of the next h seconds, from t
        fut_late.append(r[h + 1:])     # same, entering 1 s late
    if not fut_all:
        return None
    a = np.abs(np.concatenate(fut_all))
    s = np.sign(np.concatenate(past))
    f, fl = np.concatenate(fut), np.concatenate(fut_late)
    half = len(s) // 2
    # Side from the first half only: choosing it on the data it is scored on
    # turns pure noise into an "edge". Judged on the 1 s late entry, the
    # trade that can actually be made (bid-ask bounce vanishes there)
    first = s[:half] != 0
    side = 1.0 if (not first.any() or np.mean(s[:half][first] * fl[:half][first]) >= 0) else -1.0
    s2, f2, fl2 = side * s[half:], f[half:], fl[half:]
    m = s2 != 0
    moved = m & (fl2 != 0)
    # Noise: 3 standard errors over non-overlapping trades (one every h
    # seconds). 3, not 2: the conclusion picks the best of several horizons,
    # and at 2 a pure random walk would show an "edge" ~1 run in 6
    pnl = (s2 * fl2)[::h]
    pnl = pnl[s2[::h] != 0]
    se = float(np.std(pnl, ddof=1) / math.sqrt(len(pnl))) if len(pnl) >= _MIN_TRADES else math.inf
    return {
        "mean": float(a.mean()), "median": float(np.median(a)),
        "p90": float(np.quantile(a, 0.9)), "cover": [float(np.mean(a > rt)) for rt in rts],
        "signal": "momento" if side > 0 else "reversão",
        "hit": float(np.mean(np.sign(fl2[moved]) == s2[moved])) if moved.any() else 0.5,
        "gross": float(np.mean(s2[m] * f2[m])) if m.any() else 0.0,
        "gross_late": float(np.mean(s2[m] * fl2[m])) if m.any() else 0.0,
        "noise": 3 * se,
    }


def _fmt_h(h: int) -> str:
    return f"{h // 60} min" if h >= 60 and h % 60 == 0 else f"{h} s"


def _break_even(rt: float, mean_move: float) -> float:
    """Direction accuracy needed to break even (inf = impossible)."""
    if rt == 0:
        return 0.5
    if mean_move == 0:
        return math.inf
    return 0.5 + rt / (2 * mean_move)


def main() -> None:
    cfg = AppConfig()
    parser = argparse.ArgumentParser(description="Mede se operar em segundos paga as taxas.")
    parser.add_argument("--symbol", default=str(cfg.default_symbol), help="padrão: BTC/USDT")
    parser.add_argument("--days", type=int, default=7, help=f"dias (padrão: 7; máx. {_MAX_DAYS})")
    parser.add_argument("--end", type=_parse_date, help="último dia AAAA-MM-DD (UTC); padrão: ontem")
    parser.add_argument("--fees", type=lambda v: _parse_list(v, float), default=_DEFAULT_FEES,
                        help="taxas por lado em %% (padrão: 0.1,0.075,0.02,0)")
    parser.add_argument("--horizons", type=lambda v: _parse_list(v, int), default=_DEFAULT_HORIZONS,
                        help="horizontes em segundos (padrão: 1,2,5,10,30,60,300,900)")
    args = parser.parse_args()
    if not 1 <= args.days <= _MAX_DAYS:
        parser.error(f"--days precisa estar entre 1 e {_MAX_DAYS}")
    if any(f < 0 or f >= 1 for f in args.fees):
        parser.error("--fees em % por lado, entre 0 e 1 (ex.: 0.1)")
    if any(h < 1 for h in args.horizons):
        parser.error("--horizons em segundos, a partir de 1")

    symbol = _vision_symbol(args.symbol)
    end = args.end or (datetime.now(timezone.utc).date() - timedelta(days=1))
    days = [end - timedelta(days=i) for i in range(args.days - 1, -1, -1)]

    print("=" * 78)
    print(f"MURO DAS TAXAS — {symbol}, candles de 1 segundo (data.binance.vision)")
    print("=" * 78)
    try:
        loaded = _load(symbol, days)
    except VisionError as e:
        print(f"\n[!] Falha no download: {e}")
        if e.status in (403, 451):
            print("    Acesso bloqueado — a Binance bloqueia servidores nos EUA (Codespaces); rode na VM.")
        else:
            print("    Rode de novo: os dias já baixados ficam no cache.")
        sys.exit(1)
    if not loaded:
        print(f"[!] Nenhum dia disponível no período. Confira o símbolo ({symbol}, formato da")
        print("    Binance, ex.: BTCUSDT); o arquivo de cada dia sai por volta de 01:30-02:30 UTC")
        print("    do dia seguinte.")
        sys.exit(1)
    blocks, total, idle = _blocks(loaded)
    print(f"[*] {len(loaded)} dia(s), {total:,} segundos; {idle:,} sem negócio "
          f"({idle / total:.1%}) repetem o último preço.")
    print(f"[*] Cache: {CACHE_DIR}")
    if idle / total > _MAX_IDLE:
        print(f"[!] Mais de {_MAX_IDLE:.0%} dos segundos sem negócio: preço parado distorce os sinais;"
              " nenhum lucro será dado como confiável.")

    fees = sorted(set(args.fees), reverse=True)
    rts = [2 * f / 100 for f in fees]                       # round trip, fraction
    stats = [(h, _horizon(blocks, h, rts)) for h in sorted(set(args.horizons))]
    stats = [(h, s) for h, s in stats if s is not None]
    if not stats:
        print("[!] Dados curtos demais para esses horizontes.")
        sys.exit(1)

    fee_hdr = "".join(f"{f'{2 * f:g}%':>{_W}}" for f in fees)
    print("\n1) Quanto o preço anda (|variação| entre agora e daqui a N) e, à direita,")
    print("   % das vezes que esse movimento cobre a taxa de ida+volta (compra + venda):")
    print(f"   {'horizonte':>9} {'médio':>9} {'mediana':>9} {'p90':>9} |{fee_hdr}")
    for h, s in stats:
        cover = "".join(f"{c:>{_W}.1%}" for c in s["cover"])
        print(f"   {_fmt_h(h):>9} {s['mean']:>9.4%} {s['median']:>9.4%} {s['p90']:>9.4%} |{cover}")

    print("\n2) Acerto de direção necessário só para EMPATAR, por taxa de ida+volta")
    print("   (supondo ganho/perda de cada trade = |variação| média):")
    print(f"   {'horizonte':>9} |{fee_hdr}")
    for h, s in stats:
        cells = ""
        for rt in rts:
            need = _break_even(rt, s["mean"])
            cells += f"{'impossível':>{_W}}" if need > 1 else f"{need:>{_W}.1%}"
        print(f"   {_fmt_h(h):>9} |{cells}")
    print("   ('impossível' = nem acertando 100% das direções o movimento médio paga a taxa)")

    print("\n3) O sinal mais simples: seguir (momento) ou inverter (reversão) o movimento dos")
    print("   últimos N segundos. Lado escolhido na 1ª metade do período, medido na 2ª,")
    print("   entrando 1 s depois do sinal. À direita, resultado líquido por trade, por taxa:")
    print(f"   {'horizonte':>9} {'sinal':>9} {'acerto':>7} {'sem atraso':>10} {'1s atraso':>10} "
          f"{'ruído ±':>9} |{fee_hdr}")
    for h, s in stats:
        net = "".join(f"{s['gross_late'] - rt:>+{_W}.4%}" for rt in rts)
        noise = f"{'∞':>9}" if math.isinf(s["noise"]) else f"{s['noise']:>9.4%}"
        print(f"   {_fmt_h(h):>9} {s['signal']:>9} {s['hit']:>7.1%} {s['gross']:>+10.4%} "
              f"{s['gross_late']:>+10.4%} {noise} |{net}")
    print("   (ganho médio por trade antes das taxas: 'sem atraso' é impossível na prática;")
    print("    '1s atraso' = entrando 1 segundo depois, mais perto do real com ~0,26 s de ida")
    print("    e volta até a Binance. 'ruído' = margem estatística: um resultado menor que ela")
    print(f"    pode ser só sorte; ∞ = menos de {_MIN_TRADES} trades independentes, não dá para medir.)")

    print("\n" + "=" * 78)
    print("CONCLUSÃO")
    for f, rt in zip(fees, rts):
        # Best net result, preferring horizons whose NET result clears the noise
        h, s = max(stats, key=lambda hs: (hs[1]["gross_late"] - rt > hs[1]["noise"],
                                          hs[1]["gross_late"] - rt))
        net = s["gross_late"] - rt
        if net <= 0:
            verdict = f"nenhum sinal simples lucra (o melhor: {net:+.4%}/trade em {_fmt_h(h)})"
        elif net <= s["noise"]:
            verdict = (f"lucro aparente de {net:+.4%}/trade em {_fmt_h(h)} está dentro do ruído "
                       "(pode ser sorte)")
        elif idle / total > _MAX_IDLE:
            verdict = (f"lucro aparente de {net:+.4%}/trade em {_fmt_h(h)} não é confiável: "
                       f"{idle / total:.0%} dos segundos sem negócio (preço parado engana)")
        else:
            verdict = (f"{net:+.4%}/trade em {_fmt_h(h)} acima do ruído — vale investigar "
                       "(ainda sem spread e fila de ordens reais)")
        label = "Sem taxa" if rt == 0 else f"Taxa {f:g}% por lado"
        print(f"  {label}: {verdict}.")
        if rt > 0:
            first = next((hh for hh, ss in stats if ss["mean"] > rt), None)
            where = (f"cobre essa taxa só a partir de {_fmt_h(first)}" if first else
                     f"não cobre essa taxa em nenhum horizonte até {_fmt_h(stats[-1][0])}")
            print(f"  {'':>{len(label) + 2}}O movimento médio {where}.")
    if 0 in rts:
        print("  ('Sem taxa' é hipotético: no BTC/USDT a taxa é 0.1% por lado; taxa zero só")
        print("  existe para ordens maker em pares promocionais, e elas só executam quando")
        print("  alguém cruza o seu preço — muitas vezes logo antes de o preço ir contra.)")
    print("  Preços = último negócio de cada segundo. Na prática ainda há o spread")
    print("  (BTC/USDT: ~0.01 USD, desprezível) e a latência da VM até a Binance.")
    print("=" * 78)


if __name__ == "__main__":
    main()
