"""
run_comparison.py
-----------------
Script para ser executado após alguns dias de live trading (ex.: 5º dia).

Compara o robô ao vivo (`session.json`) com o `BacktestEngine` rodando a MESMA
estratégia (V3) sobre as MESMAS barras que o robô viu (`live_bars.csv`, gravado
pelo headless_main.py). Reinícios do robô (ex.: Codespace parado) viram segmentos:
as barras de quando ele estava desligado só aquecem os indicadores, como ao vivo.
Verifica a divergência entre Gêmeo Digital empírico e motor teórico. Se diverge
> 20%, suspeita de falhas.

Posições ainda abertas no fim não contam como trade (nem ao vivo nem no backtest).

Sem `live_bars.csv`, baixa o histórico via ccxt/Binance — bloqueada em servidores
nos EUA (Codespaces). Se o backtest cair para dados sintéticos, a comparação é
abortada (o resultado não teria valor).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from core.backtest_engine import BacktestEngine, BacktestResult
from core.bar_log import BAR_LOG_PATH, load_segments
from core.config import AppConfig
from core.session import load_session
from core.strategy_params import StrategyParams, load_params
from core.types import Timeframe

_INITIAL_BALANCE = 10_000.0
# Bars the strategy needs before it can signal (ATR window of 14 + previous EMA/SMA)
_STRATEGY_WARMUP_BARS = 15


def _parse_ts(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def _net_metrics(net_pnls: list[float], start_balance: float, end_balance: float) -> dict:
    """Trades, win rate, profit factor and return — all net of fees."""
    if not net_pnls:
        return {"total_trades": 0, "win_rate": 0.0, "profit_factor": 0.0, "return_pct": 0.0}
    wins = [p for p in net_pnls if p > 0]
    losses = [p for p in net_pnls if p < 0]
    profit_factor = (sum(wins) / abs(sum(losses))) if losses else (float("inf") if wins else 0.0)
    return {
        "total_trades": len(net_pnls),
        "win_rate": len(wins) / len(net_pnls) * 100,
        "profit_factor": profit_factor,
        "return_pct": (end_balance - start_balance) / start_balance * 100,
    }


def _compute_live_metrics(trades: list[dict], initial_balance: float) -> dict:
    """Live closes; each trade's net result = change in cash between closes."""
    closes = [t for t in trades if "closed_pnl" in t]
    pnls, prev = [], initial_balance
    for t in closes:
        pnls.append(t["balance_after"] - prev)
        prev = t["balance_after"]
    return _net_metrics(pnls, initial_balance, prev)


def _compute_backtest_metrics(
    result: BacktestResult,
    initial_balance: float,
    min_entry: datetime | None = None,
) -> dict:
    """Same metrics as the live side, from closed trades only (no end-of-data close)."""
    trades = [t for t in result.trade_log if t.exit_reason != "end_of_data"]
    pnls, prev, start = [], initial_balance, None
    for t in trades:
        net = t.balance_after - prev
        if min_entry is None or t.entry_time >= min_entry:
            if start is None:
                start = prev
            pnls.append(net)
        prev = t.balance_after
    return _net_metrics(pnls, start if start is not None else initial_balance, prev)


def _divergence(live: float, backtest: float) -> float:
    """Relative divergence in %, safe for zeros and infinite profit factors."""
    if math.isinf(live) or math.isinf(backtest):
        return 0.0 if live == backtest else math.inf
    if backtest != 0:
        return abs((live - backtest) / backtest) * 100
    return math.inf if live != 0 else 0.0


# Trend following on 4h makes few trades; relative % differences on a handful
# of trades or near-zero returns are noise, so the checks are absolute.
_MIN_TRADES_FOR_RATIOS = 10


def _verdict(metric: str, live: dict, bt: dict) -> tuple[str, str]:
    """(difference text, status) for one metric."""
    lv, bv = live[metric], bt[metric]
    if metric == "total_trades":
        diff = abs(lv - bv)
        return f"{diff:.0f} trade(s)", ("🚨 DIVERGE" if diff > 1 else "✅ ALINHADO")
    if metric == "return_pct":
        diff = abs(lv - bv)
        return f"{diff:.2f} p.p.", ("🚨 DIVERGE" if diff > 0.5 else "✅ ALINHADO")
    if min(live["total_trades"], bt["total_trades"]) < _MIN_TRADES_FOR_RATIOS:
        return "-", f"… amostra pequena (<{_MIN_TRADES_FOR_RATIOS} trades)"
    if metric == "win_rate":
        diff = abs(lv - bv)
        return f"{diff:.1f} p.p.", ("🚨 DIVERGE" if diff > 10 else "✅ ALINHADO")
    rel = _divergence(lv, bv)
    return f"{rel:.0f}%", ("🚨 DIVERGE" if rel > 20 else "✅ ALINHADO")


