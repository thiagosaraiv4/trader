"""
core/performance_engine.py
--------------------------
Calcula métricas de performance globais e equity curve de modo O(1) para o live session.
Reidrata de maneira linear no __init__, mas processa cada novo trade na arquitetura event-driven 
em tempo O(1) puro, sem iterar listas de trades (exceto numpy para stddev do rolamento de Sharpe).
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Callable, Any

import numpy as np
from utils.logger import get_logger


@dataclass(frozen=True, slots=True)
class PerformanceSnapshot:
    """Immutable snapshot of current performance state."""
    total_trades: int
    win_rate: float
    profit_factor: float
    sharpe: float | None
    calmar: float | None
    max_drawdown_pct: float
    max_consecutive_losses: int
    avg_winner_pct: float
    avg_loser_pct: float
    current_streak: int
    session_return_pct: float
    equity_timestamps: tuple[float, ...]
    equity_values: tuple[float, ...]


class PerformanceEngine:
    """
    Motor Python-puro (sem QObject) para análise ao vivo.
    """

    def __init__(
        self,
        initial_balance: float,
        trade_log: list[dict[str, Any]],
        on_snapshot: Callable[[PerformanceSnapshot], None],
    ) -> None:
        self._initial_balance: float = initial_balance
        self._on_snapshot: Callable[[PerformanceSnapshot], None] = on_snapshot

        self._cum_profit: float = 0.0
        self._cum_loss: float = 0.0
        self._peak_balance: float = initial_balance
        self._max_drawdown_pct: float = 0.0
        self._wins: int = 0
        self._losses: int = 0
        self._current_streak: int = 0
        self._max_consecutive_losses: int = 0
        self._returns: deque[float] = deque(maxlen=252)

        self._equity_timestamps: list[float] = []
        self._equity_values: list[float] = []
        
        # Inicia a curva no marco zero com o balance inicial, caso não tenha log prévio,
        # ou se quisermos ter a linha de base
        # Nota: Só adicionamos O ponto zero se tivermos trade log, ou podemos apenas deixar
        # a linha ser construída aos poucos. Vamos adicionar o initial_balance manual na UI.

        self._log = get_logger(__name__, component="performance_engine")
        
        # Rehidratação - replay amortizado no start
        if trade_log:
            for trade in trade_log:
                if "closed_pnl" in trade:
                    self._internal_process_trade(trade)
        
        self._log.info(
            "PerformanceEngine initialized",
            extra={"rehydrated_trades": len([t for t in trade_log if "closed_pnl" in t]) if trade_log else 0}
        )

    def on_trade_closed(self, trade: dict[str, Any]) -> None:
        """
        Processa um trade recém fechado e notifica a UI via on_snapshot em O(1).
        Deve conter as chaves 'closed_pnl', 'pnl_pct', 'balance_after', 'timestamp'.
        """
        self._internal_process_trade(trade)
        snapshot = self._build_snapshot()
        self._on_snapshot(snapshot)

    def _internal_process_trade(self, trade: dict[str, Any]) -> None:
        pnl = trade.get("closed_pnl", 0.0)
        pnl_pct = trade.get("pnl_pct", 0.0)
        balance = trade.get("balance_after", self._initial_balance)
        ts = trade.get("timestamp")

        if ts is not None:
            # timestamp dict is often datetime. Se for object, .timestamp(). Se dict/str, ignora
            if hasattr(ts, "timestamp"):
                self._equity_timestamps.append(ts.timestamp())
            else:
                self._equity_timestamps.append(0.0)
        self._equity_values.append(balance)

        self._returns.append(pnl_pct)

        # Atualiza métricas Win/Loss e Profit Factor
        if pnl > 0:
            self._cum_profit += pnl
            self._wins += 1
            if self._current_streak < 0:
                self._current_streak = 0
            self._current_streak += 1
        elif pnl < 0:
            self._cum_loss += abs(pnl)
            self._losses += 1
            if self._current_streak > 0:
                self._current_streak = 0
            self._current_streak -= 1
            if abs(self._current_streak) > self._max_consecutive_losses:
                self._max_consecutive_losses = abs(self._current_streak)

        # Atualiza Max Drawdown incremental via Max(Peak) state (O(1))
        if balance > self._peak_balance:
            self._peak_balance = balance
        
        drawdown_pct = (self._peak_balance - balance) / self._peak_balance if self._peak_balance > 0 else 0.0
        if drawdown_pct > self._max_drawdown_pct:
            self._max_drawdown_pct = drawdown_pct

    def _build_snapshot(self) -> PerformanceSnapshot:
        total = self._wins + self._losses
        win_rate = (self._wins / total) if total > 0 else 0.0
        profit_factor = (self._cum_profit / self._cum_loss) if self._cum_loss > 0 else float('inf') if self._cum_profit > 0 else 0.0
        
        avg_winner = (self._cum_profit / self._wins) if self._wins > 0 else 0.0
        avg_loser = (self._cum_loss / self._losses) if self._losses > 0 else 0.0
        
        current_balance = self._equity_values[-1] if self._equity_values else self._initial_balance
        session_ret_pct = (current_balance - self._initial_balance) / self._initial_balance

        # Sharpe ratio simplificado nos retornos (frequencia trade baseada)
        sharpe = None
        if len(self._returns) >= 30:
            arr = np.array(self._returns)
            stdev = np.std(arr)
            if stdev > 1e-10:
                # Retorna Trade-Sharpe Ratio normalizado sem raiz caso n haja freq temporal especifica
                # para o projeto local. O usuário pede apenas "media/std" sem mais calc 
                sharpe = np.mean(arr) / stdev

        # Calmar Ratio: Return / MaxDrawdown
        calmar = None
        if self._max_drawdown_pct > 0.0:
            calmar = session_ret_pct / self._max_drawdown_pct

        return PerformanceSnapshot(
            total_trades=total,
            win_rate=win_rate,
            profit_factor=profit_factor,
            sharpe=sharpe,
            calmar=calmar,
            max_drawdown_pct=self._max_drawdown_pct,
            max_consecutive_losses=self._max_consecutive_losses,
            avg_winner_pct=avg_winner,
            avg_loser_pct=avg_loser,
            current_streak=self._current_streak,
            session_return_pct=session_ret_pct,
            equity_timestamps=tuple(self._equity_timestamps),
            equity_values=tuple(self._equity_values),
        )
