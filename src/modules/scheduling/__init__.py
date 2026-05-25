from __future__ import annotations

import sqlite3

from src.db.sessions import Session
from src.delivery import register_delivery_action
from src.log import log
from src.modules.scheduling import db as sched_db


async def _handle_schedule(content: dict, session: Session, in_db: sqlite3.Connection) -> None:
    task_id = content.get("task_id")
    prompt = content.get("prompt")
    process_after = content.get("process_after")
    if not isinstance(task_id, str) or not isinstance(prompt, str) or not isinstance(process_after, str):
        log.warn("schedule_task_missing_required_fields", session_id=session.id, content=content)
        return
    sched_db.insert_task_message(
        in_db,
        task_id=task_id,
        prompt=prompt,
        process_after=process_after,
        recurrence=content.get("recurrence"),
        platform_id=content.get("platform_id"),
        channel_type=content.get("channel_type"),
        thread_id=content.get("thread_id"),
        script=content.get("script"),
    )

async def _handle_cancel(content: dict, session: Session, in_db: sqlite3.Connection) -> None:
    task_id = content.get("task_id")
    if not isinstance(task_id, str):
        log.warn("cancel_task_missing_task_id", session_id=session.id)
        return
    sched_db.cancel_task(in_db, task_id)

async def _handle_pause(content: dict, session: Session, in_db: sqlite3.Connection) -> None:
    task_id = content.get("task_id")
    if not isinstance(task_id, str):
        log.warn("pause_task_missing_task_id", session_id=session.id)
        return
    sched_db.pause_task(in_db, task_id)


async def _handle_resume(content: dict, session: Session, in_db: sqlite3.Connection) -> None:
    task_id = content.get("task_id")
    if not isinstance(task_id, str):
        log.warn("resume_task_missing_task_id", session_id=session.id)
        return
    sched_db.resume_task(in_db, task_id)


async def _handle_update(content: dict, session: Session, in_db: sqlite3.Connection) -> None:
    task_id = content.get("task_id")
    if not isinstance(task_id, str):
        log.warn("update_task_missing_task_id", session_id=session.id)
        return
    updates = {
        k: v for k, v in content.items()
        if k in ("process_after", "recurrence") and v is not None
    }
    if not updates:
        return
    try:
        sched_db.update_task(in_db, task_id, **updates)
    except ValueError as e:
        log.warn("update_task_rejected", session_id=session.id, err=str(e))



def register_all() -> None:
    register_delivery_action("schedule_task", _handle_schedule)
    register_delivery_action("cancel_task", _handle_cancel)
    register_delivery_action("pause_task", _handle_pause)
    register_delivery_action("resume_task", _handle_resume)
    register_delivery_action("update_task", _handle_update)
    log.info("scheduling_delivery_actions_registered")
