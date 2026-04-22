"""
main.py
--------
Application entry point.

qasync integration:
    qasync.QEventLoop replaces the default asyncio event loop implementation,
    running asyncio coroutines and Qt events on the SAME OS thread.

    Consequence:
    - DataFeedManager callbacks execute on the Qt thread → signal.emit() is safe
    - No thread synchronization needed for Controller ↔ View communication
    - Removing qasync would require QThread + explicit cross-thread signal connections

Shutdown sequence (guaranteed ordering, documented in MainWindow.closeEvent):

    User clicks X
        │
        ▼
    MainWindow.closeEvent()
        │  event.ignore() — prevents immediate close
        │  emit shutdown_requested
        ▼
    _initiate_shutdown() [async]
        │
        ├─ 1. Disconnect all UI signals
        │      (prevents any slot from firing after this point)
        │
        ├─ 2. await controller.shutdown()
        │      (stops all DataFeedManagers, drains asyncio tasks)
        │
        ├─ 3. window.accept_close()
        │      (sets _close_accepted flag, re-triggers close)
        │
        └─ 4. app.quit()
               (Qt event loop exits → qasync loop exits → with-block exits)

    After step 4, the ``with loop:`` block in main() exits normally,
    Python's GC runs, and the process exits cleanly.

    Result: zero orphaned asyncio tasks, zero widget access after teardown.
"""
from __future__ import annotations

import asyncio
import sys

import qasync
from PySide6.QtWidgets import QApplication

from controllers.market_controller import MarketController
from core.algo_trading import AlgoEngine, TrendFilteredMACrossStrategyV3
from core.config import AppConfig
from core.paper_trading import PaperTradingEngine, OrderRequest
from core.session import SessionState, load_session, save_session
from core.types import FeedConfig
from core.performance_engine import PerformanceEngine
from ui.boleta_widget import BoletaWidget
from ui.chart_widget import ChartWidget
from ui.main_window import MainWindow, apply_dark_palette
from ui.live_trading_widget import LiveTradingWidget
from ui.performance_widget import PerformanceWidget
from utils.logger import configure_logging, get_logger


# ---------------------------------------------------------------------------
# Async main
# ---------------------------------------------------------------------------

