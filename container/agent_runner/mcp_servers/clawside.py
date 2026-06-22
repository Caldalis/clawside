from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP

from agent_runner.skill_loader import SkillRegistry


INBOUND_PATH = os.environ.get("CLAWSIDE_INBOUND_DB", "/workspace/inbound.db")
OUTBOUND_PATH = os.environ.get("CLAWSIDE_OUTBOUND_DB", "/workspace/outbound.db")
WORKSPACE_ROOT = os.environ.get("CLAWSIDE_WORKSPACE_ROOT", "/workspace")

def _open_inbound():
    conn = sqlite3.connect(f"file:{INBOUND_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA mmap_size=0")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def _open_outbound():
    conn = sqlite3.connect(OUTBOUND_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def _next_odd_seq(db) -> int:
    row = db.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM messages_out").fetchone()
    m = row["m"]
    if m < 1:
        return 1
    if m % 2 == 1:
        return m + 2
    return m + 1

def _generate_id(prefix: str = "msg") -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _write_message_out(
    *,
    id: str,
    kind: str,
    platform_id: Optional[str],
    channel_type: Optional[str],
    thread_id: Optional[str],
    content: dict,
    in_reply_to: Optional[str] = None,
    deliver_after: Optional[str] = None,
) -> int:
    db = _open_outbound()
    try:
        seq = _next_odd_seq(db)
        db.execute(
            """
            INSERT INTO messages_out (
              id, seq, in_reply_to, timestamp, deliver_after, recurrence,
              kind, platform_id, channel_type, thread_id, content
            ) VALUES (
              :id, :seq, :in_reply_to, :timestamp, :deliver_after, NULL,
              :kind, :platform_id, :channel_type, :thread_id, :content
            )
            """,
            {
                "id": id,
                "seq": seq,
                "in_reply_to": in_reply_to,
                "timestamp": _now_iso(),
                "deliver_after": deliver_after,
                "kind": kind,
                "platform_id": platform_id,
                "channel_type": channel_type,
                "thread_id": thread_id,
                "content": json.dumps(content),
            },
        )
        db.commit()
        return seq
    finally:
        db.close()

def _get_session_routing() -> dict:
    db = _open_inbound()
    try:
        row = db.execute(
            "SELECT channel_type, platform_id, thread_id "
            "FROM session_routing WHERE id = 1"
        ).fetchone()
    finally:
        db.close()
    if row is None:
        return {"channel_type": None, "platform_id": None, "thread_id": None}
    return {
        "channel_type": row["channel_type"],
        "platform_id": row["platform_id"],
        "thread_id": row["thread_id"],
    }

def _list_destinations() -> list[dict]:
    db = _open_inbound()
    try:
        rows = db.execute(
            "SELECT name, display_name, type, channel_type, platform_id, "
            "agent_group_id FROM destinations ORDER BY name"
        ).fetchall()
    finally:
        db.close()
    return [dict(r) for r in rows]

def _find_destination(name: str) -> Optional[dict]:
    for d in _list_destinations():
        if d.get("name") == name:
            return d
    return None

def _reverse_lookup_destination_name(channel_type: str, platform_id: str) -> Optional[str]:
    db = _open_inbound()
    try:
        row = db.execute(
            "SELECT name FROM destinations "
            "WHERE channel_type = ? AND platform_id = ? AND type = 'channel' LIMIT 1",
            (channel_type, platform_id),
        ).fetchone()
    finally:
        db.close()
    return row["name"] if row else None

def _destination_names() -> str:
    names = [d["name"] for d in _list_destinations()]
    return ", ".join(names) if names else "(none)"


mcp = FastMCP("clawside-tools")

_local_registry = SkillRegistry(["/app/skills", "/workspace/agent/skills"])




@mcp.tool()
async def send_message(text: str, to: Optional[str] = None) -> str:
    """Send a message to a named destination.

    `to` is optional — when omitted, the message is sent back to the
    channel/thread this session is bound to.
    """
    if not text:
        return "Error: text is required"

    if to is None:
        # 默认目标 = 用 session_routing 做反查。
        sr = _get_session_routing()
        if not sr["channel_type"] or not sr["platform_id"]:
            return f"Error: no default destination — specify `to`. Available: {_destination_names()}"
        name = _reverse_lookup_destination_name(sr["channel_type"], sr["platform_id"])
        if not name:
            return (
                "Error: session routing has no matching destinations row — "
                f"specify `to`. Available: {_destination_names()}"
            )
        to = name

    dest = _find_destination(to)
    if dest is None:
        return f"Error: unknown destination \"{to}\". Available: {_destination_names()}"

    if dest["type"] == "channel":
        channel_type = dest["channel_type"]
        platform_id = dest["platform_id"]
        # 当发回到同一渠道时保留会话 thread_id。
        sr = _get_session_routing()
        thread_id = sr["thread_id"] if (sr["channel_type"] == channel_type and sr["platform_id"] == platform_id) else None
    else:
        channel_type = "agent"
        platform_id = dest["agent_group_id"]
        thread_id = None

    msg_id = _generate_id()
    seq = _write_message_out(
        id=msg_id,
        kind="chat",
        platform_id=platform_id,
        channel_type=channel_type,
        thread_id=thread_id,
        content={"text": text},
    )
    return f"Message sent to {to} (id: {seq})"


@mcp.tool()
async def send_file(
    path: str,
    to: Optional[str] = None,
    text: str = "",
    filename: Optional[str] = None,
) -> str:
    """Send a file from the workspace to a destination.

    `path` is resolved relative to /workspace/agent/. Files are copied into
    /workspace/outbox/<id>/ for the host to pick up.
    """
    if not path:
        return "Error: path is required"

    resolved = path if os.path.isabs(path) else os.path.abspath(os.path.join("/workspace/agent", path))
    # 拒绝越出 /workspace 的路径。
    if not resolved.startswith(WORKSPACE_ROOT):
        return f"Error: path is outside /workspace ({resolved})"
    if not os.path.isfile(resolved):
        return f"Error: file not found: {path}"

    if to is None:
        sr = _get_session_routing()
        if not sr["channel_type"] or not sr["platform_id"]:
            return f"Error: no default destination — specify `to`. Available: {_destination_names()}"
        name = _reverse_lookup_destination_name(sr["channel_type"], sr["platform_id"])
        if not name:
            return f"Error: no matching destination — specify `to`. Available: {_destination_names()}"
        to = name

    dest = _find_destination(to)
    if dest is None:
        return f"Error: unknown destination \"{to}\". Available: {_destination_names()}"

    if dest["type"] == "channel":
        channel_type = dest["channel_type"]
        platform_id = dest["platform_id"]
        sr = _get_session_routing()
        thread_id = sr["thread_id"] if (sr["channel_type"] == channel_type and sr["platform_id"] == platform_id) else None
    else:
        channel_type = "agent"
        platform_id = dest["agent_group_id"]
        thread_id = None

    msg_id = _generate_id()
    display_name = filename or os.path.basename(resolved)
    try:
        outbox_dir = os.path.join("/workspace/outbox", msg_id)
        os.makedirs(outbox_dir, exist_ok=True)
        with open(resolved, "rb") as src, open(os.path.join(outbox_dir, display_name), "wb") as dst:
            dst.write(src.read())
    except OSError as e:
        return f"Error: could not stage file: {e!r}"

    _write_message_out(
        id=msg_id,
        kind="chat",
        platform_id=platform_id,
        channel_type=channel_type,
        thread_id=thread_id,
        content={"text": text or "", "files": [display_name]},
    )
    return f"File sent to {to} (id: {msg_id}, filename: {display_name})"


@mcp.tool()
async def edit_message(message_id: int, text: str) -> str:
    """Edit a previously sent message identified by its numeric id.

    The host translates the edit into a platform-specific edit if the
    channel supports it; otherwise it sends a new message.
    """
    if not message_id or not text:
        return "Error: message_id and text are required"

    db = _open_outbound()
    try:
        row = db.execute(
            "SELECT platform_id, channel_type, thread_id FROM messages_out "
            "WHERE seq = ?",
            (int(message_id),),
        ).fetchone()
    finally:
        db.close()
    if row is None:
        return f"Error: message #{message_id} not found"

    _write_message_out(
        id=_generate_id(),
        kind="chat",
        platform_id=row["platform_id"],
        channel_type=row["channel_type"],
        thread_id=row["thread_id"],
        content={"operation": "edit", "messageId": int(message_id), "text": text},
    )
    return f"Message edit queued for #{message_id}"


@mcp.tool()
async def add_reaction(message_id: int, emoji: str) -> str:
    """Add an emoji reaction to a message. [MVP stub — not implemented in clawside]"""
    return "Error: add_reaction is not implemented"


@mcp.tool()
async def ask_user_question(
    title: str,
    question: str,
    options: list,
    timeout: int = 300,
) -> str:
    """Ask the user a multiple-choice question. Blocks until response or timeout.

    The host renders this as inline buttons (Telegram) / numbered options
    (CLI). Returns the value (or label) of the chosen option, or an error
    string on timeout.
    """
    if not title or not question or not options:
        return "Error: title, question, and options are required"

    normalized: list[dict] = []
    for o in options:
        if isinstance(o, str):
            normalized.append({"label": o, "value": o})
        elif isinstance(o, dict):
            label = o.get("label")
            if not label:
                continue
            normalized.append(
                {
                    "label": label,
                    "selectedLabel": o.get("selectedLabel", label),
                    "value": o.get("value", label),
                }
            )
    if not normalized:
        return "Error: no usable options"

    question_id = _generate_id("qst")
    sr = _get_session_routing()

    _write_message_out(
        id=question_id,
        kind="chat-sdk",
        platform_id=sr["platform_id"],
        channel_type=sr["channel_type"],
        thread_id=sr["thread_id"],
        content={
            "type": "ask_question",
            "question_id": question_id,
            "title": title,
            "question": question,
            "options": normalized,
        },
    )

    deadline = time.time() + max(1, int(timeout))
    while time.time() < deadline:
        db = _open_inbound()
        try:
            row = db.execute(
                """
                SELECT id, content FROM messages_in
                 WHERE status = 'pending'
                   AND kind = 'system'
                   AND json_extract(content, '$.question_id') = ?
                 LIMIT 1
                """,
                (question_id,),
            ).fetchone()
        finally:
            db.close()

        if row is not None:
            # 通过 processing_ack 确认，避免主机重复投递。
            ack = _open_outbound()
            try:
                ack.execute(
                    "INSERT OR REPLACE INTO processing_ack "
                    "(message_id, status, status_changed) "
                    "VALUES (?, 'completed', datetime('now'))",
                    (row["id"],),
                )
                ack.commit()
            finally:
                ack.close()

            try:
                payload = json.loads(row["content"])
            except (json.JSONDecodeError, TypeError):
                payload = {}
            return str(payload.get("value") or payload.get("selectedOption") or "")

        await asyncio.sleep(1.0)

    return f"Error: question timed out after {timeout}s"


@mcp.tool()
async def send_card(card: dict, fallback_text: str = "") -> str:
    """Send a structured card. [MVP stub — use ask_user_question instead]"""
    return "Error: send_card not implemented in clawside MVP; use ask_user_question"



def _parse_zoned_to_utc(s: str) -> str:
    """接受 naive 本地时间或带偏移的 ISO；返回 UTC ISO。失败时抛 ValueError。"""
    tz_name = os.environ.get("TZ") or "UTC"
    s = s.strip()
    s_norm = s.replace("Z", "+00:00") if s.endswith("Z") else s
    dt = datetime.fromisoformat(s_norm)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt.astimezone(ZoneInfo("UTC")).isoformat()


@mcp.tool()
async def schedule_task(
    prompt: str,
    process_after: str,
    recurrence: Optional[str] = None,
    script: Optional[str] = None,
) -> str:
    """Schedule a one-shot or recurring task.

    `process_after` may be naive local (interpreted in the user's timezone)
    or ISO with offset. `recurrence` is a cron expression in the user's tz.
    """
    if not prompt or not process_after:
        return "Error: prompt and process_after are required"

    try:
        process_after_utc = _parse_zoned_to_utc(process_after)
    except ValueError:
        return f"Error: invalid process_after: {process_after}"

    task_id = _generate_id("task")
    sr = _get_session_routing()
    _write_message_out(
        id=task_id,
        kind="system",
        platform_id=sr["platform_id"],
        channel_type=sr["channel_type"],
        thread_id=sr["thread_id"],
        content={
            "action": "schedule_task",
            "task_id": task_id,
            "prompt": prompt,
            "script": script,
            "process_after": process_after_utc,
            "recurrence": recurrence,
            "platform_id": sr["platform_id"],
            "channel_type": sr["channel_type"],
            "thread_id": sr["thread_id"],
        },
    )
    suffix = f", recurrence: {recurrence}" if recurrence else ""
    return f"Task scheduled (id: {task_id}, runs at: {process_after_utc}{suffix})"


@mcp.tool()
async def list_tasks(status: Optional[str] = None) -> str:
    """List scheduled tasks (one row per live series)."""
    db = _open_inbound()
    try:
        if status:
            rows = db.execute(
                """
                SELECT series_id AS id, status, process_after, recurrence, content,
                       MAX(seq) AS _seq
                  FROM messages_in
                 WHERE kind = 'task' AND status = ?
                 GROUP BY series_id
                 ORDER BY process_after ASC
                """,
                (status,),
            ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT series_id AS id, status, process_after, recurrence, content,
                       MAX(seq) AS _seq
                  FROM messages_in
                 WHERE kind = 'task' AND status IN ('pending', 'paused')
                 GROUP BY series_id
                 ORDER BY process_after ASC
                """
            ).fetchall()
    finally:
        db.close()

    if not rows:
        return "No tasks found."

    lines: list[str] = []
    for r in rows:
        try:
            content = json.loads(r["content"])
        except (json.JSONDecodeError, TypeError):
            content = {}
        prompt = str(content.get("prompt") or "")[:80]
        recur = f"recur={r['recurrence']} " if r["recurrence"] else ""
        lines.append(
            f"- {r['id']} [{r['status']}] at={r['process_after'] or 'now'} {recur}→ {prompt}"
        )
    return "\n".join(lines)


def _system_action_message(action: str, **kwargs: Any) -> int:
    payload = {"action": action, **kwargs}
    return _write_message_out(
        id=_generate_id("sys"),
        kind="system",
        platform_id=None,
        channel_type=None,
        thread_id=None,
        content=payload,
    )


@mcp.tool()
async def cancel_task(task_id: str) -> str:
    if not task_id:
        return "Error: task_id is required"
    _system_action_message("cancel_task", task_id=task_id)
    return f"Task cancellation requested: {task_id}"


@mcp.tool()
async def pause_task(task_id: str) -> str:
    if not task_id:
        return "Error: task_id is required"
    _system_action_message("pause_task", task_id=task_id)
    return f"Task pause requested: {task_id}"


@mcp.tool()
async def resume_task(task_id: str) -> str:
    if not task_id:
        return "Error: task_id is required"
    _system_action_message("resume_task", task_id=task_id)
    return f"Task resume requested: {task_id}"


@mcp.tool()
async def update_task(
    task_id: str,
    prompt: Optional[str] = None,
    process_after: Optional[str] = None,
    recurrence: Optional[str] = None,
    script: Optional[str] = None,
) -> str:
    """Update a scheduled task. Any field omitted is left unchanged.

    Pass an empty string to clear `recurrence` or `script`.
    """
    if not task_id:
        return "Error: task_id is required"
    update: dict[str, Any] = {"task_id": task_id}
    if prompt is not None:
        update["prompt"] = prompt
    if process_after is not None:
        try:
            update["process_after"] = _parse_zoned_to_utc(process_after)
        except ValueError:
            return f"Error: invalid process_after: {process_after}"
    if recurrence is not None:
        update["recurrence"] = None if recurrence == "" else recurrence
    if script is not None:
        update["script"] = None if script == "" else script

    if len(update) == 1:
        return "Error: at least one field to update is required"

    _system_action_message("update_task", **update)
    return f"Task update requested: {task_id}"



def _safe_workspace_path(path: str) -> Optional[str]:
    """在 /workspace/ 下解析 `path`。越出根时返回 None。"""
    if not path:
        return None
    candidate = path if os.path.isabs(path) else os.path.join(WORKSPACE_ROOT, path)
    abs_path = os.path.abspath(candidate)
    if not (abs_path == WORKSPACE_ROOT or abs_path.startswith(WORKSPACE_ROOT + os.sep)):
        return None
    return abs_path


@mcp.tool()
async def read_file(path: str, offset: int = 0, limit: int = 200) -> str:
    """Read lines [offset, offset+limit) from a file under /workspace/."""
    abs_path = _safe_workspace_path(path)
    if abs_path is None:
        return f"Error: path is outside /workspace ({path})"
    if not os.path.isfile(abs_path):
        return f"Error: file not found: {path}"
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        return f"Error: could not read file: {e!r}"
    start = max(0, int(offset))
    end = start + max(1, int(limit))
    chunk = lines[start:end]
    return "".join(chunk)


@mcp.tool()
async def write_file(path: str, content: str) -> str:
    """Write (overwrite) a file under /workspace/. Creates parent dirs as needed."""
    abs_path = _safe_workspace_path(path)
    if abs_path is None:
        return f"Error: path is outside /workspace ({path})"
    try:
        os.makedirs(os.path.dirname(abs_path) or ".", exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return f"Error: could not write file: {e!r}"
    return f"Wrote {len(content)} chars to {path}"


@mcp.tool()
async def edit_file(path: str, old_string: str, new_string: str) -> str:
    """Replace exactly one occurrence of `old_string` with `new_string`."""
    abs_path = _safe_workspace_path(path)
    if abs_path is None:
        return f"Error: path is outside /workspace ({path})"
    if not os.path.isfile(abs_path):
        return f"Error: file not found: {path}"
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        return f"Error: could not read file: {e!r}"
    count = text.count(old_string)
    if count == 0:
        return "Error: old_string not found"
    if count > 1:
        return f"Error: old_string is ambiguous (found {count} occurrences)"
    new_text = text.replace(old_string, new_string, 1)
    try:
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(new_text)
    except OSError as e:
        return f"Error: could not write file: {e!r}"
    return f"Edited {path}"


@mcp.tool()
async def run_bash(command: str, timeout_ms: int = 30000) -> str:
    """Run a bash command from /workspace/. Combined stdout+stderr returned."""
    if not command:
        return "Error: command is required"
    if "../" in command:
        return "Error: command contains traversal '../'"
    timeout_s = max(1, int(timeout_ms) // 1000)
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=WORKSPACE_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout_s}s"
    except OSError as e:
        return f"Error: failed to run command: {e!r}"
    out = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        return f"[exit {result.returncode}]\n{out}"
    return out




@mcp.tool()
async def load_skill(name: str) -> str:
    """Load the full SKILL.md content for a registered skill by name."""
    content = _local_registry.load(name)
    if content is None:
        available = [m.name for m in _local_registry.all_skills]
        return f"Skill '{name}' not found. Available: {available}"
    return content

if __name__ == "__main__":  # pragma: no cover 作为子进程启动
    mcp.run(transport="stdio")
