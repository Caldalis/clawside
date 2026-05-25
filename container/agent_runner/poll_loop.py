
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
import uuid
from typing import Any, Optional

from agent_runner.config import ContainerConfig
from agent_runner.db.connection import touch_heartbeat, clear_stale_processing_acks
from agent_runner.db.messages_in import (
    MessageInRow,
    get_pending_messages,
    mark_completed,
    mark_failed,
    mark_processing,
)
from agent_runner.db.messages_out import write_message_out
from agent_runner.db.session_routing import (
    find_destination_by_name,
    list_destinations,
)
from agent_runner.db.session_state import get_history, set_history
from agent_runner.formatter import (
    RoutingContext,
    extract_routing,
    format_messages,
    is_clear_command,
    strip_internal_tags,
)
from agent_runner.history import maybe_compress
from agent_runner.skill_loader import SkillContext, SkillRegistry, build_skills_prompt
from agent_runner.agent import run_agent


POLL_INTERVAL_MS = 1000
ACTIVE_POLL_INTERVAL_MS = 500

MESSAGE_BLOCK_RE = re.compile(
    r'<message\s+to="([^"]+)"\s*>([\s\S]*?)</message>'
)

def _log(msg: str) -> None:
    print(f"[poll-loop] {msg}", file=sys.stderr, flush=True)

def _generate_id() -> str:
    return f"msg-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"

def _has_assistant_message(history: list[dict]) -> bool:
    return any(m.get("role") == "assistant" for m in history)

def _addendum_prompt(assistant_name: str) -> str:
    dests = list_destinations()
    if not dests:
        dest_block = "(no destinations configured)"
    else:
        lines = []
        for d in dests:
            label = d.get("display_name") or d.get("name")
            if d.get("type") == "channel":
                lines.append(f"  - {d['name']}: {label} (channel: {d.get('channel_type')})")
            else:
                lines.append(f"  - {d['name']}: {label} (agent group)")
        dest_block = "\n".join(lines)
    return (
        f"You are {assistant_name}.\n\n"
        "Wrap every message you send to the user in <message to=\"name\">...</message> blocks. "
        "Use <internal>...</internal> for scratchpad / reasoning that the user should NOT see.\n\n"
        f"Your destinations:\n{dest_block}"
    )

def _build_system_prompt(
    base_prompt: str, skills_section: str, addendum: str
) -> str:
    parts = [p for p in (base_prompt or "", skills_section or "", addendum or "") if p]
    return "\n\n".join(parts)

def _dispatch_message_blocks(text: str, routing: RoutingContext) -> tuple[int, bool]:

    sent = 0
    last_index = 0
    scratchpad_parts: list[str] = []

    for match in MESSAGE_BLOCK_RE.finditer(text):
        if match.start() > last_index:
            scratchpad_parts.append(text[last_index:match.start()])
        to_name = match.group(1)
        body = match.group(2).strip()
        last_index = match.end()

        dest = find_destination_by_name(to_name)
        if dest is None:
            _log(f"unknown destination in <message to=\"{to_name}\">, dropping block")
            scratchpad_parts.append(f"[dropped: unknown destination \"{to_name}\"] {body}")
            continue
        _write_to_destination(dest, body, routing)
        sent += 1

    if last_index < len(text):
        scratchpad_parts.append(text[last_index:])

    scratch = strip_internal_tags("".join(scratchpad_parts))
    if scratch:
        _log(f"[scratchpad] {scratch[:500]}{'…' if len(scratch) > 500 else ''}")
    has_unwrapped = sent == 0 and bool(scratch)
    return sent, has_unwrapped


def _write_to_destination(dest: dict, body: str, routing: RoutingContext) -> None:
    if dest["type"] == "channel":
        platform_id = dest["platform_id"]
        channel_type = dest["channel_type"]
        thread_id = (
            routing.thread_id
            if (routing.channel_type == channel_type and routing.platform_id == platform_id)
            else None
        )
    else:
        platform_id = dest.get("agent_group_id")
        channel_type = "agent"
        thread_id = None

    write_message_out(
        id=_generate_id(),
        kind="chat",
        platform_id=platform_id,
        channel_type=channel_type,
        thread_id=thread_id,
        content=json.dumps({"text": body}),
        in_reply_to=routing.in_reply_to,
    )


async def _sleep_ms(ms: int) -> None:
    await asyncio.sleep(ms / 1000.0)


