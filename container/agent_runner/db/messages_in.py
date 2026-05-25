from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Optional

from agent_runner.db.connection import open_inbound_db, open_outbound_db


@dataclass
class MessageInRow:
    id: str
    seq: Optional[int]
    kind: str
    timestamp: str
    status: str
    process_after: Optional[str]
    recurrence: Optional[str]
    tries: int
    trigger: int
    platform_id: Optional[str]
    channel_type: Optional[str]
    thread_id: Optional[str]
    content: str
    on_wake: int

def _row_to_dataclass(row) -> MessageInRow:
    def g(name, default=None):
        try:
            return row[name]
        except (IndexError, KeyError):
            return default
    return MessageInRow(
        id=row["id"],
        seq=g("seq"),
        kind=row["kind"],
        timestamp=row["timestamp"],
        status=row["status"],
        process_after=g("process_after"),
        recurrence=g("recurrence"),
        tries=g("tries", 0) or 0,
        trigger=g("trigger", 1) or 0,
        platform_id=g("platform_id"),
        channel_type=g("channel_type"),
        thread_id=g("thread_id"),
        content=row["content"],
        on_wake=g("on_wake", 0) or 0,
    )

def get_pending_messages(
    is_first_poll: bool = False,
    max_count: int = 10,
) -> list[MessageInRow]:
    inbound = open_inbound_db()
    try:
        rows = inbound.execute(
            """
            SELECT * FROM messages_in
             WHERE status = 'pending'
               AND (process_after IS NULL OR datetime(process_after) <= datetime('now'))
               AND (on_wake = 0 OR ?1 = 1)
             ORDER BY seq DESC
             LIMIT ?2
            """,
            (1 if is_first_poll else 0, int(max_count)),
        ).fetchall()
    finally:
        inbound.close()

    if not rows:
        return []

    out = open_outbound_db()
    try:
        ack_rows = out.execute("SELECT message_id FROM processing_ack").fetchall()
    finally:
        out.close()
    acked: set[str] = {r["message_id"] for r in ack_rows}

    surviving = [_row_to_dataclass(r) for r in rows if r["id"] not in acked]
    # DESC 取出最近的 n 条 按时间顺序返回
    surviving.reverse()
    return surviving

def mark_processing(ids: Iterable[str]) -> None:
    ids = list(ids)
    if not ids:
        return
    out = open_outbound_db()
    try:
        out.executemany(
            "INSERT OR REPLACE INTO processing_ack "
            "(message_id, status, status_changed) "
            "VALUES (?, 'processing', datetime('now'))",
            [(i,) for i in ids],
        )
        out.commit()
    finally:
        out.close()


def mark_completed(ids: Iterable[str]) -> None:
    ids = list(ids)
    if not ids:
        return
    out = open_outbound_db()
    try:
        out.executemany(
            "INSERT OR REPLACE INTO processing_ack "
            "(message_id, status, status_changed) "
            "VALUES (?, 'completed', datetime('now'))",
            [(i,) for i in ids],
        )
        out.commit()
    finally:
        out.close()

def mark_failed(message_id: str) -> None:
    out = open_outbound_db()
    try:
        out.execute(
            "INSERT OR REPLACE INTO processing_ack "
            "(message_id, status, status_changed) "
            "VALUES (?, 'failed', datetime('now'))",
            (message_id,),
        )
        out.commit()
    finally:
        out.close()



def find_question_response(question_id: str) -> Optional[MessageInRow]:

    inbound = open_inbound_db()
    try:
        rows = inbound.execute(
            """
            SELECT * FROM messages_in
             WHERE status = 'pending'
               AND kind = 'system'
               AND json_extract(content, '$.question_id') = ?
            """,
            (question_id,),
        ).fetchall()
    finally:
        inbound.close()

    if not rows:
        return None

    out = open_outbound_db()
    try:
        acked = {
            r["message_id"]
            for r in out.execute(
                "SELECT message_id FROM processing_ack"
            ).fetchall()
        }
    finally:
        out.close()

    for r in rows:
        if r["id"] not in acked:
            return _row_to_dataclass(r)
    return None

def parse_content(row: MessageInRow) -> dict:
    """
    如果不是有效 JSON（例如裸文本）包装为
    {"text": <原文>}
    """
    try:
        v = json.loads(row.content)
        if isinstance(v, dict):
            return v
        return {"text": str(v)}
    except (json.JSONDecodeError, TypeError):
        return {"text": row.content or ""}
