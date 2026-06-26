from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from agent_runner.db.messages_in import MessageInRow, parse_content
from agent_runner.db.session_routing import list_destinations


def _timezone() -> str:
    return os.environ.get("TZ") or "UTC"

def escape_xml(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )

def strip_internal_tags(text: str) -> str:
    """剥离 <internal>...</internal> 块然后 trim"""
    if not text:
        return ""
    out: list[str] = []
    i = 0
    while True:
        start = text.find("<internal>", i)
        if start == -1:
            out.append(text[i:])
            break
        out.append(text[i:start])
        end = text.find("</internal>", start)
        if end == -1:
            # 不闭合 丢弃其余
            break
        i = end + len("</internal>")
    return "".join(out).strip()

def _fmt_time_safe(ts: str, tz_name: str) -> str:
    if not ts:
        return ""
    try:
        s = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        try:
            local = dt.astimezone(ZoneInfo(tz_name))
        except Exception:
            local = dt
        try:
            return local.strftime("%-I:%M %p")
        except ValueError:
            formatted = local.strftime("%I:%M %p")
            if formatted.startswith("0"):
                return formatted[1:]
            return formatted
    except Exception:
        return ts


@dataclass
class RoutingContext:
    platform_id: Optional[str]
    channel_type: Optional[str]
    thread_id: Optional[str]
    in_reply_to: Optional[str]


def extract_routing(messages: list[MessageInRow]) -> RoutingContext:
    """从批次的第一条消息继承路由上下文。"""
    if not messages:
        return RoutingContext(None, None, None, None)
    first = messages[0]
    return RoutingContext(
        platform_id=first.platform_id,
        channel_type=first.channel_type,
        thread_id=first.thread_id,
        in_reply_to=first.id,
    )



_CLEAR_PREFIX = "/clear"

def is_clear_command(msg: MessageInRow) -> bool:
    """当 chat/chat-sdk 行的文本以 /clear 开头时返回 True不区分大小写"""
    if msg.kind not in ("chat", "chat-sdk"):
        return False
    content = parse_content(msg)
    text = str(content.get("text", "") or "").strip().lower()
    return text.startswith(_CLEAR_PREFIX)
# 需要重新查询的命令
def is_runner_command(msg: MessageInRow) -> bool:
    return is_clear_command(msg)
def _destination_name_for(channel_type: Optional[str], platform_id: Optional[str]) -> Optional[str]:
    if not channel_type or not platform_id:
        return None
    for d in list_destinations():
        if d.get("type") == "channel" and d.get("channel_type") == channel_type and d.get("platform_id") == platform_id:
            return d.get("name")

    return None

def _origin_attr(msg: MessageInRow) -> str:
    """为消息的来源构建 from="..." 无路由时返回空"""
    name = _destination_name_for(msg.channel_type, msg.platform_id)
    if name:
        return f' from="{escape_xml(name)}"'
    if msg.channel_type or msg.platform_id:
        return f' from="unknown:{escape_xml(msg.channel_type or "")}:{escape_xml(msg.platform_id or "")}"'
    return ""

def _format_single_chat(msg: MessageInRow, tz_name: str) -> str:
    content = parse_content(msg)
    sender = (
        content.get("sender")
        or (content.get("author") or {}).get("fullName")
        or (content.get("author") or {}).get("userName")
        or "Unknown"
    )
    time = _fmt_time_safe(msg.timestamp, tz_name)
    text = content.get("text", "") or ""
    id_attr = f' id="{msg.seq}"' if msg.seq is not None else ""
    reply = content.get("replyTo") or {}
    reply_id = reply.get("id") if isinstance(reply, dict) else None
    reply_attr = f' reply_to="{escape_xml(str(reply_id))}"' if reply_id else ""

    reply_prefix = _format_reply_context(reply)
    attachments_suffix = _format_attachments(content.get("attachments"))
    from_attr = _origin_attr(msg)

    return (
        f"<message{id_attr}{from_attr} sender=\"{escape_xml(str(sender))}\" "
        f"time=\"{escape_xml(time)}\"{reply_attr}>"
        f"{reply_prefix}{escape_xml(str(text))}{attachments_suffix}"
        f"</message>"
    )

