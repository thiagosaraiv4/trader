"""
run_comparison.py
-----------------
Script para ser executado após alguns dias de live trading (ex.: 5º dia).

Compara o robô ao vivo (`session.json`) com o `BacktestEngine` rodando a MESMA
estratégia (V3) sobre as MESMAS barras que o robô viu (`live_bars.csv`, gravado
pelo headless_main.py). Verifica a divergência entre Gêmeo Digital empírico e
motor teórico. Se diverge > 20%, suspeita de falhas.

Sem `live_bars.csv`, baixa o histórico via ccxt/Binance — bloqueada em servidores
nos EUA (Codespaces). Se o backtest cair para dados sintéticos, a comparação é
abortada (o resultado não teria valor).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from core.backtest_engine import BacktestEngine
from core.bar_log import BAR_LOG_PATH, load_bars
from core.config import AppConfig
from core.session import load_session

# Warm-up = the live bot's snapshot (headless_main.py: 200 closed bars)
_LIVE_WARMUP_BARS = 200
_INITIAL_BALANCE = 10_000.0


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
    tf = timedelta(seconds=cfg.default_timeframe.to_seconds())

    # Load Real Session
    session = load_session()
    if not session or not session.trade_log:
        print("[!] Nenhuma sessao ao vivo com log gravado. Abortando.")
        return

    live_bars = load_bars()
    if len(live_bars) >= _LIVE_WARMUP_BARS + 10:
        # Replay exactly what the live bot saw: same warm-up, same bars
        window_start = live_bars[_LIVE_WARMUP_BARS].timestamp
        window_end = live_bars[-1].timestamp + tf
        print(f"[*] Replay de {len(live_bars)} barras gravadas pelo robô ({BAR_LOG_PATH.name})")
        engine = BacktestEngine(
            symbol=str(cfg.default_symbol),
            timeframe=cfg.default_timeframe.value,
            initial_balance=_INITIAL_BALANCE,
            bars=live_bars,
            warmup_bars=_LIVE_WARMUP_BARS,
        )
    else:
        window_start = _parse_ts(session.trade_log[0]["timestamp"])
        window_end = _parse_ts(session.trade_log[-1]["timestamp"]) + tf
        print(f"[*] {BAR_LOG_PATH.name} ausente ou curto — baixando histórico via ccxt")
        engine = BacktestEngine(
            symbol=str(cfg.default_symbol),
            timeframe=cfg.default_timeframe.value,
            initial_balance=_INITIAL_BALANCE,
            start_date=window_start,
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

    bt_metrics = {
        "total_trades": bt_res.total_trades,
        "win_rate": bt_res.win_rate * 100,               # ratio 0..1
        "profit_factor": bt_res.profit_factor,
        "return_pct": bt_res.total_return_pct * 100,     # ratio
    }

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
