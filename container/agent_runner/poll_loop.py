
from __future__ import annotations

import asyncio
import contextlib
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
HEARTBEAT_INTERVAL_MS = 10_000
MAX_FORMAT_FIX_ATTEMPTS = 3

MESSAGE_BLOCK_RE = re.compile(
    r'<message\s+to="([^"]+)"\s*>([\s\S]*?)</message>'
)

def _log(msg: str) -> None:
    print(f"[poll-loop] {msg}", file=sys.stderr, flush=True)

def _generate_id() -> str:
    return f"msg-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"

def _has_assistant_message(history: list[dict]) -> bool:
    return any(m.get("role") == "assistant" for m in history)

def _origin_destination_name(routing: RoutingContext) -> Optional[str]:
    if not routing.channel_type or not routing.platform_id:
        return None
    for d in list_destinations():
        if (
            d.get("type") == "channel"
            and d.get("channel_type") == routing.channel_type
            and d.get("platform_id") == routing.platform_id
        ):
            return d.get("name")
    return None

def _addendum_prompt(assistant_name: str, origin_name: Optional[str] = None) -> str:
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
    reply_hint = (
        f'To reply in the current conversation, send to "{origin_name}".\n'
        if origin_name
        else ""
    )
    return (
        f"You are {assistant_name}.\n\n"
        "## Output contract (REQUIRED)\n"
        "Everything you want a user to see MUST be wrapped:\n"
        '  <message to="name">your reply here</message>\n'
        "Use <internal>...</internal> for private reasoning the user must NOT see.\n\n"
        "⚠️ Any text OUTSIDE a <message> block is NOT delivered — it is discarded as "
        "scratchpad. If you forget to wrap, the user receives silence.\n\n"
        f"{reply_hint}"
        f"Your destinations:\n{dest_block}"
    )

def _build_system_prompt(
    base_prompt: str, skills_section: str, addendum: str
) -> str:
    parts = [p for p in (base_prompt or "", skills_section or "", addendum or "") if p]
    return "\n\n".join(parts)

def _dispatch_message_blocks(text: str, routing: RoutingContext) -> tuple[int, str]:
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
    return sent, scratch


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


def _fallback_deliver(text: str, routing: RoutingContext) -> bool:
    if not text or not routing.channel_type or not routing.platform_id:
        return False
    if routing.channel_type == "agent":
        return False
    write_message_out(
        id=_generate_id(),
        kind="chat",
        platform_id=routing.platform_id,
        channel_type=routing.channel_type,
        thread_id=routing.thread_id,
        content=json.dumps({"text": text}),
        in_reply_to=routing.in_reply_to,
    )
    return True

async def _sleep_ms(ms: int) -> None:
    await asyncio.sleep(ms / 1000.0)

async def _heartbeat_loop(interval_ms: int) -> None:
    try:
        while True:
            touch_heartbeat()
            await asyncio.sleep(interval_ms / 1000.0)
    except asyncio.CancelledError:
        raise


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
        hb_task: Optional[asyncio.Task] = None
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

            hb_task = asyncio.create_task(_heartbeat_loop(HEARTBEAT_INTERVAL_MS))

            routing = extract_routing(messages)

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
            origin_name = _origin_destination_name(routing)
            addendum = _addendum_prompt(config.assistant_name, origin_name)
            system_prompt = _build_system_prompt(base_prompt, skills_section, addendum)

            history.append({"role": "user", "content": prompt_xml})
            history = await maybe_compress(history, client, config.model)
            turn_messages: list[dict] = (
                [{"role": "system", "content": system_prompt}] + history
            )

            dest_names = ", ".join(d["name"] for d in list_destinations()) or "(none)"
            prefix_len = len(turn_messages)
            segment_start = prefix_len
            attempt = 0

            while True:
                segment_start = len(turn_messages)
                final_text, turn_messages = await run_agent(
                    turn_messages, mcp_manager, client, config.model
                )
                if final_text is None:
                    break
                sent, scratch = _dispatch_message_blocks(final_text, routing)
                if sent > 0 or not scratch:
                    break

                attempt += 1
                if attempt > MAX_FORMAT_FIX_ATTEMPTS:
                    if _fallback_deliver(scratch, routing):
                        _log(
                            f"format-fix exhausted ({MAX_FORMAT_FIX_ATTEMPTS} tries); "
                            "fallback-delivered unwrapped reply to origin"
                        )
                    else:
                        _log("format-fix exhausted; no resolvable origin, reply dropped")
                    break

                to_hint = (
                    f'to="{origin_name}"' if origin_name else 'to="<one of your destinations>"'
                )
                turn_messages.append({
                    "role": "user",
                    "content": (
                        "<system>Your previous reply was NOT delivered to the user: it "
                        "was not wrapped in <message to=\"name\">...</message> blocks. "
                        f"Re-send the SAME content now, wrapped. To reply here use {to_hint}. "
                        f"Valid destinations: {dest_names}.</system>"
                    ),
                })

            kept = turn_messages[:prefix_len] + turn_messages[segment_start:]
            new_history = [m for m in kept if m.get("role") != "system"]

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
            if hb_task is not None:
                hb_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await hb_task
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
