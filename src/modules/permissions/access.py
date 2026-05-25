from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from src.channels.adapter import InboundEvent
from src.db.connection import get_db
from src.db.dropped_messages import record_dropped_message
from src.db.messaging_groups import MessagingGroup, MessagingGroupAgent
from src.db.users import upsert_user
from src.log import log
from src.router import (
    AccessGateResult,
    set_access_gate,
    set_sender_resolver,
    set_sender_scope_gate,
)


@dataclass
class AccessDecision:
    allowed: bool
    role: Optional[str]      # 'owner' | 'admin' | 'member' | None
    reason: str = ""


def can_access_agent_group(user_id: str, agent_group_id: str) -> AccessDecision:
    db = get_db()


    row = db.execute(
        """
        SELECT role FROM user_roles
        WHERE user_id = ? AND agent_group_id = ?
        ORDER BY CASE role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END
        LIMIT 1
        """,
        (user_id, agent_group_id),
    ).fetchone()
    if row is not None:
        return AccessDecision(allowed=True, role=row["role"])

    row = db.execute(
        "SELECT 1 FROM agent_group_members "
        "WHERE user_id = ? AND agent_group_id = ? LIMIT 1",
        (user_id, agent_group_id),
    ).fetchone()
    if row is not None:
        return AccessDecision(allowed=True, role="member")

    return AccessDecision(allowed=False, role=None, reason="not_authorized")


def _parse_sender(event: InboundEvent) -> tuple[Optional[str], Optional[str]]:

    try:
        parsed = json.loads(event.message.content)
    except (ValueError, TypeError):
        return None, None
    if not isinstance(parsed, dict):
        return None, None
    sid = parsed.get("sender_id")
    sname = parsed.get("sender")
    return (sid if isinstance(sid, str) and sid else None,
            sname if isinstance(sname, str) and sname else None)


def _sender_resolver(event: InboundEvent) -> Optional[str]:
    user_id, display_name = _parse_sender(event)
    if user_id is None:
        return None
    # upsert users 行，使后续的角色 + 访问查询能找到真实记录。
    kind = user_id.split(":", 1)[0] if ":" in user_id else event.channel_type
    upsert_user(user_id, kind=kind, display_name=display_name)
    return user_id


def _access_gate(
    event: InboundEvent,
    user_id: Optional[str],
    mg: MessagingGroup,
    agent_group_id: str,
) -> AccessGateResult:
    if user_id is None:
        record_dropped_message(
            event.channel_type, event.platform_id,
            user_id=None, sender_name=None,
            reason="unknown_sender",
            messaging_group_id=mg.id, agent_group_id=agent_group_id,
        )
        return AccessGateResult(allowed=False, reason="unknown_sender")
    decision = can_access_agent_group(user_id, agent_group_id)
    if not decision.allowed:
        record_dropped_message(
            event.channel_type, event.platform_id,
            user_id=user_id, sender_name=None,
            reason=decision.reason or "not_authorized",
            messaging_group_id=mg.id, agent_group_id=agent_group_id,
        )
        return AccessGateResult(allowed=False, reason=decision.reason)
    return AccessGateResult(allowed=True)

def _sender_scope_gate(
    event: InboundEvent,
    user_id: Optional[str],
    mg: MessagingGroup,
    agent: MessagingGroupAgent,
) -> AccessGateResult:
    if agent.sender_scope != "known":
        return AccessGateResult(allowed=True)
    if user_id is None:
        return AccessGateResult(allowed=False, reason="unknown_sender")
    # 'known' = 在该 group 上有 user_roles 行 或 agent_group_members 行。
    decision = can_access_agent_group(user_id, agent.agent_group_id)
    if decision.allowed:
        return AccessGateResult(allowed=True)
    return AccessGateResult(allowed=False, reason="not_known_to_group")

def install() -> None:

    set_sender_resolver(_sender_resolver)
    set_access_gate(_access_gate)
    set_sender_scope_gate(_sender_scope_gate)
    log.info("permissions_hooks_installed")




def grant_member(user_id: str, agent_group_id: str, added_by: str) -> None:
    get_db().execute(
        """
        INSERT OR IGNORE INTO agent_group_members
          (user_id, agent_group_id, added_by, added_at)
        VALUES (?, ?, ?, ?)
        """,
        (user_id, agent_group_id, added_by, datetime.now(timezone.utc).isoformat()),
    )
    get_db().commit()
