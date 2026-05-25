from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from agent_runner.db.connection import open_outbound_db


HISTORY_KEY = "history"


def get_value(key: str) -> Optional[str]:
    db = open_outbound_db()
    try:
        row = db.execute(
            "SELECT value FROM session_state WHERE key = ?", (key,)
        ).fetchone()
    finally:
        db.close()
    return row["value"] if row else None

def set_value(key: str, value: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    db = open_outbound_db()
    try:
        db.execute(
            "INSERT OR REPLACE INTO session_state (key, value, updated_at) "
            "VALUES (?, ?, ?)",
            (key, value, now),
        )
        db.commit()
    finally:
        db.close()

def clear_value(key: str) -> None:
    db = open_outbound_db()
    try:
        db.execute("DELETE FROM session_state WHERE key = ?", (key,))
        db.commit()
    finally:
        db.close()

def get_history() -> list[dict]:
    """
    返回持久化的历史列表，缺失/不可读时返回 []
    """
    raw = get_value(HISTORY_KEY)
    if raw is None:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return data


def set_history(history: list[dict]) -> None:
    if not isinstance(history, list):
        raise TypeError(f"history must be a list, got {type(history).__name__}")
    set_value(HISTORY_KEY, json.dumps(history))
