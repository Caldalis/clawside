from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from src.db.connection import get_db


def record_dropped_message(
    channel_type: str,
    platform_id: str,
    *,
    user_id: Optional[str],
    sender_name: Optional[str],
    reason: str,
    messaging_group_id: Optional[str],
    agent_group_id: Optional[str],
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    db = get_db()
    db.execute(
        """
        INSERT INTO dropped_messages (
          channel_type, platform_id, user_id, sender_name, reason,
          messaging_group_id, agent_group_id, message_count, first_seen, last_seen
        ) VALUES (
          :channel_type, :platform_id, :user_id, :sender_name, :reason,
          :messaging_group_id, :agent_group_id, 1, :now, :now
        )
        ON CONFLICT (channel_type, platform_id) DO UPDATE SET
          user_id            = COALESCE(excluded.user_id, dropped_messages.user_id),
          sender_name        = COALESCE(excluded.sender_name, dropped_messages.sender_name),
          reason             = excluded.reason,
          messaging_group_id = COALESCE(excluded.messaging_group_id, dropped_messages.messaging_group_id),
          agent_group_id     = COALESCE(excluded.agent_group_id, dropped_messages.agent_group_id),
          message_count      = dropped_messages.message_count + 1,
          last_seen          = excluded.last_seen
        """,
        {
            "channel_type": channel_type,
            "platform_id": platform_id,
            "user_id": user_id,
            "sender_name": sender_name,
            "reason": reason,
            "messaging_group_id": messaging_group_id,
            "agent_group_id": agent_group_id,
            "now": now,
        },
    )
    db.commit()

def list_dropped_messages(limit: int = 50) -> list[dict]:
    rows = get_db().execute(
        "SELECT * FROM dropped_messages ORDER BY last_seen DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
