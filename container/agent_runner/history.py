from __future__ import annotations

import logging
import sys
from typing import Any, Optional

# tiktoken 是可选依赖 不可用时回退到粗略的字节估算
try:
    import tiktoken  # type: ignore
    _HAS_TIKTOKEN = True
except ImportError:  # pragma: no cover - dependency optional in tests
    tiktoken = None  # type: ignore
    _HAS_TIKTOKEN = False

MAX_HISTORY_TOKENS = 80_000
MIN_RECENT_KEEP = 10

def _log(msg: str) -> None:
    print(f"[history] {msg}", file=sys.stderr, flush=True)

def _get_encoding(model: str):
    if not _HAS_TIKTOKEN:
        return None
    try:
        return tiktoken.encoding_for_model(model)
    except Exception:
        try:
            return tiktoken.get_encoding("cl100k_base")
        except Exception:
            return None


def _stringify_message(m: dict) -> str:
    """
    把一条 chat-completions 消息强制转为字符串以便统计 token
    """
    parts: list[str] = []
    role = m.get("role")
    if role:
        parts.append(str(role))
    content = m.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
    elif content is not None:
        parts.append(str(content))

    if m.get("tool_calls"):
        for tc in m["tool_calls"]:
            fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            parts.append(str(fn.get("name", "")))
            parts.append(str(fn.get("arguments", "")))

    return "\n".join(parts)


def count_tokens(messages: list[dict], model: str) -> int:
    enc = _get_encoding(model)
    total = 0
    for m in messages:
        s = _stringify_message(m)
        if enc is not None:
            try:
                total += len(enc.encode(s))
                continue
            except Exception:
                pass
        total += max(1, len(s) // 4)
    return total

def find_safe_cut_point(non_system: list[dict], desired_keep: int) -> int:

    n = len(non_system)
    if desired_keep >= n:
        return 0
    cut = n - desired_keep

    while cut > 0:
        m = non_system[cut]
        if m.get("role") == "tool":

            cut -= 1
            continue

        prev = non_system[cut - 1]
        if prev.get("role") == "assistant" and prev.get("tool_calls"):
            cut -= 1
            continue
        break
    return cut


async def _summarize(messages_to_summarize: list[dict], client: Any, model: str) -> str:
    """
    向模型请求较早消息的简短摘要
    """
    instruction = (
        "Summarize the following conversation between a user and an AI assistant. "
        "Capture the key facts, decisions, the user's stated preferences, "
        "and the agent's current understanding of any active tasks. "
        "Keep it under 400 words. Do not editorialize — just compress."
    )
    convo_lines: list[str] = []
    for m in messages_to_summarize:
        role = m.get("role", "unknown")
        body = _stringify_message(m)
        convo_lines.append(f"[{role}] {body}")

    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": instruction},
            {"role": "user", "content": "\n\n".join(convo_lines)},
        ],
    )
    content = resp.choices[0].message.content or ""
    return str(content)


async def maybe_compress(messages: list[dict], client: Any, model: str) -> list[dict]:
    if not messages:
        return messages

    tokens = count_tokens(messages, model)
    if tokens <= MAX_HISTORY_TOKENS:
        return messages

    system_prefix: list[dict] = []
    i = 0
    while i < len(messages) and messages[i].get("role") == "system":
        system_prefix.append(messages[i])
        i += 1
    rest = messages[i:]

    if len(rest) <= MIN_RECENT_KEEP:
        return messages  # 没有合理的可摘要内容

    cut = find_safe_cut_point(rest, MIN_RECENT_KEEP)
    older = rest[:cut]
    recent = rest[cut:]

    if not older:
        return messages

    try:
        summary_text = await _summarize(older, client, model)
        summary_msg = {
            "role": "system",
            "content": (
                "Earlier conversation summary (compressed for context limits):\n"
                + summary_text
            ),
        }
        return system_prefix + [summary_msg] + recent
    except Exception as e:
        _log(f"summarization failed, falling back to system+recent: {e!r}")
        return system_prefix + recent
