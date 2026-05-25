from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Awaitable, Callable, Optional, Protocol, runtime_checkable

from src.channels.adapter import OutboundFile
from src.config import get_config
from src.db.connection import get_db, has_table
from src.db.messaging_groups import get_messaging_group_by_platform
from src.db.session_db import (
    get_delivered_ids,
    get_due_outbound_messages,
    mark_delivered,
    mark_delivery_failed,
)
from src.db.sessions import (
    Session,
    get_active_sessions,
    get_running_sessions,
)
from src.log import log
from src.session_manager import (
    clear_outbox,
    open_inbound_db,
    open_outbound_db,
    read_outbox_files,
)


@runtime_checkable
class DeliveryAdapter(Protocol):

    async def deliver(
        self,
        channel_type: str,
        platform_id: str,
        thread_id: Optional[str],
        kind: str,
        content: str,
        files: Optional[list[OutboundFile]] = None,
    ) -> Optional[str]:
        ...

    async def set_typing(
        self,
        channel_type: str,
        platform_id: str,
        thread_id: Optional[str],
    ) -> None:
        ...

_adapter: Optional[DeliveryAdapter] = None
_adapter_ready_cbs: list[Callable[[DeliveryAdapter], None | Awaitable[None]]] = []

_active_task: Optional[asyncio.Task] = None
_sweep_task: Optional[asyncio.Task] = None


_inflight: set[str] = set()


_delivery_attempts: dict[str, int] = {}



DeliveryActionHandler = Callable[
    [dict, Session, sqlite3.Connection], Awaitable[None]
]

_action_handlers: dict[str, DeliveryActionHandler] = {}


def register_delivery_action(action: str, handler: DeliveryActionHandler) -> None:
    if action in _action_handlers:
        log.warn("delivery_action_handler_overwritten", action=action)
    _action_handlers[action] = handler


def get_delivery_adapter() -> Optional[DeliveryAdapter]:
    return _adapter


def set_delivery_adapter(adapter: DeliveryAdapter) -> None:
    global _adapter
    _adapter = adapter
    for cb in list(_adapter_ready_cbs):
        _fire_adapter_ready(cb, adapter)


def on_delivery_adapter_ready(
    cb: Callable[[DeliveryAdapter], None | Awaitable[None]],
) -> None:
    _adapter_ready_cbs.append(cb)
    if _adapter is not None:
        _fire_adapter_ready(cb, _adapter)

def _fire_adapter_ready(
    cb: Callable[[DeliveryAdapter], None | Awaitable[None]],
    adapter: DeliveryAdapter,
) -> None:
    try:
        result = cb(adapter)
        if asyncio.iscoroutine(result):
            loop = _maybe_loop()
            if loop is not None:
                loop.create_task(result)
            else:

                log.warn(
                    "adapter_ready_async_cb_dropped",
                    reason="no_running_loop",
                )
    except Exception as e:
        log.error("adapter_ready_cb_failed", err=str(e))


def _maybe_loop() -> Optional[asyncio.AbstractEventLoop]:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def start_active_delivery_poll() -> None:
    global _active_task
    if _active_task is not None and not _active_task.done():
        return
    _active_task = asyncio.create_task(_poll_active(), name="delivery-active")


def start_sweep_delivery_poll() -> None:
    global _sweep_task
    if _sweep_task is not None and not _sweep_task.done():
        return
    _sweep_task = asyncio.create_task(_poll_sweep(), name="delivery-sweep")


def stop_delivery_polls() -> None:
    global _active_task, _sweep_task
    for t in (_active_task, _sweep_task):
        if t is not None and not t.done():
            t.cancel()
    _active_task = None
    _sweep_task = None

async def _poll_active() -> None:
    cfg = get_config()
    interval = cfg.active_poll_ms / 1000.0
    while True:
        try:
            for session in get_running_sessions():
                await deliver_session_messages(session)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("delivery_active_poll_error", err=str(e))
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise

async def _poll_sweep() -> None:
    cfg = get_config()
    interval = cfg.sweep_poll_ms / 1000.0
    while True:
        try:
            for session in get_active_sessions():
                await deliver_session_messages(session)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("delivery_sweep_poll_error", err=str(e))
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


async def deliver_session_messages(session: Session) -> None:

    if session.id in _inflight:
        return
    _inflight.add(session.id)
    try:
        await _drain_session(session)
    finally:
        _inflight.discard(session.id)


async def _drain_session(session: Session) -> None:
    cfg = get_config()

    try:
        out_db = open_outbound_db(session.agent_group_id, session.id, readonly=True)
    except sqlite3.OperationalError:
        return

    try:
        try:
            due = get_due_outbound_messages(out_db)
        except sqlite3.OperationalError:
            return
    finally:
        out_db.close()

    if not due:
        return

    try:
        in_db = open_inbound_db(session.agent_group_id, session.id)
    except sqlite3.OperationalError:
        return

    try:
        delivered = get_delivered_ids(in_db)
        undelivered = [m for m in due if m["id"] not in delivered]
        if not undelivered:
            return

        for msg in undelivered:
            try:
                platform_msg_id = await deliver_message(msg, session, in_db)
                mark_delivered(in_db, msg["id"], platform_msg_id)
                _delivery_attempts.pop(msg["id"], None)
            except Exception as e:
                attempts = _delivery_attempts.get(msg["id"], 0) + 1
                _delivery_attempts[msg["id"]] = attempts
                if attempts >= cfg.max_delivery_attempts:
                    log.error(
                        "message_delivery_failed_permanently",
                        message_id=msg["id"], session_id=session.id,
                        attempts=attempts, err=str(e),
                    )
                    mark_delivery_failed(in_db, msg["id"])
                    _delivery_attempts.pop(msg["id"], None)
                else:
                    log.warn(
                        "message_delivery_failed_will_retry",
                        message_id=msg["id"], session_id=session.id,
                        attempt=attempts, max_attempts=cfg.max_delivery_attempts,
                        err=str(e),
                    )
    finally:
        in_db.close()


