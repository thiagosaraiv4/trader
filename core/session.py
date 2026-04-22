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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.logger import get_logger

_log = get_logger(__name__, component="session")

# Session file lives next to the application root
_SESSION_PATH = Path(__file__).resolve().parent.parent / "session.json"


@dataclass(frozen=True, slots=True)
class SessionState:
    """Immutable snapshot of the paper trading engine state."""
    balance: float
    position: dict[str, Any] | None
    trade_log: list[dict[str, Any]] = field(default_factory=list)
    stale_position: bool = False


def save_session(
    balance: float,
    open_position: dict[str, Any] | None,
    trade_log: list[dict[str, Any]] | None = None,
) -> None:
    """
    Persist the current PaperTradingEngine state to disk.

    Args:
        balance: Current cash balance.
        open_position: Serialized position dict, or None if flat.
        trade_log: List of serialized trade records.
    """
    payload = {
        "balance": balance,
        "open_position": open_position,
        "trade_log": trade_log or [],
    }
    try:
        with open(_SESSION_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
        _log.info("Session saved", extra={"path": str(_SESSION_PATH)})
    except (OSError, TypeError) as e:
        _log.error(f"Failed to save session: {e}")


def load_session(timeframe_seconds: int = 60) -> SessionState | None:
    """
    Load a previously saved session from disk.

    Returns:
        SessionState if file exists and is valid, None otherwise.
        Never raises — logs warning on any error.
    """
    if not _SESSION_PATH.exists():
        _log.info("No session file found — starting fresh")
        return None

    try:
        with open(_SESSION_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        balance = float(data["balance"])
        position = data.get("open_position")
        trade_log = data.get("trade_log", [])

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
        )

    except json.JSONDecodeError as e:
        _log.warning(f"Session file corrupted (invalid JSON): {e}")
        return None
    except KeyError as e:
        _log.warning(f"Session file missing required key: {e}")
        return None
    except Exception as e:
        _log.warning(f"Unexpected error loading session: {e}")
        return None
