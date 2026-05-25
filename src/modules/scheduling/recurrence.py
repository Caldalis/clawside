from __future__ import annotations

import json
import os
import random
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from src.db.sessions import Session
from src.db.session_db import next_even_seq
from src.log import log


def _gen_id(prefix: str = "task") -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=6))}"


def _tz() -> ZoneInfo:
    name = os.environ.get("TZ") or "UTC"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def handle_recurrence(in_db: sqlite3.Connection, session: Session) -> None:

    try:
        from croniter import croniter  # type: ignore
    except ImportError:
        # croniter 是 pyproject 依赖，但仍做防御。
        log.warn("croniter_not_installed_skipping_recurrence", session_id=session.id)
        return

    completed = in_db.execute(
        """
        SELECT id, series_id, recurrence, content, platform_id, channel_type,
               thread_id
          FROM messages_in
         WHERE kind = 'task'
           AND status = 'completed'
           AND recurrence IS NOT NULL
        """
    ).fetchall()
    if not completed:
        return

    now_utc = datetime.now(timezone.utc)
    user_tz = _tz()

    for row in completed:
        series_id = row["series_id"] or row["id"]
        recurrence = row["recurrence"]


        already = in_db.execute(
            """
            SELECT 1 FROM messages_in
             WHERE series_id = ?
               AND kind = 'task'
               AND status IN ('pending', 'paused')
             LIMIT 1
            """,
            (series_id,),
        ).fetchone()
        if already is not None:
            continue

        try:
            base = now_utc.astimezone(user_tz)
            it = croniter(recurrence, base)
            next_local = it.get_next(datetime)
            next_utc_iso = next_local.astimezone(timezone.utc).isoformat()
        except Exception as e:
            log.warn(
                "recurrence_invalid_cron",
                series_id=series_id, recurrence=recurrence, err=str(e),
            )
            continue

        new_id = _gen_id()
        try:
            content_dict = json.loads(row["content"])
            if not isinstance(content_dict, dict):
                content_dict = {"prompt": str(content_dict)}
        except (TypeError, ValueError):
            content_dict = {"prompt": ""}
        content_dict["task_id"] = new_id

        seq = next_even_seq(in_db)
        in_db.execute(
            """
            INSERT INTO messages_in (
              id, seq, kind, timestamp, status, platform_id, channel_type,
              thread_id, content, process_after, recurrence, series_id,
              trigger, source_session_id, on_wake
            ) VALUES (
              :id, :seq, 'task', :timestamp, 'pending', :platform_id,
              :channel_type, :thread_id, :content, :process_after,
              :recurrence, :series_id, 1, NULL, 0
            )
            """,
            {
                "id": new_id,
                "seq": seq,
                "timestamp": now_utc.isoformat(),
                "platform_id": row["platform_id"],
                "channel_type": row["channel_type"],
                "thread_id": row["thread_id"],
                "content": json.dumps(content_dict),
                "process_after": next_utc_iso,
                "recurrence": recurrence,
                "series_id": series_id,
            },
        )
        in_db.commit()
        log.info(
            "recurrence_next_inserted",
            series_id=series_id, new_id=new_id,
            next=next_utc_iso, recurrence=recurrence,
        )