async def deliver_message(
    msg: dict,
    session: Session,
    in_db: sqlite3.Connection,
) -> Optional[str]:

    adapter = _adapter
    content = _safe_load_content(msg["content"])

    if msg["kind"] == "system":
        await _handle_system_action(content, session, in_db)
        return None

    if adapter is None:
        log.warn("delivery_no_adapter_dropping", message_id=msg["id"])
        return None

    channel_type = msg.get("channel_type")
    platform_id = msg.get("platform_id")
    if not channel_type or not platform_id:
        log.warn(
            "delivery_missing_routing",
            message_id=msg["id"],
            channel_type=channel_type, platform_id=platform_id,
        )
        return None

    mg = get_messaging_group_by_platform(channel_type, platform_id)
    if mg is None:
        raise RuntimeError(
            f"unknown messaging group for {channel_type}/{platform_id} "
            f"(message {msg['id']})"
        )
    is_origin = session.messaging_group_id == mg.id
    if not is_origin and has_table(get_db(), "agent_destinations"):
        row = get_db().execute(
            """
            SELECT 1 FROM agent_destinations
            WHERE agent_group_id = ?
              AND type = 'channel'
              AND channel_type = ?
              AND platform_id = ?
            LIMIT 1
            """,
            (session.agent_group_id, channel_type, platform_id),
        ).fetchone()
        if row is None:
            raise PermissionError(
                f"unauthorized channel destination: "
                f"{session.agent_group_id} cannot send to {channel_type}/{platform_id}"
            )

    if (
        isinstance(content, dict)
        and content.get("type") == "ask_question"
        and content.get("question_id")
        and has_table(get_db(), "pending_questions")
    ):
        _maybe_persist_pending_question(content, msg, session)

    files = None
    if isinstance(content, dict):
        decl = content.get("files")
        if isinstance(decl, list) and decl:
            filenames = [f for f in decl if isinstance(f, str)]
            if filenames:
                files = read_outbox_files(
                    session.agent_group_id, session.id, msg["id"], filenames,
                )

    platform_msg_id = await adapter.deliver(
        channel_type=channel_type,
        platform_id=platform_id,
        thread_id=msg.get("thread_id"),
        kind=msg["kind"],
        content=msg["content"],
        files=files,
    )

    log.info(
        "message_delivered",
        message_id=msg["id"],
        channel_type=channel_type, platform_id=platform_id,
        platform_message_id=platform_msg_id,
        file_count=(len(files) if files else 0),
    )

    clear_outbox(session.agent_group_id, session.id, msg["id"])
    return platform_msg_id


def _safe_load_content(raw: str):
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


async def _handle_system_action(
    content,
    session: Session,
    in_db: sqlite3.Connection,
) -> None:
    if not isinstance(content, dict):
        log.warn(
            "system_action_non_dict_content",
            session_id=session.id, content_type=type(content).__name__,
        )
        return
    action = content.get("action")
    if not isinstance(action, str):
        log.warn("system_action_missing_action", session_id=session.id)
        return

    log.info("system_action_from_agent", session_id=session.id, action=action)
    handler = _action_handlers.get(action)
    if handler is None:
        log.warn("system_action_unknown", action=action, session_id=session.id)
        return
    try:
        await handler(content, session, in_db)
    except Exception as e:
        log.error(
            "system_action_handler_failed",
            action=action, session_id=session.id, err=str(e),
        )
        raise


def _maybe_persist_pending_question(
    content: dict,
    msg: dict,
    session: Session,
) -> None:

    from datetime import datetime, timezone

    title = content.get("title")
    options = content.get("options")
    if not isinstance(title, str) or not isinstance(options, list):
        log.error(
            "ask_question_missing_required_fields",
            question_id=content.get("question_id"),
        )
        return

    try:
        get_db().execute(
            """
            INSERT OR IGNORE INTO pending_questions (
                question_id, session_id, message_out_id,
                platform_id, channel_type, thread_id,
                title, options_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                content["question_id"],
                session.id,
                msg["id"],
                msg.get("platform_id"),
                msg.get("channel_type"),
                msg.get("thread_id"),
                title,
                json.dumps(options),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        get_db().commit()
        log.info(
            "pending_question_created",
            question_id=content["question_id"], session_id=session.id,
        )
    except sqlite3.Error as e:
        log.warn(
            "pending_question_insert_failed",
            question_id=content.get("question_id"), err=str(e),
        )
