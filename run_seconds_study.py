"""
run_seconds_study.py
--------------------
Dá para ganhar dinheiro operando BTC em SEGUNDOS? Antes de escrever um robô,
mede com dados reais de 1 segundo da Binance (data.binance.vision) o "muro
das taxas":

  1. Quanto o preço anda em 1 s, 5 s, 30 s, 1 min, 15 min...
  2. Que fração desses movimentos cobre a taxa de ida+volta
  3. Que taxa de acerto de direção seria preciso para empatar
  4. Quanto acertam os sinais mais simples (momento / reversão)

Uso (rode na VM — a Binance bloqueia servidores nos EUA):
    python run_seconds_study.py                      # últimos 7 dias, BTCUSDT
    python run_seconds_study.py --days 3 --end 2026-10-01
    python run_seconds_study.py --fees 0.1,0.075,0   # taxas por lado, em %

Baixa um arquivo por dia (~2-4 MB) e guarda em backtest_cache/vision/.
Não negocia nada; só mede.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from core.config import AppConfig
from data.binance_vision import CACHE_DIR, VisionError, download_day, read_day

_DEFAULT_FEES = (0.1, 0.075, 0.02, 0.0)        # % per side
_DEFAULT_HORIZONS = (1, 2, 5, 10, 30, 60, 300, 900)
_MAX_DAYS = 14                                  # ~190 MB peak: leaves room for the live bot on a 1 GB VM
_W = 11                                         # width of each fee column


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


def _load(symbol: str, days: list[date]) -> list[tuple[date, pd.Series]]:
    """Close of every second with trades, per available day."""
    loaded = []
    for i, day in enumerate(days, 1):
        print(f"\r[*] Baixando {symbol} 1s {day:%Y-%m-%d} ({i}/{len(days)})", end="", flush=True)
        path = download_day(symbol, day)
        if path is None:
            print(f"\n[i] {day:%Y-%m-%d} não disponível na Binance (ainda não publicado?) — pulado")
            continue
        loaded.append((day, read_day(path)["close"]))
    print()
    return loaded


def _blocks(loaded: list[tuple[date, pd.Series]]) -> tuple[list[np.ndarray], int, int]:
    """
    Closes on a full 1-second grid, one array per run of consecutive days
    (a missing day must not become a flat stretch). Seconds without trades
    repeat the last close. Returns (blocks, seconds, filled seconds).
    """
    runs: list[list[pd.Series]] = []
    prev: date | None = None
    for day, closes in loaded:
        if prev is None or day != prev + timedelta(days=1):
            runs.append([])
        runs[-1].append(closes)
        prev = day
    blocks, total, filled = [], 0, 0
    for run in runs:
        closes = pd.concat(run)
        grid = pd.date_range(closes.index[0], closes.index[-1], freq="1s")
        blocks.append(closes.reindex(grid).ffill().to_numpy())
        total += len(grid)
        filled += len(grid) - len(closes)
    return blocks, total, filled


def _horizon(blocks: list[np.ndarray], h: int, rts: list[float]) -> dict | None:
    """
    Stats of h-second returns, share of them above each round-trip fee in
    `rts`, and the simplest signals over that horizon.
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
    p, f, fl = np.concatenate(past), np.concatenate(fut), np.concatenate(fut_late)
    s = np.sign(p)
    m = s != 0
    gross = float(np.mean(s[m] * f[m])) if m.any() else 0.0
    gross_late = float(np.mean(s[m] * fl[m])) if m.any() else 0.0
    moved = m & (f != 0)
    hit = float(np.mean(np.sign(f[moved]) == s[moved])) if moved.any() else 0.5
    # Use whichever of momentum / reversal did better (chosen in-sample: optimistic)
    label = "momento"
    if gross < 0:
        label, gross, gross_late, hit = "reversão", -gross, -gross_late, 1 - hit
    return {
        "n": len(a), "mean": float(a.mean()), "median": float(np.median(a)),
        "p90": float(np.quantile(a, 0.9)), "cover": [float(np.mean(a > rt)) for rt in rts],
        "signal": label, "hit": hit, "gross": gross, "gross_late": gross_late,
    }


