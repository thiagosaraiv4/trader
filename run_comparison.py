"""
run_comparison.py
-----------------
Script para ser executado no 5º dia pós-live trading.
Objetivo: Carregar o histórico consolidado do `session.json`, verificar as datas operadas
pelo robô live e acionar o `BacktestEngine` rigorosamente com a mesma configuração e período.
Verifica a divergência entre Gêmeo Digital empírico e motor teórico. Se diverge > 20%, suspeita de falhas.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from enum import Enum
from core.config import AppConfig
from core.backtest_engine import BacktestEngine
from core.session import SessionState, load_session

def _compute_live_metrics(trade_log: list[dict], initial_balance: float) -> dict:
    if not trade_log:
        return {"total_trades": 0, "win_rate": 0.0, "profit_factor": 0.0, "return_pct": 0.0}
    
    trades = [t for t in trade_log if "closed_pnl" in t]
    total_trades = len(trades)
    if total_trades == 0:
        return {"total_trades": 0, "win_rate": 0.0, "profit_factor": 0.0, "return_pct": 0.0}

    wins = 0
    losses = 0
    gross_profit = 0.0
    gross_loss = 0.0

    for t in trades:
        pnl = t["closed_pnl"]
        if pnl > 0:
            wins += 1
            gross_profit += pnl
        elif pnl < 0:
            losses += 1
            gross_loss += abs(pnl)
            
    win_rate = (wins / total_trades) if total_trades > 0 else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float('inf') if gross_profit > 0 else 0.0)
    
    # Calculate live return
    final_balance = trade_log[-1].get("balance_after", initial_balance)
    return_pct = (final_balance - initial_balance) / initial_balance

    return {
        "total_trades": total_trades,
        "win_rate": win_rate * 100,
        "profit_factor": profit_factor,
        "return_pct": return_pct * 100,
    }

async def compare_live_with_backtest():
    print("============================================================")
    print("V3 LIVE vs BACKTEST - COMPARISON ENGINE (5-DAY)")
    print("============================================================")

    cfg = AppConfig()
    
    # Load Real Session
    session = load_session()
    if not session or not session.trade_log:
        print("[!] Nenhuma sessao ao vivo com log gravado. Abortando.")
        return

    # Extract date boundaries
    start_str = session.trade_log[0].get("timestamp")
    end_str = session.trade_log[-1].get("timestamp")
    
    if isinstance(start_str, str):
        start_dt = datetime.fromisoformat(start_str)
    else:
        start_dt = start_str # might be datetime object already
        
    if isinstance(end_str, str):
        end_dt = datetime.fromisoformat(end_str)
    else:
        end_dt = end_str

    print(f"[*] Live bounds detectado: {start_dt} ---> {end_dt}")
    
    live_metrics = _compute_live_metrics(session.trade_log, 10_000.0) # Assumindo 10k hardcoded padrao

    print("\n[*] Re-rodando Motor Historico no mesmo Gêmeo Digital...")
    
    # Run Backtest identically (BacktestEngine always runs Strategy V3)
    engine = BacktestEngine(
        symbol=str(cfg.default_symbol),
        timeframe=cfg.default_timeframe.value,
        initial_balance=10_000.0,
        start_date=start_dt,
        end_date=end_dt,
        use_ccxt=True,
    )

    bt_res = engine.run()
    if bt_res.total_trades == 0 and not bt_res.equity_curve:
        print("[!] Backtest falhou em gerar relatórios (dados insuficientes?).")
        return

    bt_metrics = {
        "total_trades": bt_res.total_trades,
        "win_rate": bt_res.win_rate * 100,               # ratio 0..1
        "profit_factor": bt_res.profit_factor,
        "return_pct": bt_res.total_return_pct * 100,     # ratio
    }

    print("\n============================================================")
    print(f" LIVE vs BACKTEST (DIVERGÊNCIA ACEITÁVEL < 20%)")
    print("============================================================\n")

    metrics_list = ["total_trades", "win_rate", "profit_factor", "return_pct"]
    for m in metrics_list:
        liv_v = live_metrics[m]
        bt_v = bt_metrics[m]
        
        # Avoid division by zero
        if bt_v != 0:
            divergence = abs((liv_v - bt_v) / bt_v) * 100
        else:
            divergence = float('inf') if liv_v != 0 else 0.0
            
        warn_flag = "🚨 >20% DIVERGENCIA" if divergence > 20.0 else "✅ ALINHADO"
        
        print(f" -> {m.upper():<15}")
        print(f"    Live: {liv_v:>8.2f} | Backtest: {bt_v:>8.2f} | Δ: {divergence:>6.2f}% [{warn_flag}]")

if __name__ == "__main__":
    asyncio.run(compare_live_with_backtest())
