"""
core/paths.py
-------------
Where a bot instance keeps its state.

One repository can run several paper bots, one per coin (scripts/
install_coin.sh). Each instance sets TRADER_DATA_DIR to its own folder
(bots/eth, bots/sol, ...) for session.json, live_bars.csv, session.lock and
archive/. Without it — the original BTC bot — everything stays at the
repository root, exactly as before.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _data_dir() -> Path:
    raw = os.environ.get("TRADER_DATA_DIR", "").strip()
    if not raw:
        return ROOT
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


DATA_DIR = _data_dir()
