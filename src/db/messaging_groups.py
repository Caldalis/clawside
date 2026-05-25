from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.db.connection import get_db


@dataclass
class MessagingGroup:
    id: str
    channel_type: str
    platform_id: str
    name: Optional[str]
    is_group: int
    unknown_sender_policy: str
    created_at: str


@dataclass
class MessagingGroupAgent:
    id: str
    messaging_group_id: str
    agent_group_id: str
    engage_mode: str             # 'pattern' | 'mention' | 'mention-sticky'（接入模式）
    engage_pattern: Optional[str]
    sender_scope: str            # 'all' | 'known'（发送者范围）
    ignored_message_policy: str  # 'drop' | 'accumulate'（忽略消息策略）
    session_mode: Optional[str]
    priority: int
    created_at: str


def _row_to_mg(row) -> MessagingGroup:
    return MessagingGroup(
        id=row["id"],
        channel_type=row["channel_type"],
        platform_id=row["platform_id"],
        name=row["name"],
        is_group=row["is_group"],
        unknown_sender_policy=row["unknown_sender_policy"],
        created_at=row["created_at"],
    )


def _row_to_mga(row) -> MessagingGroupAgent:
    return MessagingGroupAgent(
        id=row["id"],
        messaging_group_id=row["messaging_group_id"],
        agent_group_id=row["agent_group_id"],
        engage_mode=row["engage_mode"],
        engage_pattern=row["engage_pattern"],
        sender_scope=row["sender_scope"],
        ignored_message_policy=row["ignored_message_policy"],
        session_mode=row["session_mode"],
        priority=row["priority"],
        created_at=row["created_at"],
    )


def get_messaging_group(id: str) -> Optional[MessagingGroup]:
    row = get_db().execute(
        "SELECT * FROM messaging_groups WHERE id = ?", (id,)
    ).fetchone()
    return _row_to_mg(row) if row else None

def get_messaging_group_by_platform(
    channel_type: str, platform_id: str
) -> Optional[MessagingGroup]:
    row = get_db().execute(
        "SELECT * FROM messaging_groups WHERE channel_type = ? AND platform_id = ?",
        (channel_type, platform_id),
    ).fetchone()
    return _row_to_mg(row) if row else None

def get_messaging_group_with_agent_count(
    channel_type: str, platform_id: str
) -> Optional[dict]:
    row = get_db().execute(
        """
        SELECT mg.*, COUNT(mga.id) AS agent_count
          FROM messaging_groups mg
     LEFT JOIN messaging_group_agents mga ON mga.messaging_group_id = mg.id
         WHERE mg.channel_type = ? AND mg.platform_id = ?
      GROUP BY mg.id
        """,
        (channel_type, platform_id),
    ).fetchone()
    if row is None:
        return None
    mg = MessagingGroup(
        id=row["id"],
        channel_type=row["channel_type"],
        platform_id=row["platform_id"],
        name=row["name"],
        is_group=row["is_group"],
        unknown_sender_policy=row["unknown_sender_policy"],
        created_at=row["created_at"],
    )
    return {"mg": mg, "agent_count": row["agent_count"]}

def list_messaging_groups() -> list[MessagingGroup]:
    rows = get_db().execute(
        "SELECT * FROM messaging_groups ORDER BY name"
    ).fetchall()
    return [_row_to_mg(r) for r in rows]

def create_messaging_group(mg: MessagingGroup) -> None:
    get_db().execute(
        """
        INSERT INTO messaging_groups
          (id, channel_type, platform_id, name, is_group, unknown_sender_policy, created_at)
        VALUES
          (:id, :channel_type, :platform_id, :name, :is_group, :unknown_sender_policy, :created_at)
        """,
        {
            "id": mg.id,
            "channel_type": mg.channel_type,
            "platform_id": mg.platform_id,
            "name": mg.name,
            "is_group": mg.is_group,
            "unknown_sender_policy": mg.unknown_sender_policy,
            "created_at": mg.created_at,
        },
    )
    get_db().commit()



def update_messaging_group(
    id: str,
    *,
    name: Optional[str] = None,
    is_group: Optional[int] = None,
    unknown_sender_policy: Optional[str] = None,
) -> None:
    fields: list[str] = []
    values: dict = {"id": id}
    if name is not None:
        fields.append("name = :name")
        values["name"] = name
    if is_group is not None:
        fields.append("is_group = :is_group")
        values["is_group"] = is_group
    if unknown_sender_policy is not None:
        fields.append("unknown_sender_policy = :unknown_sender_policy")
        values["unknown_sender_policy"] = unknown_sender_policy
    if not fields:
        return
    get_db().execute(
        f"UPDATE messaging_groups SET {', '.join(fields)} WHERE id = :id", values
    )
    get_db().commit()


def delete_messaging_group(id: str) -> None:
    get_db().execute("DELETE FROM messaging_groups WHERE id = ?", (id,))
    get_db().commit()

def get_messaging_group_agents(messaging_group_id: str) -> list[MessagingGroupAgent]:
    rows = get_db().execute(
        "SELECT * FROM messaging_group_agents WHERE messaging_group_id = ? "
        "ORDER BY priority DESC",
        (messaging_group_id,),
    ).fetchall()
    return [_row_to_mga(r) for r in rows]

def create_messaging_group_agent(mga: MessagingGroupAgent) -> None:
    get_db().execute(
        """
        INSERT INTO messaging_group_agents (
          id, messaging_group_id, agent_group_id,
          engage_mode, engage_pattern, sender_scope, ignored_message_policy,
          session_mode, priority, created_at
        ) VALUES (
          :id, :messaging_group_id, :agent_group_id,
          :engage_mode, :engage_pattern, :sender_scope, :ignored_message_policy,
          :session_mode, :priority, :created_at
        )
        """,
        {
            "id": mga.id,
            "messaging_group_id": mga.messaging_group_id,
            "agent_group_id": mga.agent_group_id,
            "engage_mode": mga.engage_mode,
            "engage_pattern": mga.engage_pattern,
            "sender_scope": mga.sender_scope,
            "ignored_message_policy": mga.ignored_message_policy,
            "session_mode": mga.session_mode,
            "priority": mga.priority,
            "created_at": mga.created_at,
        },
    )
    get_db().commit()

def delete_messaging_group_agent(id: str) -> None:
    get_db().execute("DELETE FROM messaging_group_agents WHERE id = ?", (id,))
    get_db().commit()
