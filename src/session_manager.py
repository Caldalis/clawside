from __future__ import annotations

import json
import os
import random
import sqlite3
import time
from base64 import b64decode
from datetime import datetime, timezone
from typing import Optional

from src.channels.adapter import OutboundFile
from src.config import get_config
from src.db.messaging_groups import get_messaging_group
from src.db.session_db import (
    ensure_schema,
    insert_message,
    open_inbound_db as _open_inbound_raw,
    open_outbound_db as _open_outbound_raw,
    upsert_session_routing,
)
from src.db.sessions import (
    Session,
    create_session,
    find_session_by_agent_group,
    find_session_for_agent,
    get_session,
    update_session,
)
from src.log import log


def sessions_base_dir() -> str:
    return os.path.join(get_config().data_dir_abs, "v2-sessions")


def session_dir(agent_group_id: str, session_id: str) -> str:
    return os.path.join(sessions_base_dir(), agent_group_id, session_id)


def inbound_db_path(agent_group_id: str, session_id: str) -> str:
    return os.path.join(session_dir(agent_group_id, session_id), "inbound.db")


def outbound_db_path(agent_group_id: str, session_id: str) -> str:
    return os.path.join(session_dir(agent_group_id, session_id), "outbound.db")


def heartbeat_path(agent_group_id: str, session_id: str) -> str:
    return os.path.join(session_dir(agent_group_id, session_id), ".heartbeat")

def _is_safe_name(name: object) -> bool:
    if not isinstance(name, str) or name == "":
        return False
    if name in (".", ".."):
        return False
    if "\x00" in name:
        return False
    if "/" in name or "\\" in name:
        return False
    if name.startswith("."):
        return False
    if os.path.basename(name) != name:
        return False
    return True


def _is_path_inside(parent: str, child: str) -> bool:
    parent = os.path.realpath(parent)
    child = os.path.realpath(child)
    try:
        rel = os.path.relpath(child, parent)
    except ValueError:
        # Windows 上跨盘符。
        return False
    if rel == "." or rel == "":
        return True
    return not rel.startswith("..") and not os.path.isabs(rel)


def _derive_attachment_name(att: dict) -> str:

    for key in ("name", "filename"):
        v = att.get(key)
        if isinstance(v, str) and v:
            return v
    return f"attachment-{int(time.time() * 1000)}"


def _generate_id() -> str:
    return f"sess-{int(time.time() * 1000)}-{''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=6))}"

