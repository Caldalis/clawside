from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from src.db.connection import get_db


def list_destinations_for_group(agent_group_id: str) -> list[dict]:
    rows = get_db().execute(
        """
        SELECT name, display_name, type, channel_type, platform_id,
               target_agent_group_id, created_at
          FROM agent_destinations
         WHERE agent_group_id = ?
         ORDER BY name
        """,
        (agent_group_id,),
    ).fetchall()
    return [dict(r) for r in rows]

def to_inbound_destination_rows(rows: list[dict]) -> list[dict]:

    return [
        {
            "name": r["name"],
            "display_name": r.get("display_name"),
            "type": r["type"],
            "channel_type": r.get("channel_type"),
            "platform_id": r.get("platform_id"),
            "agent_group_id": r.get("target_agent_group_id"),
        }
        for r in rows
    ]


def add_destination(
    agent_group_id: str,
    name: str,
    type: str,
    *,
    channel_type: Optional[str] = None,
    platform_id: Optional[str] = None,
    target_agent_group_id: Optional[str] = None,
    display_name: Optional[str] = None,
) -> int:

    if type not in ("channel", "agent"):
        raise ValueError(f"type must be 'channel' or 'agent', got {type!r}")
    if type == "channel" and (channel_type is None or platform_id is None):
        raise ValueError("type='channel' requires channel_type and platform_id")
    if type == "agent" and target_agent_group_id is None:
        raise ValueError("type='agent' requires target_agent_group_id")

    now = datetime.now(timezone.utc).isoformat()
    cur = get_db().execute(
        """
        INSERT OR IGNORE INTO agent_destinations (
          agent_group_id, name, display_name, type,
          channel_type, platform_id, target_agent_group_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            agent_group_id,
            name,
            display_name,
            type,
            channel_type,
            platform_id,
            target_agent_group_id,
            now,
        ),
    )
    get_db().commit()
    return cur.rowcount

def remove_destination(agent_group_id: str, name: str) -> bool:
    cur = get_db().execute(
        "DELETE FROM agent_destinations WHERE agent_group_id = ? AND name = ?",
        (agent_group_id, name),
    )
    get_db().commit()
    return cur.rowcount > 0
