"""
core/market_monitor.py
----------------------
Guardian de integridade do feed. Monitora o recebimento de novos bars.
Se o feed estagnar por mais de 3x o timeframe, emite um sinal feed_stale.
O AlgoEngine deve ser protegido (modo HALTED) enquanto a feed não restaurar.
"""
from __future__ import annotations

import time
from PySide6.QtCore import QObject, QTimer, Signal

from core.types import BarData
from utils.logger import get_logger


class MarketMonitor(QObject):
    """
    Rastreia o `last_bar_timestamp` da data feed ativa.
    QTimer verifica staleness baseado em time.monotonic().

    Sinais:
      - feed_stale(float): Emitido quando feed congela (passa elapsed time).
      - feed_restored(): Emitido quando a feed retoma após estar stale.
    """

    feed_stale: Signal = Signal(float)  # elapsed seconds
    feed_restored: Signal = Signal()

    def __init__(self, timeframe_seconds: int, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._timeframe_seconds = timeframe_seconds
        self._stale_threshold = 3 * timeframe_seconds
        self._last_bar_ts: float = time.monotonic()
        self._is_stale: bool = False
        self._active: bool = False

        self._log = get_logger(__name__, component="market_monitor")

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._on_timer_tick)

    def start(self) -> None:
        self._last_bar_ts = time.monotonic()
        self._active = True
        self._is_stale = False
        self._timer.start()
        self._log.info("MarketMonitor started", extra={"stale_threshold_sec": self._stale_threshold})

    def stop(self) -> None:
        self._active = False
        self._timer.stop()
        self._log.info("MarketMonitor stopped")

    def on_bar_received(self, bar: BarData) -> None:
        """Chamado pelo MarketController a cada novo bar."""
        if not self._active:
            return

        self._last_bar_ts = time.monotonic()

        if self._is_stale:
            self._is_stale = False
            self._log.info("Feed RESTORED (new bar received)")
            self.feed_restored.emit()

    def _on_timer_tick(self) -> None:
        if not self._active:
            return

        elapsed = time.monotonic() - self._last_bar_ts

        if elapsed > self._stale_threshold and not self._is_stale:
            self._is_stale = True
            self._log.warning(
                "Feed STALE detected",
                extra={"elapsed_sec": round(elapsed, 1), "threshold": self._stale_threshold}
            )
            self.feed_stale.emit(elapsed)
        elif self._is_stale:
            # Continua atualizando a UI se quisermos que o contador avance,
            # mas conforme o requisito "emitir feed_stale(elapsed: float)":
            # vamos apenas emitir quando ocorrer a transição.
            pass
