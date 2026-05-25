from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Optional

from src.channels.adapter import get_adapter
from src.log import log
from src.session_manager import heartbeat_path
from src.db.sessions import get_session


TYPING_REFRESH_MS = 4000
TYPING_GRACE_MS = 15000
HEARTBEAT_FRESH_MS = 6000


@dataclass
class _Refresher:
    session_id: str
    channel_type: str
    platform_id: str
    thread_id: Optional[str]
    task: asyncio.Task
    started_at_ms: float


_refreshers: dict[str, _Refresher] = {}


def _now_ms() -> float:
    import time
    return time.time() * 1000.0


def _heartbeat_fresh(agent_group_id: str, session_id: str) -> bool:
    try:
        st = os.stat(heartbeat_path(agent_group_id, session_id))
    except (FileNotFoundError, OSError):
        return False
    return (_now_ms() - st.st_mtime * 1000.0) < HEARTBEAT_FRESH_MS


async def _trigger(channel_type: str, platform_id: str, thread_id: Optional[str]) -> None:
    adapter = get_adapter(channel_type)
    if adapter is None:
        return
    try:
        await adapter.set_typing(platform_id, thread_id)
    except Exception:
        pass


async def _refresh_loop(session_id: str) -> None:
    interval = TYPING_REFRESH_MS / 1000.0
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return

        entry = _refreshers.get(session_id)
        if entry is None:
            return

        within_grace = (_now_ms() - entry.started_at_ms) < TYPING_GRACE_MS

        sess = get_session(session_id)
        fresh = (
            sess is not None
            and _heartbeat_fresh(sess.agent_group_id, session_id)
        )

        if within_grace or fresh:
            await _trigger(entry.channel_type, entry.platform_id, entry.thread_id)
            continue

        _refreshers.pop(session_id, None)
        return


def start_typing_refresh(
    session_id: str,
    channel_type: str,
    platform_id: str,
    thread_id: Optional[str],
) -> None:
    existing = _refreshers.get(session_id)
    if existing is not None:
        existing.started_at_ms = _now_ms()
        existing.channel_type = channel_type
        existing.platform_id = platform_id
        existing.thread_id = thread_id
        asyncio.create_task(_trigger(channel_type, platform_id, thread_id))
        return

    asyncio.create_task(_trigger(channel_type, platform_id, thread_id))
    task = asyncio.create_task(_refresh_loop(session_id), name=f"typing:{session_id}")
    _refreshers[session_id] = _Refresher(
        session_id=session_id,
        channel_type=channel_type,
        platform_id=platform_id,
        thread_id=thread_id,
        task=task,
        started_at_ms=_now_ms(),
    )

def stop_typing_refresh(session_id: str) -> None:
    entry = _refreshers.pop(session_id, None)
    if entry is None:
        return
    if not entry.task.done():
        entry.task.cancel()

def stop_all_typing_refreshers() -> None:
    for sid in list(_refreshers.keys()):
        stop_typing_refresh(sid)