def compare_live_with_backtest() -> None:
    print("============================================================")
    print("V3 LIVE vs BACKTEST - COMPARISON ENGINE (5-DAY)")
    print("============================================================")

    cfg = AppConfig()

    # Load Real Session
    session = load_session()
    if session is None:
        print("[!] Nenhuma sessão ao vivo (session.json). Abortando.")
        return

    # Same parameters the live session ran with
    stored = session.meta.get("strategy_params")
    params = StrategyParams.from_dict(stored) if stored else load_params()
    timeframe = Timeframe(params.timeframe)
    tf = timedelta(seconds=timeframe.to_seconds())
    print(f"[*] Parâmetros: {params.to_dict()}")

    segments = load_segments()
    bt_min_entry: datetime | None = None
    if segments:
        # Replay exactly what the live bot saw, restart by restart
        window_start = segments[0].bars[0].timestamp
        # The signal of the last recorded bar fills at the first poll of the
        # next bar (a few seconds in); later live activity was not recorded.
        window_end = segments[-1].bars[-1].timestamp + tf + timedelta(seconds=30)
        n_bars = sum(len(seg.bars) for seg in segments)
        restarts = sum(1 for seg in segments[1:] if seg.new_run)
        print(
            f"[*] Replay de {n_bars} barras gravadas pelo robô ({BAR_LOG_PATH.name}): "
            f"{len(segments)} segmento(s), {restarts} reinício(s)"
        )
        engine = BacktestEngine(
            symbol=str(cfg.default_symbol),
            timeframe=timeframe.value,
            params=params,
            initial_balance=_INITIAL_BALANCE,
            segments=segments,
        )
    else:
        if not session.trade_log:
            print(f"[!] Sem {BAR_LOG_PATH.name} e sem trades ao vivo — nada para comparar ainda.")
            return
        first_fill = _parse_ts(session.trade_log[0]["timestamp"])
        tf_sec = int(tf.total_seconds())
        first_bar = datetime.fromtimestamp(
            int(first_fill.timestamp()) // tf_sec * tf_sec, tz=first_fill.tzinfo,
        )
        # Start early enough for the strategy to be able to signal the first
        # live entry; backtest trades entered before it are ignored.
        window_start = first_bar
        bt_min_entry = first_bar
        window_end = _parse_ts(session.trade_log[-1]["timestamp"]) + tf
        print(f"[*] {BAR_LOG_PATH.name} ausente — baixando histórico via ccxt")
        engine = BacktestEngine(
            symbol=str(cfg.default_symbol),
            timeframe=timeframe.value,
            params=params,
            initial_balance=_INITIAL_BALANCE,
            start_date=first_bar - _STRATEGY_WARMUP_BARS * tf,
            end_date=window_end,
            use_ccxt=True,
        )

    print(f"[*] Janela comparada: {window_start} ---> {window_end}")

    trades_before = [t for t in session.trade_log if _parse_ts(t["timestamp"]) < window_start]
    trades_in_window = [
        t for t in session.trade_log
        if window_start <= _parse_ts(t["timestamp"]) < window_end
    ]
    live_initial = _INITIAL_BALANCE
    closes_before = [t for t in trades_before if "closed_pnl" in t]
    if closes_before:
        live_initial = closes_before[-1].get("balance_after", _INITIAL_BALANCE)
    if trades_before:
        print(
            f"[!] {len(trades_before)} trades do session.json são anteriores às barras e foram "
            "ignorados. Para comparação exata, comece com session.json e live_bars.csv novos."
        )

    live_metrics = _compute_live_metrics(trades_in_window, live_initial)

    print("\n[*] Re-rodando Motor Historico no mesmo Gêmeo Digital...")
    bt_res = engine.run()

    if bt_res.data_source == "synthetic":
        print("[!] O backtest só conseguiu dados SINTÉTICOS (Binance inacessível daqui?).")
        print("    Comparação abortada — rode o robô por alguns dias para gerar live_bars.csv.")
        return
    if not bt_res.equity_curve:
        print("[!] Backtest falhou em gerar relatórios (dados insuficientes?).")
        return

    bt_metrics = _compute_backtest_metrics(bt_res, _INITIAL_BALANCE, bt_min_entry)

    if session.position and session.position.get("side") not in (None, "FLAT"):
        print("[i] O robô está com posição aberta — ela não entra na comparação.")

    print("\n============================================================")
    print(f" LIVE vs BACKTEST — dados: {bt_res.data_source}")
    print("============================================================\n")

    metrics_list = ["total_trades", "win_rate", "profit_factor", "return_pct"]
    for m in metrics_list:
        liv_v = live_metrics[m]
        bt_v = bt_metrics[m]
        diff, status = _verdict(m, live_metrics, bt_metrics)

        print(f" -> {m.upper():<15}")
        print(f"    Live: {liv_v:>8.2f} | Backtest: {bt_v:>8.2f} | Δ: {diff:>10} [{status}]")
    print("\n(Tudo líquido de taxas. Trades: tolerância 1; retorno: 0,5 p.p.;"
          f" win rate e profit factor só com {_MIN_TRADES_FOR_RATIOS}+ trades.)")


if __name__ == "__main__":
    compare_live_with_backtest()
