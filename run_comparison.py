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


def _compute_live_metrics(trades: list[dict], initial_balance: float) -> dict:
    closes = [t for t in trades if "closed_pnl" in t]
    total_trades = len(closes)
    if total_trades == 0:
        return {"total_trades": 0, "win_rate": 0.0, "profit_factor": 0.0, "return_pct": 0.0}

    wins = 0
    gross_profit = 0.0
    gross_loss = 0.0

    for t in closes:
        pnl = t["closed_pnl"]
        if pnl > 0:
            wins += 1
            gross_profit += pnl
        elif pnl < 0:
            gross_loss += abs(pnl)

    win_rate = wins / total_trades
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float('inf') if gross_profit > 0 else 0.0)

    # Balance after the last CLOSE (after an entry, balance_after is cash only)
    final_balance = closes[-1].get("balance_after", initial_balance)
    return_pct = (final_balance - initial_balance) / initial_balance

    return {
        "total_trades": total_trades,
        "win_rate": win_rate * 100,
        "profit_factor": profit_factor,
        "return_pct": return_pct * 100,
    }


def _compute_backtest_metrics(
    result: BacktestResult,
    initial_balance: float,
    min_entry: datetime | None = None,
) -> dict:
    """Same metrics as the live side, from closed trades only (no end-of-data close)."""
    trades = [
        t for t in result.trade_log
        if t.exit_reason != "end_of_data"
        and (min_entry is None or t.entry_time >= min_entry)
    ]
    if not trades:
        return {"total_trades": 0, "win_rate": 0.0, "profit_factor": 0.0, "return_pct": 0.0}

    gross_profit = sum(t.profit for t in trades if t.profit > 0)
    gross_loss = abs(sum(t.profit for t in trades if t.profit < 0))
    wins = sum(1 for t in trades if t.profit > 0)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float('inf') if gross_profit > 0 else 0.0)

    return {
        "total_trades": len(trades),
        "win_rate": wins / len(trades) * 100,
        "profit_factor": profit_factor,
        "return_pct": (trades[-1].balance_after - initial_balance) / initial_balance * 100,
    }


def _divergence(live: float, backtest: float) -> float:
    """Relative divergence in %, safe for zeros and infinite profit factors."""
    if math.isinf(live) or math.isinf(backtest):
        return 0.0 if live == backtest else math.inf
    if backtest != 0:
        return abs((live - backtest) / backtest) * 100
    return math.inf if live != 0 else 0.0


def compare_live_with_backtest() -> None:
    print("============================================================")
    print("V3 LIVE vs BACKTEST - COMPARISON ENGINE (5-DAY)")
    print("============================================================")

    cfg = AppConfig()

    # Load Real Session
    session = load_session()
    if not session or not session.trade_log:
        print("[!] Nenhuma sessao ao vivo com log gravado. Abortando.")
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
    print(f" LIVE vs BACKTEST (DIVERGÊNCIA ACEITÁVEL < 20%) — dados: {bt_res.data_source}")
    print("============================================================\n")

    metrics_list = ["total_trades", "win_rate", "profit_factor", "return_pct"]
    for m in metrics_list:
        liv_v = live_metrics[m]
        bt_v = bt_metrics[m]
        divergence = _divergence(liv_v, bt_v)

        warn_flag = "🚨 >20% DIVERGENCIA" if divergence > 20.0 else "✅ ALINHADO"

        print(f" -> {m.upper():<15}")
        print(f"    Live: {liv_v:>8.2f} | Backtest: {bt_v:>8.2f} | Δ: {divergence:>6.2f}% [{warn_flag}]")


if __name__ == "__main__":
    compare_live_with_backtest()