def _format_reply_context(reply_to: Any) -> str:
    if not isinstance(reply_to, dict):
        return ""
    sender = reply_to.get("sender")
    text = reply_to.get("text")
    if not sender or not text:
        return ""
    return f"\n  <quoted_message from=\"{escape_xml(str(sender))}\">{escape_xml(str(text))}</quoted_message>\n"

def _format_attachments(attachments: Any) -> str:
    if not isinstance(attachments, list) or not attachments:
        return ""
    parts = []
    for a in attachments:
        if not isinstance(a, dict):
            continue
        name = a.get("name") or a.get("filename") or "attachment"
        atype = a.get("type") or "file"
        local_path = a.get("localPath")
        url = a.get("url") or ""
        if local_path:
            parts.append(f"[{atype}: {escape_xml(str(name))} — saved to {escape_xml('/workspace/' + str(local_path))}]")
        elif url:
            parts.append(f"[{atype}: {escape_xml(str(name))} ({escape_xml(str(url))})]")
        else:
            parts.append(f"[{atype}: {escape_xml(str(name))}]")
    return "\n" + "\n".join(parts) if parts else ""

def _format_chat_batch(messages: list[MessageInRow], tz_name: str) -> str:
    if len(messages) == 1:
        return _format_single_chat(messages[0], tz_name)
    lines = ["<messages>"]
    for m in messages:
        lines.append(_format_single_chat(m, tz_name))
    lines.append("</messages>")
    return "\n".join(lines)

def _format_task(msg: MessageInRow, tz_name: str) -> str:
    content = parse_content(msg)
    from_attr = _origin_attr(msg)
    time = _fmt_time_safe(msg.timestamp, tz_name)
    parts: list[str] = []
    if content.get("scriptOutput") is not None:
        parts.append("Script output:")
        parts.append(json.dumps(content["scriptOutput"], indent=2))
        parts.append("")
    parts.append("Instructions:")
    parts.append(str(content.get("prompt") or ""))
    return f"<task{from_attr} time=\"{escape_xml(time)}\">{chr(10).join(parts)}</task>"

def _format_webhook(msg: MessageInRow) -> str:
    content = parse_content(msg)
    source = content.get("source") or "unknown"
    event = content.get("event") or "unknown"
    from_attr = _origin_attr(msg)
    payload = content.get("payload", content)
    return (
        f"<webhook{from_attr} source=\"{escape_xml(str(source))}\" event=\"{escape_xml(str(event))}\">"
        f"{json.dumps(payload, indent=2)}</webhook>"
    )

def _format_system(msg: MessageInRow) -> str:
    content = parse_content(msg)
    from_attr = _origin_attr(msg)
    action = content.get("action") or "unknown"
    status = content.get("status") or "unknown"
    result = content.get("result")
    return (
        f"<system_response{from_attr} action=\"{escape_xml(str(action))}\" "
        f"status=\"{escape_xml(str(status))}\">"
        f"{json.dumps(result)}</system_response>"
    )



def format_messages(messages: list[MessageInRow]) -> str:
    """把一批消息渲染为 agent prompt 所用的 XML"""
    tz_name = _timezone()
    header = f"<context timezone=\"{escape_xml(tz_name)}\" />\n"
    if not messages:
        return header

    chats = [m for m in messages if m.kind in ("chat", "chat-sdk")]
    tasks = [m for m in messages if m.kind == "task"]
    webhooks = [m for m in messages if m.kind == "webhook"]
    systems = [m for m in messages if m.kind == "system"]

    parts: list[str] = []
    if chats:
        parts.append(_format_chat_batch(chats, tz_name))
    for t in tasks:
        parts.append(_format_task(t, tz_name))
    for w in webhooks:
        parts.append(_format_webhook(w))
    for s in systems:
        parts.append(_format_system(s))

    return header + "\n\n".join(parts)
