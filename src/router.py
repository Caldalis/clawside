from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from src.channels.adapter import InboundEvent, get_adapter
from src.command_gate import gate_command
from src.db.agent_groups import get_agent_group
from src.db.dropped_messages import record_dropped_message
from src.db.messaging_groups import (
    MessagingGroup,
    MessagingGroupAgent,
    create_messaging_group,
    get_messaging_group_agents,
    get_messaging_group_with_agent_count,
)
from src.db.sessions import find_session_for_agent
from src.log import log
from src.session_manager import (
    resolve_session,
    write_outbound_direct,
    write_session_message,
)


@dataclass
class AccessGateResult:
    allowed: bool
    reason: str = ""


SenderResolverFn = Callable[[InboundEvent], Optional[str]]
AccessGateFn = Callable[[InboundEvent, Optional[str], MessagingGroup, str], AccessGateResult]
SenderScopeGateFn = Callable[[InboundEvent, Optional[str], MessagingGroup, MessagingGroupAgent], AccessGateResult]
MessageInterceptorFn = Callable[[InboundEvent], Awaitable[bool]]
ChannelRequestGateFn = Callable[[MessagingGroup, InboundEvent], Awaitable[None]]


_sender_resolver: Optional[SenderResolverFn] = None
_access_gate: Optional[AccessGateFn] = None
_sender_scope_gate: Optional[SenderScopeGateFn] = None
_message_interceptor: Optional[MessageInterceptorFn] = None
_channel_request_gate: Optional[ChannelRequestGateFn] = None


def set_sender_resolver(fn: SenderResolverFn) -> None:
    global _sender_resolver
    if _sender_resolver is not None:
        log.warn("sender_resolver_overwritten")
    _sender_resolver = fn


def set_access_gate(fn: AccessGateFn) -> None:
    global _access_gate
    if _access_gate is not None:
        log.warn("access_gate_overwritten")
    _access_gate = fn


def set_sender_scope_gate(fn: SenderScopeGateFn) -> None:
    global _sender_scope_gate
    if _sender_scope_gate is not None:
        log.warn("sender_scope_gate_overwritten")
    _sender_scope_gate = fn


def set_message_interceptor(fn: MessageInterceptorFn) -> None:
    global _message_interceptor
    _message_interceptor = fn


def set_channel_request_gate(fn: ChannelRequestGateFn) -> None:
    global _channel_request_gate
    if _channel_request_gate is not None:
        log.warn("channel_request_gate_overwritten")
    _channel_request_gate = fn

def reset_router_hooks_for_tests() -> None:
    global _sender_resolver, _access_gate, _sender_scope_gate
    global _message_interceptor, _channel_request_gate
    _sender_resolver = None
    _access_gate = None
    _sender_scope_gate = None
    _message_interceptor = None
    _channel_request_gate = None

def _gen_id() -> str:
    return f"msg-{int(time.time() * 1000)}-{''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=6))}"

