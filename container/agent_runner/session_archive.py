from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from agent_runner.db.session_routing import get_session_routing


TOOL_RESULT_MAX_CHARS = 500     # 工具输出截断原始文件都在磁盘上 归档只留线索
TOOL_ARGS_MAX_CHARS = 200
SLUG_MAX_CHARS = 20

# maybe_compress 生成的摘要标记消息 其原文已在更早的归档里，不重复归档
_SUMMARY_MARKER = "Earlier conversation summary"

_TAG_RE = re.compile(r"<[^>]*>")
_SLUG_UNSAFE_RE = re.compile(r'[<>:"|?*/\\\s\x00-\x1f]+')


def _log(msg: str) -> None:
    print(f"[archive] {msg}", file=sys.stderr, flush=True)


def _agent_dir() -> str:
    return os.environ.get("CLAWSIDE_AGENT_DIR", "/workspace/agent")


def _tz() -> ZoneInfo:
    name = os.environ.get("TZ") or "UTC"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def _slug_from_history(history: list[dict]) -> str:
    """从首条真实用户消息取 slug确定性 不调 LLM。
    """
    for m in history:
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if content.lstrip().startswith(_SUMMARY_MARKER):
            continue
        text = _TAG_RE.sub(" ", content)
        text = _SLUG_UNSAFE_RE.sub("-", text.strip()).strip("-")
        if text:
            return text[:SLUG_MAX_CHARS].rstrip("-")
    return "session"


def _render_message(m: dict) -> Optional[str]:
    """无归档价值时返回 None"""
    role = m.get("role") or "unknown"
    if role == "system":
        return None   # prompt 组装物，不是对话

    content = m.get("content")
    text = content if isinstance(content, str) else ("" if content is None else str(content))

    if role == "user" and text.lstrip().startswith(_SUMMARY_MARKER):
        return None

    parts: list[str] = []
    if role == "assistant":
        for tc in m.get("tool_calls") or []:
            fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            name = fn.get("name") or "?"
            args = str(fn.get("arguments") or "")
            if len(args) > TOOL_ARGS_MAX_CHARS:
                args = args[:TOOL_ARGS_MAX_CHARS] + "…"
            parts.append(f"[tool call: {name}({args})]")

    if role == "tool" and len(text) > TOOL_RESULT_MAX_CHARS:
        text = text[:TOOL_RESULT_MAX_CHARS] + "…(truncated)"

    if text.strip():
        parts.append(text.strip())

    if not parts:
        return None
    return f"## {role}\n" + "\n".join(parts)


def archive_history(history: list[dict], reason: str) -> Optional[str]:
    try:
        if not history:
            return None
        sections = [s for s in (_render_message(m) for m in history) if s]
        if not sections:
            return None

        now = datetime.now(_tz())

        channel = "unknown"
        scope = "unknown"
        try:
            sr = get_session_routing()
            channel = sr.get("channel_type") or "unknown"
            ig = sr.get("is_group")
            scope = "private" if ig == 0 else ("group" if ig == 1 else "unknown")
        except Exception:
            pass

        slug = _slug_from_history(history)
        sess_dir = os.path.join(_agent_dir(), "sessions")
        fname = f"{now:%Y-%m-%d-%H%M}-{slug}.md"
        path = os.path.join(sess_dir, fname)
        n = 1
        while os.path.exists(path):     # 同分钟重名 → 追加序号
            n += 1
            fname = f"{now:%Y-%m-%d-%H%M}-{slug}-{n}.md"
            path = os.path.join(sess_dir, fname)

        header = (
            f"# Session archive — {now:%Y-%m-%d %H:%M}\n"
            f"- reason: {reason}\n"
            f"- channel: {channel} / {scope}\n"
        )
        os.makedirs(sess_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(header + "\n" + "\n\n".join(sections) + "\n")

        rel = f"sessions/{fname}"
        _log(f"archived {len(sections)} message(s) → {rel} (reason={reason})")
        return rel
    except Exception as e:
        _log(f"archive failed (non-fatal): {e!r}")
        return None