async def run(
    mcp_manager: Any,
    client: Any,
    config: ContainerConfig,
    registry: SkillRegistry,
    base_prompt: str,
) -> None:

    clear_stale_processing_acks()
    is_first_poll = True
    poll_count = 0

    while True:
        poll_count += 1
        batch_ids: list[str] = []
        try:
            messages = [
                m
                for m in get_pending_messages(
                    is_first_poll=is_first_poll,
                    max_count=config.max_messages_per_prompt,
                )
                if m.kind != "system"
            ]
            is_first_poll = False

            if poll_count % 30 == 0:
                _log(f"heartbeat ({poll_count} iters, {len(messages)} pending)")

            if not messages:
                await _sleep_ms(POLL_INTERVAL_MS)
                continue

            if not any(m.trigger == 1 for m in messages):
                # 全是累积型 —— 保留 pending；等待 trigger=1。
                await _sleep_ms(POLL_INTERVAL_MS)
                continue

            batch_ids = [m.id for m in messages]
            mark_processing(batch_ids)

            routing = extract_routing(messages)

            # /clear 处理 —— 单命令短路。
            normal: list[MessageInRow] = []
            command_ids: list[str] = []
            for msg in messages:
                if is_clear_command(msg):
                    _log("clearing session history")
                    set_history([])
                    write_message_out(
                        id=_generate_id(),
                        kind="chat",
                        platform_id=routing.platform_id,
                        channel_type=routing.channel_type,
                        thread_id=routing.thread_id,
                        content=json.dumps({"text": "Session cleared."}),
                    )
                    command_ids.append(msg.id)
                    continue
                normal.append(msg)

            if command_ids:
                mark_completed(command_ids)
            if not normal:
                continue

            prompt_xml = format_messages(normal)

            history = get_history()
            ctx = SkillContext(
                channel_type=routing.channel_type,
                is_first_message=not _has_assistant_message(history),
            )
            auto, lazy = registry.resolve(ctx)
            skills_section = build_skills_prompt(auto, lazy)
            addendum = _addendum_prompt(config.assistant_name)
            system_prompt = _build_system_prompt(base_prompt, skills_section, addendum)

            history.append({"role": "user", "content": prompt_xml})
            history = await maybe_compress(history, client, config.model)
            turn_messages: list[dict] = (
                [{"role": "system", "content": system_prompt}] + history
            )

            final_text, turn_messages = await run_agent(
                turn_messages, mcp_manager, client, config.model
            )

            new_history = [m for m in turn_messages if m.get("role") != "system"]

            if final_text:
                sent, unwrapped = _dispatch_message_blocks(final_text, routing)
                if unwrapped:
                    names = ", ".join(d["name"] for d in list_destinations()) or "(none)"
                    nudge = (
                        f"<system>Your response was not delivered — it was not "
                        f"wrapped in <message to=\"name\">...</message> blocks. "
                        f"All output must be wrapped: use <message to=\"name\"> "
                        f"for content to send, or <internal> for scratchpad. "
                        f"Your destinations: {names}. "
                        f"Please re-send your response with the correct wrapping."
                        f"</system>"
                    )
                    new_history.append({"role": "user", "content": nudge})

            set_history(new_history)
            mark_completed(batch_ids)
            touch_heartbeat()

            if any(m.trigger == 1 for m in get_pending_messages(max_count=1)):
                await _sleep_ms(ACTIVE_POLL_INTERVAL_MS)
            else:
                await _sleep_ms(POLL_INTERVAL_MS)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            _log(f"iteration error: {e!r}")
            for mid in batch_ids:
                try:
                    mark_failed(mid)
                except Exception as inner:
                    _log(f"mark_failed({mid}) raised: {inner!r}")
            # 不要重新抛出 保持循环存活。
            await _sleep_ms(POLL_INTERVAL_MS)
        finally:
            if batch_ids:
                try:
                    from agent_runner.db.connection import open_outbound_db
                    db = open_outbound_db()
                    try:
                        db.executemany(
                            "UPDATE processing_ack SET status='completed', "
                            "status_changed=datetime('now') "
                            "WHERE message_id = ? AND status = 'processing'",
                            [(mid,) for mid in batch_ids],
                        )
                        db.commit()
                    finally:
                        db.close()
                except Exception as e:
                    _log(f"finally drain failed: {e!r}")
