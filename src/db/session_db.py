from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable, Literal, Optional

from src.db.schema import INBOUND_SCHEMA, OUTBOUND_SCHEMA


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")


def ensure_schema(db_path: str, schema: Literal["inbound", "outbound"]) -> None:
    sql = INBOUND_SCHEMA if schema == "inbound" else OUTBOUND_SCHEMA
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    try:
        conn.executescript(sql)
        conn.commit()
    finally:
        conn.close()


def open_inbound_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    return conn


def open_outbound_db(path: str, readonly: bool = True) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    return conn


def next_even_seq(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM messages_in").fetchone()
    max_seq = row["m"]
    if max_seq < 2:
        return 2
    # 推进到严格大于 max_seq 的下一个偶数。
    return max_seq + 2 - (max_seq % 2)


def next_odd_seq(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM messages_out").fetchone()
    max_seq = row["m"]
    if max_seq < 1:
        return 1
    if max_seq % 2 == 1:
        return max_seq + 2
    return max_seq + 1



def insert_message(
    db: sqlite3.Connection,
    *,
    id: str,
    kind: str,
    timestamp: str,
    platform_id: Optional[str],
    channel_type: Optional[str],
    thread_id: Optional[str],
    content: str,
    process_after: Optional[str] = None,
    recurrence: Optional[str] = None,
    trigger: int = 1,
    source_session_id: Optional[str] = None,
    on_wake: int = 0,
) -> None:
    seq = next_even_seq(db)
    db.execute(
        """
        INSERT INTO messages_in (
            id, seq, kind, timestamp, status, platform_id, channel_type, thread_id,
            content, process_after, recurrence, series_id, trigger,
            source_session_id, on_wake
        ) VALUES (
            :id, :seq, :kind, :timestamp, 'pending', :platform_id, :channel_type,
            :thread_id, :content, :process_after, :recurrence, :id, :trigger,
            :source_session_id, :on_wake
        )
        """,
        {
            "id": id,
            "seq": seq,
            "kind": kind,
            "timestamp": timestamp,
            "platform_id": platform_id,
            "channel_type": channel_type,
            "thread_id": thread_id,
            "content": content,
            "process_after": process_after,
            "recurrence": recurrence,
            "trigger": trigger,
            "source_session_id": source_session_id,
            "on_wake": on_wake,
        },
    )
    db.commit()

def count_due_messages(db: sqlite3.Connection) -> int:
    row = db.execute(
        """
        SELECT COUNT(*) AS c FROM messages_in
        WHERE status = 'pending'
          AND trigger = 1
          AND (process_after IS NULL OR datetime(process_after) <= datetime('now'))
        """
    ).fetchone()
    return row["c"]

def mark_message_failed(db: sqlite3.Connection, message_id: str) -> None:
    db.execute("UPDATE messages_in SET status = 'failed' WHERE id = ?", (message_id,))
    db.commit()



def retry_with_backoff(db: sqlite3.Connection, message_id: str, backoff_sec: int) -> None:
    if not isinstance(backoff_sec, int) or backoff_sec < 0:
        raise ValueError(f"backoff_sec must be a non-negative int, got {backoff_sec!r}")
    db.execute(
        f"UPDATE messages_in SET tries = tries + 1, "
        f"process_after = datetime('now', '+{backoff_sec} seconds') "
        f"WHERE id = ?",
        (message_id,),
    )
    db.commit()

def get_message_for_retry(
    db: sqlite3.Connection, message_id: str, status: str
) -> Optional[dict]:
    row = db.execute(
        "SELECT id, tries, process_after FROM messages_in WHERE id = ? AND status = ?",
        (message_id, status),
    ).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "tries": row["tries"], "process_after": row["process_after"]}

def get_inbound_source_session_id(
    db: sqlite3.Connection, message_id: str
) -> Optional[str]:
    row = db.execute(
        "SELECT source_session_id FROM messages_in WHERE id = ?", (message_id,)
    ).fetchone()
    if row is None:
        return None
    return row["source_session_id"]


def sync_processing_acks(
    in_db: sqlite3.Connection, out_db: sqlite3.Connection
) -> None:
    completed = out_db.execute(
        "SELECT message_id FROM processing_ack WHERE status IN ('completed', 'failed')"
    ).fetchall()
    if not completed:
        return
    for row in completed:
        in_db.execute(
            "UPDATE messages_in SET status = 'completed' "
            "WHERE id = ? AND status != 'completed'",
            (row["message_id"],),
        )
    in_db.commit()


def get_processing_claims(out_db: sqlite3.Connection) -> list[dict]:
    rows = out_db.execute(
        "SELECT message_id, status_changed FROM processing_ack WHERE status = 'processing'"
    ).fetchall()
    return [{"message_id": r["message_id"], "status_changed": r["status_changed"]} for r in rows]


def delete_orphan_processing_claims(out_db: sqlite3.Connection) -> int:
    cur = out_db.execute("DELETE FROM processing_ack WHERE status = 'processing'")
    out_db.commit()
    return cur.rowcount



@dataclass
class ContainerState:
    current_tool: Optional[str]
    tool_declared_timeout_ms: Optional[int]
    tool_started_at: Optional[str]


def get_container_state(out_db: sqlite3.Connection) -> Optional[dict]:

    try:
        row = out_db.execute(
            "SELECT current_tool, tool_declared_timeout_ms, tool_started_at "
            "FROM container_state WHERE id = 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return {
        "current_tool": row["current_tool"],
        "tool_declared_timeout_ms": row["tool_declared_timeout_ms"],
        "tool_started_at": row["tool_started_at"],
    }



def get_due_outbound_messages(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(
        """
        SELECT id, kind, platform_id, channel_type, thread_id, content, in_reply_to
        FROM messages_out
        WHERE (deliver_after IS NULL OR deliver_after <= datetime('now'))
        ORDER BY timestamp ASC
        """
    ).fetchall()
    return [dict(r) for r in rows]



def get_delivered_ids(db: sqlite3.Connection) -> set[str]:
    rows = db.execute("SELECT message_out_id FROM delivered").fetchall()
    return {r["message_out_id"] for r in rows}


def mark_delivered(
    db: sqlite3.Connection,
    message_out_id: str,
    platform_message_id: Optional[str],
) -> None:
    db.execute(
        "INSERT OR IGNORE INTO delivered "
        "(message_out_id, platform_message_id, status, delivered_at) "
        "VALUES (?, ?, 'delivered', datetime('now'))",
        (message_out_id, platform_message_id),
    )
    db.commit()


def mark_delivery_failed(db: sqlite3.Connection, message_out_id: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO delivered "
        "(message_out_id, platform_message_id, status, delivered_at) "
        "VALUES (?, NULL, 'failed', datetime('now'))",
        (message_out_id,),
    )
    db.commit()


def upsert_session_routing(
    db: sqlite3.Connection,
    channel_type: Optional[str],
    platform_id: Optional[str],
    thread_id: Optional[str],
) -> None:
    db.execute(
        """
        INSERT INTO session_routing (id, channel_type, platform_id, thread_id)
        VALUES (1, :channel_type, :platform_id, :thread_id)
        ON CONFLICT(id) DO UPDATE SET
          channel_type = excluded.channel_type,
          platform_id  = excluded.platform_id,
          thread_id    = excluded.thread_id
        """,
        {"channel_type": channel_type, "platform_id": platform_id, "thread_id": thread_id},
    )
    db.commit()


def replace_destinations(
    db: sqlite3.Connection, entries: Iterable[dict]
) -> None:

    rows = list(entries)
    db.execute("BEGIN")
    try:
        db.execute("DELETE FROM destinations")
        for row in rows:
            db.execute(
                """
                INSERT INTO destinations
                  (name, display_name, type, channel_type, platform_id, agent_group_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    row["name"],
                    row.get("display_name"),
                    row["type"],
                    row.get("channel_type"),
                    row.get("platform_id"),
                    row.get("agent_group_id"),
                ),
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
