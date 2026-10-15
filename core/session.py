"""
core/session.py
---------------
Session persistence for PaperTradingEngine state.

Serializes balance, open position, and trade log to session.json.
Deserializes on startup to restore state across restarts.

Design:
    - Never raises exceptions on load — corrupted/missing files return None
    - Uses json.dump with default=str to handle datetime serialization
    - SessionState is a frozen dataclass (value object)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.paths import DATA_DIR
from utils.logger import get_logger

_log = get_logger(__name__, component="session")

# Session file of this bot instance (repository root unless TRADER_DATA_DIR)
SESSION_PATH = DATA_DIR / "session.json"


@dataclass(frozen=True, slots=True)
class SessionState:
    """Immutable snapshot of the paper trading engine state."""
    balance: float
    position: dict[str, Any] | None
    trade_log: list[dict[str, Any]] = field(default_factory=list)
    stale_position: bool = False
    meta: dict[str, Any] = field(default_factory=dict)  # e.g. strategy_params


def save_session(
    balance: float,
    open_position: dict[str, Any] | None,
    trade_log: list[dict[str, Any]] | None = None,
    meta: dict[str, Any] | None = None,
    path: Path | None = None,
) -> None:
    """
    Persist the current PaperTradingEngine state to disk.

    Args:
        balance: Current cash balance.
        open_position: Serialized position dict, or None if flat.
        trade_log: List of serialized trade records.
        meta: Extra info about the run (e.g. strategy parameters).
        path: session file of this bot (default: SESSION_PATH).
    """
    path = path or SESSION_PATH
    payload = {
        "balance": balance,
        "open_position": open_position,
        "trade_log": trade_log or [],
        "meta": meta or {},
    }
    try:
        # Atomic write: write to temp file, then rename.
        # Prevents corruption if process dies mid-write (Codespace shutdown).
        tmp_path = path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(path)
        _log.info("Session saved", extra={"path": str(path)})
    except (OSError, TypeError) as e:
        _log.error(f"Failed to save session: {e}")


def load_session(timeframe_seconds: int = 60, path: Path | None = None) -> SessionState | None:
    """
    Load a previously saved session from disk.

    Returns:
        SessionState if file exists and is valid, None otherwise.
        Never raises — logs warning on any error.
    """
    path = path or SESSION_PATH
    if not path.exists():
        _log.info("No session file found — starting fresh")
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        balance = float(data["balance"])
        position = data.get("open_position")
        trade_log = data.get("trade_log", [])
        meta = data.get("meta") or {}

        stale_position = False
        if position is not None and position.get("side") != "FLAT":
            entry_iso = position.get("entry_time_iso", "")
            if entry_iso:
                try:
                    entry_time = datetime.fromisoformat(entry_iso)
                    age_seconds = (datetime.now(timezone.utc) - entry_time).total_seconds()
                    if age_seconds > 2 * timeframe_seconds:
                        stale_position = True
                        _log.warning(f"Session position is stale (age: {age_seconds:.1f}s)")
                except ValueError:
                    pass

        _log.info(
            "Session loaded",
            extra={
                "balance": round(balance, 2),
                "has_position": position is not None and position.get("side") != "FLAT",
                "trade_count": len(trade_log),
                "stale_position": stale_position,
            },
        )
        return SessionState(
            balance=balance,
            position=position,
            trade_log=trade_log,
            stale_position=stale_position,
            meta=meta,
        )

    except Exception as e:
        # Keep the unreadable file for inspection instead of letting the next
        # save overwrite it with a fresh session
        _log.error(f"Session file unreadable ({e!r}) — moved aside")
        _move_aside(path)
        return None


def _move_aside(path: Path) -> None:
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path.replace(path.with_name(f"{path.stem}.corrupt-{stamp}{path.suffix}"))
    except OSError as e:
        _log.error(f"Could not move {path.name} aside: {e}")
