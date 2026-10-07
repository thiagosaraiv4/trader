"""
utils/logger.py
---------------
Structured logging infrastructure.

Two output modes:
- Console (TTY): colored, human-readable format
- File: JSON-structured for log aggregation / ELK / Datadog

Factory functions are idempotent — safe to call from multiple modules.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

class StructuredJsonFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object."""

    _EXTRA_KEYS = (
        "component", "symbol", "timeframe", "provider",
        "bars", "exit_code", "feed_count", "feed_index",
        "from", "to",
    )

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Merge extra fields (from LoggerAdapter or extra={})
        for key in self._EXTRA_KEYS:
            val = getattr(record, key, None)
            if val is not None:
                log_entry[key] = val

        if record.exc_info and record.exc_info[1] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, default=str)


class ColoredConsoleFormatter(logging.Formatter):
    """Human-readable colored output for interactive development."""

    _COLORS: dict[str, str] = {
        "DEBUG":    "\033[36m",    # Cyan
        "INFO":     "\033[32m",    # Green
        "WARNING":  "\033[33m",    # Yellow
        "ERROR":    "\033[31m",    # Red
        "CRITICAL": "\033[1;31m",  # Bold Red
    }
    _RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self._COLORS.get(record.levelname, "")
        reset = self._RESET

        # Extract component if present
        component = getattr(record, "component", "")
        comp_str = f"[{component}] " if component else ""

        # Collect extra fields
        extras: dict[str, Any] = {}
        for key in ("symbol", "timeframe", "provider", "bars",
                     "exit_code", "feed_count", "feed_index",
                     "from", "to"):
            val = getattr(record, key, None)
            if val is not None:
                extras[key] = val

        extra_str = ""
        if extras:
            pairs = " ".join(f"{k}={v}" for k, v in extras.items())
            extra_str = f" | {pairs}"

        msg = record.getMessage()
        formatted = (
            f"{color}{record.levelname:<8}{reset} "
            f"{comp_str}{msg}{extra_str}"
        )

        if record.exc_info and record.exc_info[1] is not None:
            formatted += "\n" + self.formatException(record.exc_info)

        return formatted


# ---------------------------------------------------------------------------
# Configuration (idempotent)
# ---------------------------------------------------------------------------

_configured: bool = False


def configure_logging(
    level: int = logging.DEBUG,
    log_file: str | None = None,
) -> None:
    """
    Configure the root logger with console + optional file handlers.

    Idempotent: subsequent calls are no-ops. This prevents duplicate
    handlers when multiple modules call configure_logging() defensively.
    """
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger()
    root.setLevel(level)

    # Console handler (always)
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(level)
    console_handler.setFormatter(ColoredConsoleFormatter())
    root.addHandler(console_handler)

    # File handler (optional)
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(StructuredJsonFormatter())
        root.addHandler(file_handler)


# ---------------------------------------------------------------------------
# Logger factory
# ---------------------------------------------------------------------------

class _ContextAdapter(logging.LoggerAdapter):
    """
    LoggerAdapter that merges per-call extra={...} with the bound context.

    The stdlib adapter replaces per-call extras with the bound context,
    silently dropping fields such as the exchange error message.
    """

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        kwargs["extra"] = {**self.extra, **(kwargs.get("extra") or {})}
        return msg, kwargs


def get_logger(name: str, **context: Any) -> logging.LoggerAdapter:
    """
    Return a LoggerAdapter with bound context fields.

    Usage:
        log = get_logger(__name__, component="feed", symbol="BTCUSDT")
        log.info("Connected")  # → "[feed] Connected | symbol=BTCUSDT"
    """
    logger = logging.getLogger(name)
    return _ContextAdapter(logger, extra=context)
