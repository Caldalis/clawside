from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from src.db.session_db import next_even_seq
from src.log import log


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def insert_task_message(
    in_db: sqlite3.Connection,
    task_id: str,
    prompt: str,
    process_after: str,
    recurrence: Optional[str],
    platform_id: Optional[str],
    channel_type: Optional[str],
    thread_id: Optional[str],
    script: Optional[str] = None,
) -> None:
    seq = next_even_seq(in_db)
    content = json.dumps({
        "prompt": prompt,
        "script": script,
        "task_id": task_id,
    })
    in_db.execute(
        """
        INSERT INTO messages_in (
          id, seq, kind, timestamp, status, platform_id, channel_type,
          thread_id, content, process_after, recurrence, series_id, trigger,
          source_session_id, on_wake
        ) VALUES (
          :id, :seq, 'task', :timestamp, 'pending', :platform_id, :channel_type,
          :thread_id, :content, :process_after, :recurrence, :id, 1,
          NULL, 0
        )
        """,
        {
            "id": task_id,
            "seq": seq,
            "timestamp": _now_iso(),
            "platform_id": platform_id,
            "channel_type": channel_type,
            "thread_id": thread_id,
            "content": content,
            "process_after": process_after,
            "recurrence": recurrence,
        },
    )
    in_db.commit()
    log.info(
        "task_scheduled",
        task_id=task_id, process_after=process_after, recurrence=recurrence,
    )



def cancel_task(in_db: sqlite3.Connection, task_id: str) -> None:
    cur = in_db.execute(
        "UPDATE messages_in SET status = 'cancelled' "
        "WHERE id = ? AND status = 'pending'",
        (task_id,),
    )
    in_db.commit()
    log.info("task_cancelled", task_id=task_id, rows=cur.rowcount)


def pause_task(in_db: sqlite3.Connection, task_id: str) -> None:
    cur = in_db.execute(
        "UPDATE messages_in SET status = 'paused' "
        "WHERE id = ? AND status = 'pending'",
        (task_id,),
    )
    in_db.commit()
    log.info("task_paused", task_id=task_id, rows=cur.rowcount)


def resume_task(in_db: sqlite3.Connection, task_id: str) -> None:
    cur = in_db.execute(
        "UPDATE messages_in SET status = 'pending' "
        "WHERE id = ? AND status = 'paused'",
        (task_id,),
    )
    in_db.commit()
    log.info("task_resumed", task_id=task_id, rows=cur.rowcount)



_UPDATABLE_FIELDS = {"process_after", "recurrence"}


def update_task(in_db: sqlite3.Connection, task_id: str, **kwargs: object) -> None:
    bad = [k for k in kwargs if k not in _UPDATABLE_FIELDS]
    if bad:
        raise ValueError(
            f"update_task: unsupported fields {bad!r}; "
            f"allowed = {sorted(_UPDATABLE_FIELDS)}"
        )
    if not kwargs:
        return

    set_clauses = []
    values: dict = {"task_id": task_id}
    for k, v in kwargs.items():
        set_clauses.append(f"{k} = :{k}")
        values[k] = v
    sql = f"UPDATE messages_in SET {', '.join(set_clauses)} WHERE id = :task_id"
    cur = in_db.execute(sql, values)
    in_db.commit()
    log.info("task_updated", task_id=task_id, fields=list(kwargs.keys()), rows=cur.rowcount)
