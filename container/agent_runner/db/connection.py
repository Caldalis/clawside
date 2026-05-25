from __future__ import annotations

import os
import sqlite3
from pathlib import Path


INBOUND_PATH = "/workspace/inbound.db"
OUTBOUND_PATH = "/workspace/outbound.db"
HEARTBEAT_PATH = "/workspace/.heartbeat"

def _inbound_path() -> str:
    return os.environ.get("CLAWSIDE_INBOUND_DB", INBOUND_PATH)

def _outbound_path() -> str:
    return os.environ.get("CLAWSIDE_OUTBOUND_DB", OUTBOUND_PATH)

def _heartbeat_path() -> str:
    return os.environ.get("CLAWSIDE_HEARTBEAT_PATH", HEARTBEAT_PATH)

def open_inbound_db() -> sqlite3.Connection:
    path = _inbound_path()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA mmap_size=0")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def open_outbound_db() -> sqlite3.Connection:
    path = _outbound_path()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def touch_heartbeat() -> None:
    p = _heartbeat_path()
    try:
        os.utime(p, None)
    except FileNotFoundError:
        try:
            Path(p).touch()
        except OSError:
            pass
    except OSError:
        pass

def clear_stale_processing_acks() -> None:
    """删除前一个崩溃容器残留的 processing 行
    """
    conn = open_outbound_db()
    try:
        conn.execute("DELETE FROM processing_ack WHERE status = 'processing'")
        conn.commit()
    finally:
        conn.close()