def _fmt_h(h: int) -> str:
    return f"{h // 60} min" if h >= 60 and h % 60 == 0 else f"{h} s"


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

    symbol = args.symbol.replace("/", "").upper()
    end = args.end or (datetime.now(timezone.utc).date() - timedelta(days=1))
    days = [end - timedelta(days=i) for i in range(args.days - 1, -1, -1)]

    print("=" * 78)
    print(f"MURO DAS TAXAS — {symbol}, candles de 1 segundo (data.binance.vision)")
    print("=" * 78)
    try:
        loaded = _load(symbol, days)
    except VisionError as e:
        print(f"\n[!] Falha no download: {e}")
        print("    A Binance bloqueia servidores nos EUA (Codespaces); rode na VM.")
        sys.exit(1)
    if not loaded:
        print("[!] Nenhum dia disponível no período.")
        sys.exit(1)
    blocks, total, filled = _blocks(loaded)
    print(f"[*] {len(loaded)} dia(s), {total:,} segundos; {filled:,} sem negócio "
          f"({filled / total:.1%}) repetem o último preço.")
    print(f"[*] Cache: {CACHE_DIR}")

    fees = sorted(set(args.fees), reverse=True)
    rts = [2 * f / 100 for f in fees]                       # round trip, fraction
    stats = [(h, _horizon(blocks, h, rts)) for h in sorted(set(args.horizons))]
    stats = [(h, s) for h, s in stats if s is not None]
    if not stats:
        print("[!] Dados curtos demais para esses horizontes.")
        sys.exit(1)

    fee_hdr = "".join(f"{2 * f:>{_W - 1}.2f}%" for f in fees)
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
            need = 0.5 + rt / (2 * s["mean"])
            cells += f"{'impossível':>{_W}}" if need > 1 else f"{need:>{_W}.1%}"
        print(f"   {_fmt_h(h):>9} |{cells}")
    print("   ('impossível' = nem acertando 100% das direções o movimento médio paga a taxa)")

    print("\n3) O sinal mais simples: seguir (momento) ou inverter (reversão) o movimento")
    print("   dos últimos N segundos. À direita, resultado líquido por trade, por taxa:")
    print(f"   {'horizonte':>9} {'sinal':>9} {'acerto':>7} {'bruto':>9} {'1s atraso':>10} |{fee_hdr}")
    for h, s in stats:
        net = "".join(f"{s['gross'] - rt:>+{_W}.4%}" for rt in rts)
        print(f"   {_fmt_h(h):>9} {s['signal']:>9} {s['hit']:>7.1%} {s['gross']:>+9.4%} "
              f"{s['gross_late']:>+10.4%} |{net}")
    print("   (bruto = ganho médio por trade antes das taxas, entrando no fechamento do segundo;")
    print("    '1s atraso' = o mesmo entrando 1 segundo depois. O lado momento/reversão foi")
    print("    escolhido olhando estes mesmos dados, então o número é otimista.)")

    print("\n" + "=" * 78)
    print("CONCLUSÃO")
    for f, rt in zip(fees, rts):
        best = max(stats, key=lambda hs: hs[1]["gross"] - rt)
        best_net = f"melhor sinal simples: {best[1]['gross'] - rt:+.4%}/trade ({_fmt_h(best[0])})"
        if rt == 0:
            print(f"  Sem taxa: {best_net}.")
            continue
        first = next((h for h, s in stats if s["mean"] > rt), None)
        where = (f"só a partir de {_fmt_h(first)}" if first else
                 f"em nenhum horizonte até {_fmt_h(stats[-1][0])}")
        print(f"  Taxa {f:g}% por lado: o movimento médio cobre a taxa {where};")
        print(f"  {'':>{len(f'Taxa {f:g}% por lado: ')}}{best_net}.")
    print("  Preços = último negócio de cada segundo. Na prática ainda há o spread")
    print("  (BTC/USDT: ~0.01 USD, desprezível) e a latência da VM até a Binance.")
    print("=" * 78)


if __name__ == "__main__":
    main()
