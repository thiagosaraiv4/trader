"""
ui/main_window.py
------------------
MainWindow: application shell with docking system and dark mode.

Dark mode strategy:
    QPalette (not CSS stylesheets) ensures all native widgets inherit the
    theme automatically, including QDockWidget title bars, QScrollBar,
    QSplitter handles, etc. Fusion style is required for QPalette to apply
    consistently on all platforms (Windows/macOS/Linux).

    Supplemental QSS is used only for structural layout elements
    (borders, padding on DockWidget titles) that QPalette cannot control.

Docking layout:
    ┌─────────────────────────────┬──────────────┐
    │                             │              │
    │        ChartWidget          │  Order Book  │
    │         (Central)           │    (DOM)     │
    │                             │   [Fase 3]   │
    ├─────────────────────────────┴──────────────┤
    │           Boleta / Order Entry   [Fase 3]  │
    └─────────────────────────────────────────────┘

Graceful shutdown contract:
    1. User triggers window close (X button or Alt+F4).
    2. closeEvent() intercepts: ignores event, emits shutdown_requested.
    3. main.py coroutine: disconnects UI signals, awaits Controller.shutdown().
    4. main.py calls window.accept_close() → re-triggers close (now accepted).
    5. QApplication.quit() → qasync loop exits cleanly.

    This ordering guarantees:
    - No Qt slot can fire after the event loop stops (signals disconnected).
    - No asyncio task is orphaned (all feeds awaited before loop exit).
    - No segfault from a C++ Qt object accessed from a destroyed Python scope.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QCloseEvent, QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QLabel,
    QMainWindow,
    QSizePolicy,
    QStatusBar,
    QWidget,
    QVBoxLayout,
)

from core.config import AppConfig
from core.types import FeedState, FeedStateChange
from ui.boleta_widget import BoletaWidget
from ui.chart_widget import ChartWidget
from ui.live_trading_widget import LiveTradingWidget
from ui.performance_widget import PerformanceWidget
from utils.logger import get_logger

log = get_logger(__name__, component="main_window")


# ---------------------------------------------------------------------------
# Dark palette factory
# ---------------------------------------------------------------------------

def apply_dark_palette(app: QApplication) -> None:
    """
    Apply a trading-grade dark QPalette to the entire application.

    Must be called BEFORE any QWidget is instantiated so all child widgets
    inherit the palette at construction time.

    Palette is applied to ``app`` (not a specific window) so it propagates
    to all top-level windows including detached QDockWidgets.

    Args:
        app: The QApplication instance (single instance per process).
    """
    app.setStyle("Fusion")  # Required: only Fusion honors QPalette fully

    palette = QPalette()
    C = QColor  # alias

    # Active color group
    palette.setColor(QPalette.ColorRole.Window,          C(20, 20, 20))
    palette.setColor(QPalette.ColorRole.WindowText,      C(218, 218, 218))
    palette.setColor(QPalette.ColorRole.Base,            C(14, 14, 14))
    palette.setColor(QPalette.ColorRole.AlternateBase,   C(26, 26, 26))
    palette.setColor(QPalette.ColorRole.ToolTipBase,     C(35, 35, 35))
    palette.setColor(QPalette.ColorRole.ToolTipText,     C(200, 200, 200))
    palette.setColor(QPalette.ColorRole.Text,            C(218, 218, 218))
    palette.setColor(QPalette.ColorRole.Button,          C(32, 32, 32))
    palette.setColor(QPalette.ColorRole.ButtonText,      C(218, 218, 218))
    palette.setColor(QPalette.ColorRole.BrightText,      C(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Link,            C(42, 140, 230))
    palette.setColor(QPalette.ColorRole.Highlight,       C(0, 112, 200))
    palette.setColor(QPalette.ColorRole.HighlightedText, C(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Mid,             C(40, 40, 40))
    palette.setColor(QPalette.ColorRole.Dark,            C(28, 28, 28))
    palette.setColor(QPalette.ColorRole.Shadow,          C(0, 0, 0))
    palette.setColor(QPalette.ColorRole.Light,           C(55, 55, 55))
    palette.setColor(QPalette.ColorRole.Midlight,        C(44, 44, 44))

    # Disabled color group
    G = QPalette.ColorGroup.Disabled
    palette.setColor(G, QPalette.ColorRole.WindowText,   C(90, 90, 90))
    palette.setColor(G, QPalette.ColorRole.Text,         C(90, 90, 90))
    palette.setColor(G, QPalette.ColorRole.ButtonText,   C(90, 90, 90))
    palette.setColor(G, QPalette.ColorRole.Highlight,    C(50, 50, 50))
    palette.setColor(G, QPalette.ColorRole.Base,         C(18, 18, 18))

    app.setPalette(palette)


# ---------------------------------------------------------------------------
# Placeholder widget for future modules
# ---------------------------------------------------------------------------

class _PlaceholderDockContent(QWidget):
    """
    Minimal placeholder for dock slots reserved for Phase 3 modules.

    Styled to be visually distinct but unobtrusive. The label communicates
    which module will occupy this space, aiding development orientation.
    """

    def __init__(self, module_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(
            "background: #0e0e0e;"
            "border: 1px solid #1e1e1e;"
        )
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        label = QLabel(f"{module_name}\n[Phase 3]", self)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setFont(QFont("Consolas", 9))
        label.setStyleSheet(
            "color: #2a2a2a;"
            "border: none;"
            "background: transparent;"
        )
        label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        layout.addWidget(label)


# ---------------------------------------------------------------------------
# MainWindow
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    """
    Application shell: window, docks, status bar, and shutdown coordination.

    Does NOT own or import the Controller — receives pre-wired widgets.
    This preserves the MVVM separation: the window is a passive shell that
    hosts views, not a coordinator of data flow.

    Signals:
        shutdown_requested — emitted on first close attempt.
                             main.py connects this to its shutdown coroutine.
    """

    shutdown_requested: Signal = Signal()

    def __init__(
        self,
        chart_widget: ChartWidget,
        live_trading_widget: LiveTradingWidget,
        boleta_widget: BoletaWidget,
        performance_widget: PerformanceWidget,
        config: AppConfig | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._chart = chart_widget
        self._live_trading = live_trading_widget
        self._boleta = boleta_widget
        self._performance = performance_widget
        self._cfg = config or AppConfig()
        self._close_accepted = False   # Guards against double-close

        self._setup_window()
        self._setup_docks()
        self._setup_statusbar()

        log.info("MainWindow initialized")

    # ------------------------------------------------------------------
    # Slots (wired by main.py to Controller signals)
    # ------------------------------------------------------------------

    def on_feed_state_changed(self, change: FeedStateChange) -> None:
        """Update status bar indicator when feed lifecycle state changes."""
        _state_style: dict[FeedState, str] = {
            FeedState.IDLE:       "color: #444;",
            FeedState.CONNECTING: "color: #d4881a;",
            FeedState.STREAMING:  "color: #00c864;",
            FeedState.PAUSED:     "color: #d4881a;",
            FeedState.ERROR:      "color: #dc3c3c;",
            FeedState.STOPPED:    "color: #444;",
        }
        style = _state_style.get(change.current, "color: #666;")
        self._lbl_feed_state.setText(f"Feed: {change.current.name}")
        self._lbl_feed_state.setStyleSheet(style)

        if change.reason:
            self.statusBar().showMessage(change.reason, 4000)

    def set_active_symbol(self, symbol: str) -> None:
        """Update window title and chart header when active symbol changes."""
        self.setWindowTitle(f"{self._cfg.window_title}  —  {symbol}")
        self._chart.set_symbol_label(symbol)

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------

    def accept_close(self) -> None:
        """
        Called by main.py's shutdown coroutine AFTER all feeds are stopped.

        Sets the accept flag so closeEvent() does not intercept again,
        then re-triggers window close.
        """
        self._close_accepted = True
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        """
        Override to implement deferred graceful shutdown.

        First close attempt: ignore the event, emit shutdown_requested.
        Subsequent close (after accept_close()): pass through to base class.

        This ensures all asyncio tasks have been awaited before the Qt
        event loop terminates — preventing the 'RuntimeError: Event loop
        is closed' and potential segfaults from dangling C++ objects.
        """
        if self._close_accepted:
            log.info("Close event accepted — window closing")
            super().closeEvent(event)
        else:
            log.info("Close intercepted — initiating graceful shutdown")
            event.ignore()
            self.shutdown_requested.emit()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_window(self) -> None:
        self.setWindowTitle(self._cfg.window_title)
        self.setMinimumSize(
            QSize(self._cfg.window_min_width, self._cfg.window_min_height)
        )
        self.resize(
            QSize(self._cfg.window_default_width, self._cfg.window_default_height)
        )

        # Central widget: the chart
        self.setCentralWidget(self._chart)

        self.setStyleSheet("""
            QMainWindow::separator {
                background: #252525;
                width: 1px;
                height: 1px;
            }
            QDockWidget {
                font-size: 11px;
                color: #888;
            }
            QDockWidget::title {
                background: #161616;
                padding: 5px 8px;
                border-bottom: 1px solid #252525;
                text-align: left;
            }
            QDockWidget::close-button,
            QDockWidget::float-button {
                border: none;
                padding: 1px;
                background: transparent;
            }
        """)

    def _setup_docks(self) -> None:
        """
        Create and register all dock widgets.

        Phase 3: placeholders replaced with real OrderBookWidget
        and BoletaWidget passed via constructor injection.
        """
        _features = (
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )

        # Right dock: Live Trading State
        live_dock = QDockWidget("Live Trading State", self)
        live_dock.setObjectName("dock_live_trading")
        live_dock.setAllowedAreas(
            Qt.DockWidgetArea.RightDockWidgetArea
            | Qt.DockWidgetArea.LeftDockWidgetArea
        )
        live_dock.setFeatures(_features)
        live_dock.setWidget(self._live_trading)
        live_dock.setMinimumWidth(220)
        live_dock.setMaximumWidth(320)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, live_dock)

        # Bottom dock: Boleta / Order Entry
        boleta_dock = QDockWidget("Boleta", self)
        boleta_dock.setObjectName("dock_boleta")
        boleta_dock.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea
            | Qt.DockWidgetArea.TopDockWidgetArea
        )
        boleta_dock.setFeatures(_features)
        boleta_dock.setWidget(self._boleta)
        boleta_dock.setFixedHeight(140)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, boleta_dock)

        # Bottom dock: Live Analytics
        perf_dock = QDockWidget("Live Analytics", self)
        perf_dock.setObjectName("dock_performance")
        perf_dock.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea
            | Qt.DockWidgetArea.TopDockWidgetArea
        )
        perf_dock.setFeatures(_features)
        perf_dock.setWidget(self._performance)
        # Tabify with boleta to save vertical space
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, perf_dock)
        self.tabifyDockWidget(boleta_dock, perf_dock)
        boleta_dock.raise_()

    def _setup_statusbar(self) -> None:
        sb = QStatusBar(self)
        sb.setSizeGripEnabled(False)
        sb.setStyleSheet(
            "QStatusBar {"
            "  background: #141414;"
            "  border-top: 1px solid #252525;"
            "  color: #555;"
            "  font-family: Consolas;"
            "  font-size: 10px;"
            "}"
        )

        self._lbl_feed_state = QLabel("Feed: IDLE")
        self._lbl_feed_state.setStyleSheet("color: #444; padding: 0 8px;")

        self._lbl_version = QLabel("v0.1.0-alpha")
        self._lbl_version.setStyleSheet("color: #2a2a2a; padding: 0 8px;")

        sb.addWidget(self._lbl_feed_state)
        sb.addPermanentWidget(self._lbl_version)
        self.setStatusBar(sb)
