"""文件层记忆的加载（无索引，纯文件）。

文件是 source of truth：
  /workspace/agent/CLAUDE.local.md      —— 长期记忆（精编）
  /workspace/agent/memory/YYYY-MM-DD.md —— 每日日志（append-only）

每轮 poll 调用 build_memory_section()：私聊会话注入
CLAUDE.local.md + 昨日/今日两天日志；群聊或无法判定时返回空串
（fail-closed —— 隐私泄露不可逆，少加载一轮只是体验损失）。

CLAWSIDE_AGENT_DIR 环境变量仅用于测试覆盖路径。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agent_runner.db.session_routing import get_session_routing


MAX_FILE_CHARS = 16_000


def _log(msg: str) -> None:
    print(f"[memory] {msg}", file=sys.stderr, flush=True)


def _agent_dir() -> str:
    return os.environ.get("CLAWSIDE_AGENT_DIR", "/workspace/agent")


def _tz() -> ZoneInfo:
    name = os.environ.get("TZ") or "UTC"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def _local_dates() -> tuple[str, str]:
    now = datetime.now(_tz())
    return now.strftime("%Y-%m-%d"), (now - timedelta(days=1)).strftime("%Y-%m-%d")


def _is_private_session() -> bool:
    try:
        sr = get_session_routing()
    except Exception as e:
        _log(f"routing read failed, treating as group: {e!r}")
        return False
    return sr.get("is_group") == 0


def _read_capped(path: str, keep: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except FileNotFoundError:
        return ""
    except OSError as e:
        _log(f"could not read {path}: {e!r}")
        return ""
    if len(text) <= MAX_FILE_CHARS:
        return text
    if keep == "tail":
        return "…(truncated)\n" + text[-MAX_FILE_CHARS:]
    return text[:MAX_FILE_CHARS] + "\n…(truncated)"


def build_memory_section() -> str:
    if not _is_private_session():
        return ""

    agent_dir = _agent_dir()
    parts: list[str] = []

    local = _read_capped(os.path.join(agent_dir, "CLAUDE.local.md"), keep="head")
    if local.strip():
        parts.append("## Long-term memory (CLAUDE.local.md)\n" + local.strip())

    today, yesterday = _local_dates()
    for d in (yesterday, today):
        content = _read_capped(
            os.path.join(agent_dir, "memory", f"{d}.md"), keep="tail"
        )
        if content.strip():
            parts.append(f"## Daily log — {d}\n" + content.strip())

    if not parts:
        return ""
    return (
        "# Memory (private session — not shown to anyone else)\n\n"
        + "\n\n".join(parts)
    )
