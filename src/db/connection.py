from __future__ import annotations

import os
import sqlite3
from typing import Optional

_db: Optional[sqlite3.Connection] = None


def init_db(path: str) -> sqlite3.Connection:

    global _db
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    _db = conn
    return conn

def get_db() -> sqlite3.Connection:
    if _db is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _db

def close_db() -> None:
    global _db
    if _db is not None:
        _db.close()
        _db = None



def has_table(db: sqlite3.Connection, name: str) -> bool:

    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None
