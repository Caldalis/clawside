from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from agent_runner.db.connection import open_outbound_db




def _next_odd_seq(db) -> int:
    row = db.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM messages_out").fetchone()
    max_seq = row["m"]
    if max_seq < 1:
        return 1
    if max_seq % 2 == 1:
        return max_seq + 2
    return max_seq + 1

def write_message_out(
    *,
    id: str,
    kind: str,
    platform_id: Optional[str],
    channel_type: Optional[str],
    thread_id: Optional[str],
    content: str,
    in_reply_to: Optional[str] = None,
    deliver_after: Optional[str] = None,
) -> int:
    """
    原子分配 seq
    """
    now = datetime.now(timezone.utc).isoformat()
    db = open_outbound_db()
    try:
        seq = _next_odd_seq(db)
        db.execute(
            """
            INSERT INTO messages_out (
              id, seq, in_reply_to, timestamp, deliver_after, recurrence,
              kind, platform_id, channel_type, thread_id, content
            ) VALUES (
              :id, :seq, :in_reply_to, :timestamp, :deliver_after, NULL,
              :kind, :platform_id, :channel_type, :thread_id, :content
            )
            """,
            {
                "id": id,
                "seq": seq,
                "in_reply_to": in_reply_to,
                "timestamp": now,
                "deliver_after": deliver_after,
                "kind": kind,
                "platform_id": platform_id,
                "channel_type": channel_type,
                "thread_id": thread_id,
                "content": content,
            },
        )
        db.commit()
        return seq
    finally:
        db.close()
