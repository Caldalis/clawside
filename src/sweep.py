from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional, Union

from src.config import get_config
from src.container_runner import (
    is_container_running,
    kill_container,
    wake_container,
)
from src.db.agent_groups import get_agent_group
from src.db.connection import get_db
from src.db.session_db import (
    count_due_messages,
    delete_orphan_processing_claims,
    get_container_state,
    get_message_for_retry,
    get_processing_claims,
    mark_message_failed,
    retry_with_backoff,
    sync_processing_acks,
)
from src.db.sessions import Session, get_active_sessions
from src.log import log
from src.session_manager import (
    heartbeat_path,
    inbound_db_path,
    open_inbound_db,
    open_outbound_db,
)


ABSOLUTE_CEILING_MS = 30 * 60 * 1000
CLAIM_STUCK_MS = 60_000
MAX_TRIES = 5
BACKOFF_BASE_MS = 5_000

_TZ_SUFFIX_RE = re.compile(r"([zZ]|[+\-]\d{2}:?\d{2})$")


def parse_sqlite_utc(s: str) -> float:

    from datetime import datetime, timezone

    if not isinstance(s, str) or not s:
        return float("nan")
    text = s.strip()
    if not _TZ_SUFFIX_RE.search(text):
        text = text + "Z"

    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%SZ").replace(
                tzinfo=timezone.utc,
            )
        except ValueError:
            return float("nan")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000.0


@dataclass(frozen=True)
class StuckOk:
    action: str = "ok"


@dataclass(frozen=True)
class StuckKillCeiling:
    heartbeat_age_ms: float
    ceiling_ms: float
    action: str = "kill-ceiling"


@dataclass(frozen=True)
class StuckKillClaim:
    message_id: str
    claim_age_ms: float
    tolerance_ms: float
    action: str = "kill-claim"

StuckDecision = Union[StuckOk, StuckKillCeiling, StuckKillClaim]


def _bash_timeout_ms(container_state: Optional[dict]) -> Optional[int]:

    if not container_state:
        return None
    if container_state.get("current_tool") != "Bash":
        return None
    val = container_state.get("tool_declared_timeout_ms")
    return val if isinstance(val, int) else None


def decide_stuck_action(
    *,
    now: float,
    heartbeat_mtime_ms: float,
    container_state: Optional[dict],
    claims: list[dict],
) -> StuckDecision:

    declared_bash_ms = _bash_timeout_ms(container_state)

    if heartbeat_mtime_ms != 0:
        heartbeat_age = now - heartbeat_mtime_ms
        ceiling = max(ABSOLUTE_CEILING_MS, declared_bash_ms or 0)
        if heartbeat_age > ceiling:
            return StuckKillCeiling(heartbeat_age_ms=heartbeat_age, ceiling_ms=ceiling)

    tolerance = max(CLAIM_STUCK_MS, declared_bash_ms or 0)
    for claim in claims:
        ts = claim.get("status_changed")
        if not isinstance(ts, str):
            continue
        claimed_at = parse_sqlite_utc(ts)
        if claimed_at != claimed_at:   # NaN 检测
            continue
        claim_age = now - claimed_at
        if claim_age <= tolerance:
            continue
        if heartbeat_mtime_ms > claimed_at:
            continue
        msg_id = claim.get("message_id", "")
        if not isinstance(msg_id, str):
            msg_id = str(msg_id)
        return StuckKillClaim(
            message_id=msg_id,
            claim_age_ms=claim_age,
            tolerance_ms=tolerance,
        )

    return StuckOk()


_sweep_task: Optional[asyncio.Task] = None


def start_host_sweep() -> None:
    global _sweep_task
    if _sweep_task is not None and not _sweep_task.done():
        return
    _sweep_task = asyncio.create_task(_run_sweep(), name="host-sweep")


def stop_host_sweep() -> None:

    global _sweep_task
    if _sweep_task is not None and not _sweep_task.done():
        _sweep_task.cancel()
    _sweep_task = None


# 用户从不点选的 ask_user_question 会留下孤儿 pending_questions 行。每次 sweep
# 清理远超任何合理问题超时的陈旧行（ask_user_question 默认超时 300s，这里留 24h 余量，
# 不会误删仍在等待回答的问题）。
PENDING_QUESTION_TTL_SEC = 24 * 3600

def _cleanup_pending_questions() -> None:
    try:
        db = get_db()
        cur = db.execute(
            "DELETE FROM pending_questions "
            "WHERE datetime(created_at) < datetime('now', ?)",
            (f"-{PENDING_QUESTION_TTL_SEC} seconds",),
        )
        db.commit()
        if cur.rowcount > 0:
            log.info("sweep_cleaned_pending_questions", count=cur.rowcount)
    except sqlite3.Error as e:
        log.warn("sweep_cleanup_pending_questions_failed", err=str(e))

async def _run_sweep() -> None:
    cfg = get_config()
    interval = cfg.sweep_poll_ms / 1000.0
    while True:
        try:
            _cleanup_pending_questions()
            for session in get_active_sessions():
                await sweep_session(session)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("host_sweep_error", err=str(e))
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise

async def sweep_session(session: Session) -> None:
    agent_group = get_agent_group(session.agent_group_id)
    if agent_group is None:
        return

    in_path = inbound_db_path(agent_group.id, session.id)
    if not os.path.exists(in_path):
        return

    try:
        in_db = open_inbound_db(agent_group.id, session.id)
    except sqlite3.OperationalError:
        return

    out_db: Optional[sqlite3.Connection] = None
    try:
        try:
            out_db = open_outbound_db(agent_group.id, session.id, readonly=True)
        except sqlite3.OperationalError:
            out_db = None

        if out_db is not None:
            sync_processing_acks(in_db, out_db)


        due = count_due_messages(in_db)
        if due > 0 and not is_container_running(session.id):
            log.info(
                "sweep_waking_container",
                session_id=session.id, due=due,
            )

            await wake_container(session)

        alive = is_container_running(session.id)

        if alive and out_db is not None:
            await _enforce_running_container_sla(in_db, out_db, session)

        if not alive and out_db is not None:
            _reset_stuck_processing_rows(
                in_db, out_db, session, "container not running",
            )

        await _maybe_handle_recurrence(in_db, session)
    finally:
        in_db.close()
        if out_db is not None:
            out_db.close()


async def _maybe_handle_recurrence(
    in_db: sqlite3.Connection, session: Session,
) -> None:

    try:
        from src.modules.scheduling.recurrence import handle_recurrence  # type: ignore
    except ImportError:
        return
    try:
        result = handle_recurrence(in_db, session)
        if asyncio.iscoroutine(result):
            await result
    except Exception as e:
        log.error("recurrence_handler_failed", session_id=session.id, err=str(e))



def _heartbeat_mtime_ms(agent_group_id: str, session_id: str) -> float:

    try:
        st = os.stat(heartbeat_path(agent_group_id, session_id))
    except (FileNotFoundError, OSError):
        return 0.0
    return st.st_mtime * 1000.0


async def _enforce_running_container_sla(
    in_db: sqlite3.Connection,
    out_db: sqlite3.Connection,
    session: Session,
) -> None:
    decision = decide_stuck_action(
        now=time.time() * 1000.0,
        heartbeat_mtime_ms=_heartbeat_mtime_ms(session.agent_group_id, session.id),
        container_state=get_container_state(out_db),
        claims=get_processing_claims(out_db),
    )

    if isinstance(decision, StuckOk):
        return

    if isinstance(decision, StuckKillCeiling):
        log.warn(
            "sweep_kill_ceiling",
            session_id=session.id,
            heartbeat_age_ms=decision.heartbeat_age_ms,
            ceiling_ms=decision.ceiling_ms,
        )
        await kill_container(session.id, "absolute-ceiling")
        _reset_stuck_processing_rows(in_db, out_db, session, "absolute-ceiling")
        return

    # StuckKillClaim
    log.warn(
        "sweep_kill_claim_stuck",
        session_id=session.id,
        message_id=decision.message_id,
        claim_age_ms=decision.claim_age_ms,
        tolerance_ms=decision.tolerance_ms,
    )
    await kill_container(session.id, "claim-stuck")
    _reset_stuck_processing_rows(in_db, out_db, session, "claim-stuck")


def _reset_stuck_processing_rows(
    in_db: sqlite3.Connection,
    out_db: sqlite3.Connection,
    session: Session,
    reason: str,
) -> None:
    cfg = get_config()
    claims = get_processing_claims(out_db)
    now_ms = time.time() * 1000.0

    for claim in claims:
        mid = claim.get("message_id")
        if not isinstance(mid, str):
            continue
        msg = get_message_for_retry(in_db, mid, "pending")
        if msg is None:
            continue

        pa = msg.get("process_after")
        if isinstance(pa, str) and pa:
            pa_ms = parse_sqlite_utc(pa)
            if pa_ms == pa_ms and pa_ms > now_ms:
                continue

        tries = msg.get("tries", 0) or 0
        if tries >= cfg.max_tries:
            mark_message_failed(in_db, mid)
            log.warn(
                "sweep_message_failed_max_retries",
                message_id=mid, session_id=session.id, reason=reason,
            )
            continue

        backoff_ms = cfg.backoff_base_ms * (2 ** tries)
        backoff_sec = max(1, int(backoff_ms // 1000))
        retry_with_backoff(in_db, mid, backoff_sec)
        log.info(
            "sweep_reset_stale_message",
            message_id=mid,
            tries=tries,
            backoff_ms=backoff_ms,
            reason=reason,
        )

    try:
        rw = open_outbound_db(session.agent_group_id, session.id, readonly=False)
    except sqlite3.OperationalError as e:
        log.warn(
            "sweep_orphan_cleanup_open_failed",
            session_id=session.id, err=str(e),
        )
        return
    try:
        cleared = delete_orphan_processing_claims(rw)
        if cleared > 0:
            log.info(
                "sweep_cleared_orphan_claims",
                session_id=session.id, cleared=cleared, reason=reason,
            )
    except sqlite3.Error as e:
        log.warn(
            "sweep_orphan_cleanup_failed",
            session_id=session.id, err=str(e),
        )
    finally:
        rw.close()