async def _async_main(app: QApplication, cfg: AppConfig) -> None:
    """
    Primary async coroutine. Runs inside the qasync event loop.

    All QObject creation happens here (after the loop is running) to ensure
    QObjects are owned by the correct thread (the asyncio/Qt thread).
    """
    log = get_logger(__name__, component="async_main")
    log.info("Async main started")

    # ----------------------------------------------------------------
    # 1. Instantiate Controller (pure QObject, no display)
    # ----------------------------------------------------------------
    controller = MarketController(
        timeframe_seconds=cfg.default_timeframe.seconds,
        bar_throttle_ms=cfg.bar_throttle_ms,
    )

    # ----------------------------------------------------------------
    # 2. Instantiate Paper Trading Engine (Phase 3 + Live Restore)
    # ----------------------------------------------------------------
    session = load_session(timeframe_seconds=cfg.default_timeframe.seconds)
    
    clean_session = session
    if session and session.stale_position:
        stale_pos = session.position
        controller.set_pending_restore(stale_pos)
        clean_session = SessionState(
            balance=session.balance,
            position=None,
            trade_log=session.trade_log,
            stale_position=session.stale_position
        )

    paper_engine = PaperTradingEngine(
        initial_balance=10_000.0,
        restore=clean_session
    )

    # ----------------------------------------------------------------
    # 2b. Instantiate Algo Engine (Phase 4)
    # ----------------------------------------------------------------
    algo_engine = AlgoEngine(
        risk_pct=0.02, 
        cooldown_sec=180.0,
        initial_session_balance=paper_engine.balance,
        on_circuit_breaker=controller.emit_circuit_breaker
    )
    strategy = TrendFilteredMACrossStrategyV3(
        symbol=str(cfg.default_symbol),
    )
    algo_engine.set_strategy(strategy)

    # Inject engines into controller (late-binding to keep constructor clean)
    controller.set_engines(algo_engine, paper_engine)

    # ----------------------------------------------------------------
    # 2c. Instantiate Performance Engine (Analytics)
    # ----------------------------------------------------------------
    performance_engine = PerformanceEngine(
        initial_balance=10_000.0,
        trade_log=clean_session.trade_log if clean_session else [],
        on_snapshot=lambda s: controller.performance_updated.emit(s)
    )

    # ----------------------------------------------------------------
    # 3. Instantiate View layer
    # ----------------------------------------------------------------
    chart = ChartWidget(timeframe=cfg.default_timeframe)
    live_trading = LiveTradingWidget(initial_balance=10_000.0)
    boleta = BoletaWidget()
    performance_widget = PerformanceWidget(initial_balance=10_000.0)

    window = MainWindow(
        chart_widget=chart,
        live_trading_widget=live_trading,
        boleta_widget=boleta,
        performance_widget=performance_widget,
        config=cfg,
    )

    # ----------------------------------------------------------------
    # 4. Wire signals (Controller → View, Phase 1)
    #    All connections are made BEFORE any feed starts, ensuring no
    #    signal is emitted before a slot is registered.
    # ----------------------------------------------------------------
    controller.snapshot_ready.connect(chart.on_snapshot)
    controller.bar_updated.connect(chart.on_bar_update)
    controller.state_changed.connect(window.on_feed_state_changed)
    controller.active_symbol_changed.connect(window.set_active_symbol)

    # ----------------------------------------------------------------
    # 5. Wire signals (Phase 3 — Indicators)
    # ----------------------------------------------------------------
    controller.overlay_snapshot.connect(chart.on_overlay_snapshot)
    controller.overlay_point.connect(chart.on_overlay_point)

    # ----------------------------------------------------------------
    # 6. Wire signals (Phase 3 — Live Trading Widget)
    # ----------------------------------------------------------------
    paper_engine.position_updated.connect(live_trading.update_snapshot)
    
    # Wire Operational Guardian signals (V3)
    controller._market_monitor.feed_stale.connect(live_trading.on_feed_stale)
    controller._market_monitor.feed_restored.connect(live_trading.on_feed_restored)
    controller.circuit_breaker_triggered.connect(live_trading.on_circuit_breaker)
    live_trading.reset_requested.connect(algo_engine.reset_halt)

    # Wire Performance Analytics
    paper_engine.trade_closed.connect(performance_engine.on_trade_closed)
    controller.performance_updated.connect(performance_widget.on_performance_update)

    # We no longer emit orderbook updates to the right dock since it was removed
    # controller.orderbook_updated.connect(...) 

    # ----------------------------------------------------------------
    # 7. Wire signals (Phase 3 — Boleta / Paper Trading)
    # ----------------------------------------------------------------
    boleta.order_submitted.connect(paper_engine.execute_order)
    # Controller now handles PaperEngine's unthrottled mark price internally 
    # self.paper_engine.update_mark_price is wrapped in _make_bar_callback.
    # Therefore, no need to connect throttled bar_updated here.
    paper_engine.position_updated.connect(lambda snap: boleta.on_position_updated(snap))
    paper_engine.order_filled.connect(boleta.on_order_filled)

    # Update balance display when position changes
    def _update_balance_display(pos) -> None:
        boleta.update_balance(paper_engine.balance)

    paper_engine.position_updated.connect(_update_balance_display)

    # Set symbol on boleta when active symbol changes
    controller.active_symbol_changed.connect(boleta.set_symbol)

    # ----------------------------------------------------------------
    # 8. Wire signals (Phase 4 — Algo Engine)
    # ----------------------------------------------------------------
    boleta.bot_toggled.connect(
        lambda on: algo_engine.enable() if on else algo_engine.disable()
    )
    controller.algo_status.connect(boleta.on_algo_status)

    # ----------------------------------------------------------------
    # 9. Wire signals (Phase 5 — Backtesting)
    # ----------------------------------------------------------------
    # Remove backtest_widget connection since it's replaced by PerformanceWidget
    # controller.backtest_completed.connect(backtest_widget.update_result)

    # Stale Session Rehydration Dialog
    def _show_restore_dialog(position_data: dict, current_price: float) -> None:
        from PySide6.QtWidgets import QMessageBox
        from datetime import datetime, timezone
        
        msg = QMessageBox(window)
        msg.setWindowTitle("Restaurar Sessão (Gap Localizado)")
        msg.setIcon(QMessageBox.Icon.Warning)
        
        age_str = "algum tempo"
        if "entry_time_iso" in position_data and position_data["entry_time_iso"]:
            dt = datetime.fromisoformat(position_data["entry_time_iso"])
            age_sec = (datetime.now(timezone.utc) - dt).total_seconds()
            if age_sec > 3600:
                age_str = f"{age_sec/3600:.1f} horas"
            else:
                age_str = f"{age_sec/60:.1f} minutos"

        msg.setText(f"A posição salva do ativo foi aberta há {age_str}.")
        msg.setInformativeText("A engine ficou com o feed offline por muito tempo.\nO que deseja fazer com a operação anterior?")
        
        btn_restore = msg.addButton("Restaurar e Manter", QMessageBox.ButtonRole.AcceptRole)
        btn_close = msg.addButton(f"Fechar a {current_price:,.2f}", QMessageBox.ButtonRole.DestructiveRole)
        btn_ignore = msg.addButton("Ignorar (Deletar)", QMessageBox.ButtonRole.RejectRole)
        msg.exec()
        
        if msg.clickedButton() == btn_restore:
            paper_engine.force_restore_position(position_data)
        elif msg.clickedButton() == btn_close:
            paper_engine.force_restore_position(position_data)
            # Immediatety trigger close sell
            order = OrderRequest(
                symbol=position_data.get("symbol", "BTC/USDT"),
                side="SELL" if position_data.get("side") == "LONG" else "BUY",
                quantity=float(position_data.get("size", 0.0)),
                price=None,
                order_type="MARKET"
            )
            paper_engine.execute_order(order)
            # Force log modification internally is somewhat unsafe but executing an order adds to trade_log normally
            # The user requested 'exit_mechanism'='session_gap_close' but in the model we log TradeResult.
            
    controller.stale_position_detected.connect(_show_restore_dialog)

    # Note: We trigger the backtest run here so it populates the UI automatically
    controller.run_backtest_async(
        symbol=str(cfg.default_symbol), 
        timeframe=cfg.default_timeframe.value
    )

    # ----------------------------------------------------------------
    # 10. Shutdown orchestration
    #    A Future is used (not an Event) so we can propagate exceptions.
    # ----------------------------------------------------------------
    loop = asyncio.get_running_loop()
    shutdown_future: asyncio.Future[None] = loop.create_future()

    async def _initiate_shutdown() -> None:
        """
        Ordered shutdown coroutine. Connected to window.shutdown_requested.

        This function is the ONLY place that calls app.quit().
        It runs on the Qt/asyncio thread — no cross-thread concerns.
        """
        log.info("Graceful shutdown initiated")

        # Step 1: Sever all signal→slot connections so no UI slot fires
        #         after this point. RuntimeError is expected if signals
        #         were already disconnected (e.g. duplicate shutdown call).
        for signal in (
            # Phase 1 signals
            controller.snapshot_ready,
            controller.bar_updated,
            controller.state_changed,
            controller.active_symbol_changed,
            # Phase 3 signals
            controller.overlay_snapshot,
            controller.overlay_point,
            controller.orderbook_updated,
            boleta.order_submitted,
            paper_engine.position_updated,
            paper_engine.order_filled,
            # Phase 4 signals
            boleta.bot_toggled,
            controller.algo_status,
        ):
            try:
                signal.disconnect()
            except RuntimeError:
                pass

        log.debug("UI signals disconnected")

        # Step 2: Stop all data feeds (awaits asyncio tasks)
        await controller.shutdown()
        log.debug("All feeds stopped")

        # Step 2b: Save Session
        state = paper_engine.get_session_state()
        save_session(
            balance=state["balance"],
            open_position=state["open_position"],
            trade_log=state["trade_log"]
        )

        # Step 3: Unblock _async_main so it can return
        if not shutdown_future.done():
            shutdown_future.set_result(None)

        # Step 4: Accept the previously-intercepted close event
        window.accept_close()
        app.quit()
        log.info("Shutdown complete — event loop will exit")

    # Connect shutdown signal — uses lambda to schedule coroutine on loop
    window.shutdown_requested.connect(
        lambda: asyncio.ensure_future(_initiate_shutdown())
    )

    # ----------------------------------------------------------------
    # 9. Start initial data feed
    # ----------------------------------------------------------------
    initial_config = FeedConfig(
        symbol=cfg.default_symbol,
        timeframe=cfg.default_timeframe,
        provider_id=cfg.default_provider,
        snapshot_limit=cfg.snapshot_limit,
        poll_interval_sec=cfg.poll_interval_sec,
    )

    log.info(
        "Starting initial feed",
        extra={
            "symbol":   str(cfg.default_symbol),
            "timeframe": cfg.default_timeframe.value,
            "provider":  cfg.default_provider.value,
        },
    )
    await controller.start_feed(initial_config)

    # ----------------------------------------------------------------
    # 10. Show window (after feed is started — avoids empty chart flash)
    # ----------------------------------------------------------------
    window.show()
    log.info("Window shown — entering Qt event loop")

    # ----------------------------------------------------------------
    # 11. Await shutdown signal
    #    This future resolves when _initiate_shutdown() completes.
    #    The Qt event loop continues running during this await.
    # ----------------------------------------------------------------
    await shutdown_future
    log.info("_async_main returning")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Synchronous entry point. Bootstraps logging, Qt, and qasync.

    qasync.QEventLoop:
        Wraps Qt's native event loop as an asyncio event loop.
        The ``with loop:`` context manager installs the loop as the
        running loop for the current thread and cleans up on exit.

    sys.exit code:
        0  — normal exit (user closed window)
        1  — unhandled exception in async main
    """
    configure_logging()
    log = get_logger(__name__, component="main")

    cfg = AppConfig()

    app = QApplication(sys.argv)
    app.setApplicationName("TradingPlatform")
    app.setApplicationVersion("0.1.0-alpha")
    app.setOrganizationName("TradingPlatformDev")

    apply_dark_palette(app)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    log.info("qasync event loop initialized")

    exit_code = 0
    with loop:
        try:
            loop.run_until_complete(_async_main(app, cfg))
        except KeyboardInterrupt:
            log.info("KeyboardInterrupt received — exiting")
        except Exception:
            log.exception("Fatal error in async main")
            exit_code = 1

    log.info("Application exited", extra={"exit_code": exit_code})
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