def _safe_parse_content(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        return {"text": raw}
    except (ValueError, TypeError):
        return {"text": raw}

def message_id_for_agent(base_id: Optional[str], agent_group_id: str) -> str:

    base = base_id if base_id else _gen_id()
    return f"{base}:{agent_group_id}"


def evaluate_engage(
    agent: MessagingGroupAgent,
    text: str,
    is_mention: bool,
    mg: MessagingGroup,
    thread_id: Optional[str],
) -> bool:
    mode = agent.engage_mode
    if mode == "pattern":
        pat = agent.engage_pattern or "."
        if pat == ".":
            return True
        try:
            return re.search(pat, text) is not None
        except re.error:

            return True
    if mode == "mention":
        return is_mention
    if mode == "mention-sticky":
        if is_mention:
            return True
        if mg.is_group == 0:
            return False
        existing = find_session_for_agent(agent.agent_group_id, mg.id, thread_id)
        return existing is not None
    return False

async def route_inbound(event: InboundEvent) -> None:
    if _message_interceptor and await _message_interceptor(event):
        return

    adapter = get_adapter(event.channel_type)
    if adapter is not None and not adapter.supports_threads:
        event.thread_id = None

    is_mention = bool(event.message.is_mention)

    found = get_messaging_group_with_agent_count(event.channel_type, event.platform_id)
    if found is None:
        if not is_mention:
            return
        mg_id = f"mg-{int(time.time() * 1000)}-{''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=6))}"
        mg = MessagingGroup(
            id=mg_id,
            channel_type=event.channel_type,
            platform_id=event.platform_id,
            name=None,
            is_group=1 if event.message.is_group else 0,
            unknown_sender_policy="request_approval",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        create_messaging_group(mg)
        log.info(
            "auto_created_messaging_group",
            id=mg_id, channel_type=event.channel_type, platform_id=event.platform_id,
        )
        agent_count = 0
    else:
        mg = found["mg"]
        agent_count = int(found["agent_count"])

    if agent_count == 0:
        if not is_mention:
            return

        parsed = _safe_parse_content(event.message.content)
        record_dropped_message(
            event.channel_type,
            event.platform_id,
            user_id=None,
            sender_name=parsed.get("sender"),
            reason="no_agent_wired",
            messaging_group_id=mg.id,
            agent_group_id=None,
        )

        if _channel_request_gate is not None:
            try:
                await _channel_request_gate(mg, event)
            except Exception as e:
                log.error("channel_request_gate_threw", messaging_group_id=mg.id, err=str(e))
        else:
            log.warn(
                "message_dropped_no_wiring",
                messaging_group_id=mg.id,
                channel_type=event.channel_type, platform_id=event.platform_id,
            )
        return

    user_id: Optional[str] = _sender_resolver(event) if _sender_resolver is not None else None

    agents = get_messaging_group_agents(mg.id)
    parsed = _safe_parse_content(event.message.content)
    message_text = parsed.get("text") or ""

    engaged_count = 0
    accumulated_count = 0
    subscribed = False
    supports_threads = adapter is not None and adapter.supports_threads

    for agent in agents:
        agent_group = get_agent_group(agent.agent_group_id)
        if agent_group is None:
            continue

        engages = evaluate_engage(agent, message_text, is_mention, mg, event.thread_id)

        access_ok = True
        if engages and _access_gate is not None:
            access_ok = _access_gate(event, user_id, mg, agent.agent_group_id).allowed
        scope_ok = True
        if engages and _sender_scope_gate is not None:
            scope_ok = _sender_scope_gate(event, user_id, mg, agent).allowed

        if engages and access_ok and scope_ok:
            await _deliver_to_agent(
                agent, agent_group, mg, event, user_id, supports_threads, wake=True,
            )
            engaged_count += 1

            if (
                not subscribed
                and agent.engage_mode == "mention-sticky"
                and supports_threads
                and adapter is not None
                and event.thread_id is not None
                and mg.is_group != 0
            ):
                subscribed = True
                try:
                    await adapter.subscribe(event.platform_id, event.thread_id)
                except Exception as e:
                    log.warn(
                        "adapter_subscribe_failed",
                        channel_type=event.channel_type, thread_id=event.thread_id, err=str(e),
                    )
        elif (
            agent.ignored_message_policy == "accumulate"
            and not (engages and (not access_ok or not scope_ok))
        ):

            await _deliver_to_agent(
                agent, agent_group, mg, event, user_id, supports_threads, wake=False,
            )
            accumulated_count += 1
        else:
            log.debug(
                "message_not_engaged_for_agent",
                agent_group_id=agent.agent_group_id,
                engage_mode=agent.engage_mode,
                engages=engages, access_ok=access_ok, scope_ok=scope_ok,
            )

    if engaged_count + accumulated_count == 0:
        record_dropped_message(
            event.channel_type,
            event.platform_id,
            user_id=user_id,
            sender_name=parsed.get("sender"),
            reason="no_agent_engaged",
            messaging_group_id=mg.id,
            agent_group_id=None,
        )


async def _deliver_to_agent(
    agent: MessagingGroupAgent,
    agent_group,
    mg: MessagingGroup,
    event: InboundEvent,
    user_id: Optional[str],
    adapter_supports_threads: bool,
    wake: bool,
) -> None:
    effective_mode = agent.session_mode or "shared"
    if (
        adapter_supports_threads
        and effective_mode != "agent-shared"
        and mg.is_group != 0
    ):
        effective_mode = "per-thread"

    session, _created = resolve_session(
        agent.agent_group_id, mg.id, event.thread_id, effective_mode,
    )

    if event.reply_to is not None:
        addr_channel = event.reply_to.channel_type
        addr_platform = event.reply_to.platform_id
        addr_thread = event.reply_to.thread_id
    else:
        addr_channel = event.channel_type
        addr_platform = event.platform_id
        addr_thread = event.thread_id

    if event.message.kind in ("chat", "chat-sdk"):
        gate = gate_command(event.message.content, user_id, agent.agent_group_id)
        if gate.action == "filter":
            log.debug("filtered_command_dropped", agent_group_id=agent.agent_group_id)
            return
        if gate.action == "deny":
            write_outbound_direct(
                session.agent_group_id,
                session.id,
                {
                    "id": f"deny-{int(time.time() * 1000)}-{''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=6))}",
                    "kind": "chat",
                    "platform_id": addr_platform,
                    "channel_type": addr_channel,
                    "thread_id": addr_thread,
                    "content": json.dumps(
                        {"text": f"Permission denied: {gate.command} requires admin access."}
                    ),
                },
            )
            log.info(
                "admin_command_denied",
                command=gate.command, user_id=user_id, agent_group_id=agent.agent_group_id,
            )
            return

    write_session_message(
        session.agent_group_id,
        session.id,
        {
            "id": message_id_for_agent(event.message.id, agent.agent_group_id),
            "kind": event.message.kind,
            "timestamp": event.message.timestamp,
            "platform_id": addr_platform,
            "channel_type": addr_channel,
            "thread_id": addr_thread,
            "content": event.message.content,
            "trigger": 1 if wake else 0,
        },
    )

    log.info(
        "message_routed",
        session_id=session.id,
        agent_group=agent.agent_group_id,
        engage_mode=agent.engage_mode,
        kind=event.message.kind,
        user_id=user_id,
        wake=wake,
    )

    if wake:

        try:
            from src.container_runner import wake_container  # type: ignore
            await wake_container(session)
        except ImportError:
            log.debug("container_runner_not_available", session_id=session.id)