def resolve_session(
    agent_group_id: str,
    messaging_group_id: Optional[str],
    thread_id: Optional[str],
    session_mode: str,                # 'shared' | 'per-thread' | 'agent-shared'（会话模式）
) -> tuple[Session, bool]:

    if session_mode == "agent-shared":
        existing = find_session_by_agent_group(agent_group_id)
        if existing is not None:
            return existing, False
    elif messaging_group_id is not None:
        lookup_thread_id = None if session_mode == "shared" else thread_id
        existing = find_session_for_agent(agent_group_id, messaging_group_id, lookup_thread_id)
        if existing is not None:
            return existing, False

    sid = _generate_id()
    persisted_thread_id = thread_id if session_mode == "per-thread" else None
    session = Session(
        id=sid,
        agent_group_id=agent_group_id,
        messaging_group_id=messaging_group_id,
        thread_id=persisted_thread_id,
        agent_provider=None,
        status="active",
        container_status="stopped",
        last_active=None,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    create_session(session)
    init_session_folder(agent_group_id, sid)
    log.info(
        "session_created",
        id=sid,
        agent_group_id=agent_group_id,
        messaging_group_id=messaging_group_id,
        thread_id=persisted_thread_id,
        session_mode=session_mode,
    )
    return session, True


def init_session_folder(agent_group_id: str, session_id: str) -> None:

    dir_ = session_dir(agent_group_id, session_id)
    os.makedirs(dir_, exist_ok=True)
    os.makedirs(os.path.join(dir_, "outbox"), exist_ok=True)

    ensure_schema(inbound_db_path(agent_group_id, session_id), "inbound")
    ensure_schema(outbound_db_path(agent_group_id, session_id), "outbound")


def write_session_routing(agent_group_id: str, session_id: str) -> None:

    db_path = inbound_db_path(agent_group_id, session_id)
    if not os.path.exists(db_path):
        return

    session = get_session(session_id)
    if session is None:
        return

    channel_type: Optional[str] = None
    platform_id: Optional[str] = None
    if session.messaging_group_id:
        mg = get_messaging_group(session.messaging_group_id)
        if mg is not None:
            channel_type = mg.channel_type
            platform_id = mg.platform_id

    db = _open_inbound_raw(db_path)
    try:
        upsert_session_routing(db, channel_type=channel_type, platform_id=platform_id, thread_id=session.thread_id)
    finally:
        db.close()


def write_session_message(
    agent_group_id: str,
    session_id: str,
    message: dict,
) -> None:
    content = extract_attachment_files(
        agent_group_id,
        session_id,
        message["id"],
        message["content"],
    )

    db_path = inbound_db_path(agent_group_id, session_id)
    db = _open_inbound_raw(db_path)
    try:
        insert_message(
            db,
            id=message["id"],
            kind=message["kind"],
            timestamp=message["timestamp"],
            platform_id=message.get("platform_id"),
            channel_type=message.get("channel_type"),
            thread_id=message.get("thread_id"),
            content=content,
            process_after=message.get("process_after"),
            recurrence=message.get("recurrence"),
            trigger=int(message.get("trigger", 1)),
            source_session_id=message.get("source_session_id"),
            on_wake=int(message.get("on_wake", 0)),
        )
    finally:
        db.close()

    update_session(session_id, last_active=datetime.now(timezone.utc).isoformat())

def write_outbound_direct(
    agent_group_id: str,
    session_id: str,
    message: dict,
) -> None:

    db = _open_outbound_raw(outbound_db_path(agent_group_id, session_id), readonly=False)
    try:
        db.execute(
            """
            INSERT OR IGNORE INTO messages_out
              (id, seq, timestamp, kind, platform_id, channel_type, thread_id, content)
            VALUES
              (?, (SELECT COALESCE(MAX(seq), 0) + 2 FROM messages_out), datetime('now'),
               ?, ?, ?, ?, ?)
            """,
            (
                message["id"],
                message["kind"],
                message.get("platform_id"),
                message.get("channel_type"),
                message.get("thread_id"),
                message["content"],
            ),
        )
        db.commit()
    finally:
        db.close()


def extract_attachment_files(
    agent_group_id: str,
    session_id: str,
    message_id: str,
    content_str: str,
) -> str:
    try:
        parsed = json.loads(content_str)
    except (ValueError, TypeError):
        return content_str

    if not isinstance(parsed, dict):
        return content_str

    attachments = parsed.get("attachments")
    if not isinstance(attachments, list):
        return content_str

    if not _is_safe_name(message_id):
        log.warn("inbound_unsafe_message_id", message_id=message_id)
        return content_str

    sess_dir = session_dir(agent_group_id, session_id)
    inbox_root = os.path.join(sess_dir, "inbox")
    inbox_dir = os.path.join(inbox_root, message_id)

    changed = False
    for att in attachments:
        if not isinstance(att, dict):
            continue
        data = att.get("data")
        if not isinstance(data, str):
            continue

        raw_name = _derive_attachment_name(att)
        filename = raw_name if _is_safe_name(raw_name) else f"attachment-{int(time.time() * 1000)}"
        if filename != raw_name:
            log.warn(
                "inbound_unsafe_attachment_name",
                message_id=message_id, raw_name=raw_name, replacement=filename,
            )

        if os.path.exists(inbox_dir):
            try:
                st = os.lstat(inbox_dir)
            except OSError:
                continue
            import stat as _stat
            if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISDIR(st.st_mode):
                log.warn("inbox_dir_unsafe", message_id=message_id, inbox_dir=inbox_dir)
                continue
        os.makedirs(inbox_dir, exist_ok=True)

        if not os.path.isdir(inbox_root):
            continue
        if not _is_path_inside(inbox_root, inbox_dir):
            log.warn("inbox_dir_escape", message_id=message_id, inbox_dir=inbox_dir)
            continue

        file_path = os.path.join(inbox_dir, filename)
        try:
            fd = os.open(file_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
        except FileExistsError:
            log.warn("inbox_file_exists", message_id=message_id, filename=filename)
            continue
        except OSError as e:
            log.warn("inbox_file_open_failed", message_id=message_id, filename=filename, err=str(e))
            continue

        try:
            try:
                payload = b64decode(data, validate=False)
            except Exception:
                os.close(fd)
                os.unlink(file_path)
                log.warn("inbox_b64_invalid", message_id=message_id, filename=filename)
                continue
            os.write(fd, payload)
        finally:
            os.close(fd)

        att["name"] = filename
        att["localPath"] = f"inbox/{message_id}/{filename}"
        att.pop("data", None)
        changed = True
        log.debug("inbox_saved", message_id=message_id, filename=filename)

    return json.dumps(parsed) if changed else content_str


def read_outbox_files(
    agent_group_id: str,
    session_id: str,
    msg_id: str,
    filenames: list[str],
) -> Optional[list[OutboundFile]]:

    if not _is_safe_name(msg_id):
        log.warn("outbox_unsafe_message_id", message_id=msg_id)
        return None

    sess_dir = session_dir(agent_group_id, session_id)
    outbox_dir = os.path.join(sess_dir, "outbox", msg_id)
    if not os.path.exists(outbox_dir):
        return None

    import stat as _stat
    try:
        st = os.lstat(outbox_dir)
    except OSError as e:
        log.warn("outbox_lstat_failed", message_id=msg_id, err=str(e))
        return None
    if not _stat.S_ISDIR(st.st_mode) or _stat.S_ISLNK(st.st_mode):
        log.warn("outbox_dir_unsafe", message_id=msg_id, outbox_dir=outbox_dir)
        return None

    real_outbox_dir = os.path.realpath(outbox_dir)

    files: list[OutboundFile] = []
    for filename in filenames:
        if not _is_safe_name(filename):
            log.warn("outbox_unsafe_name", message_id=msg_id, filename=filename)
            continue
        file_path = os.path.join(outbox_dir, filename)
        try:
            fst = os.lstat(file_path)
        except OSError:
            log.warn("outbox_file_missing", message_id=msg_id, filename=filename)
            continue
        if not _stat.S_ISREG(fst.st_mode) or _stat.S_ISLNK(fst.st_mode):
            log.warn("outbox_file_unsafe", message_id=msg_id, filename=filename)
            continue
        real_path = os.path.realpath(file_path)
        if not _is_path_inside(real_outbox_dir, real_path):
            log.warn("outbox_file_escape", message_id=msg_id, filename=filename)
            continue
        try:
            with open(real_path, "rb") as f:
                data = f.read()
        except OSError as e:
            log.warn("outbox_file_read_failed", message_id=msg_id, filename=filename, err=str(e))
            continue
        files.append(OutboundFile(filename=filename, data=data))

    return files if files else None


def clear_outbox(agent_group_id: str, session_id: str, msg_id: str) -> None:

    if not _is_safe_name(msg_id):
        log.warn("outbox_cleanup_unsafe_id", message_id=msg_id)
        return

    sess_dir = session_dir(agent_group_id, session_id)
    outbox_dir = os.path.join(sess_dir, "outbox", msg_id)
    if not os.path.exists(outbox_dir):
        return

    import shutil
    import stat as _stat
    try:
        st = os.lstat(outbox_dir)
        if not _stat.S_ISDIR(st.st_mode) or _stat.S_ISLNK(st.st_mode):
            log.warn("outbox_cleanup_unsafe_dir", message_id=msg_id, outbox_dir=outbox_dir)
            return
        outbox_base = os.path.join(sess_dir, "outbox")
        if not _is_path_inside(outbox_base, outbox_dir):
            log.warn("outbox_cleanup_escape", message_id=msg_id)
            return
        shutil.rmtree(outbox_dir, ignore_errors=True)
    except OSError as e:
        log.warn("outbox_cleanup_failed", message_id=msg_id, err=str(e))


def mark_container_running(session_id: str) -> None:
    update_session(
        session_id,
        container_status="running",
        last_active=datetime.now(timezone.utc).isoformat(),
    )


def mark_container_idle(session_id: str) -> None:
    update_session(session_id, container_status="idle")


def mark_container_stopped(session_id: str) -> None:
    update_session(session_id, container_status="stopped")


def open_inbound_db(agent_group_id: str, session_id: str) -> sqlite3.Connection:
    return _open_inbound_raw(inbound_db_path(agent_group_id, session_id))

def open_outbound_db(agent_group_id: str, session_id: str, readonly: bool = True) -> sqlite3.Connection:
    return _open_outbound_raw(outbound_db_path(agent_group_id, session_id), readonly=readonly)
