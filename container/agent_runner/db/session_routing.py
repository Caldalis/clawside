from __future__ import annotations

from agent_runner.db.connection import open_inbound_db


def get_session_routing() -> dict:
    inbound = open_inbound_db()
    try:
        row = inbound.execute(
            "SELECT channel_type, platform_id, thread_id "
            "FROM session_routing WHERE id = 1"
        ).fetchone()
    finally:
        inbound.close()
    if row is None:
        return {"channel_type": None, "platform_id": None, "thread_id": None}
    return {
        "channel_type": row["channel_type"],
        "platform_id": row["platform_id"],
        "thread_id": row["thread_id"],
    }



def find_destination_name_for_session() -> str | None:
    """
    反查与 session_routing 匹配的 destination 名称
    """
    sr = get_session_routing()
    if not sr["channel_type"] or not sr["platform_id"]:
        return None

    inbound = open_inbound_db()
    try:
        row = inbound.execute(
            """
            SELECT name FROM destinations
             WHERE channel_type = ? AND platform_id = ? AND type = 'channel'
             LIMIT 1
            """,
            (sr["channel_type"], sr["platform_id"]),
        ).fetchone()
    finally:
        inbound.close()
    return row["name"] if row else None

def list_destinations() -> list[dict]:
    inbound = open_inbound_db()
    try:
        rows = inbound.execute(
            "SELECT name, display_name, type, channel_type, platform_id, "
            "agent_group_id FROM destinations ORDER BY name"
        ).fetchall()
    finally:
        inbound.close()
    return [dict(r) for r in rows]

def find_destination_by_name(name: str) -> dict | None:
    inbound = open_inbound_db()
    try:
        row = inbound.execute(
            "SELECT name, display_name, type, channel_type, platform_id, "
            "agent_group_id FROM destinations WHERE name = ?",
            (name,),
        ).fetchone()
    finally:
        inbound.close()
    return dict(row) if row else None
