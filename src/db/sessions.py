from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.db.connection import get_db


@dataclass
class Session:
    id: str
    agent_group_id: str
    messaging_group_id: Optional[str]
    thread_id: Optional[str]
    agent_provider: Optional[str]
    status: str
    container_status: str
    last_active: Optional[str]
    created_at: str


def _row_to_session(row) -> Session:
    return Session(
        id=row["id"],
        agent_group_id=row["agent_group_id"],
        messaging_group_id=row["messaging_group_id"],
        thread_id=row["thread_id"],
        agent_provider=row["agent_provider"],
        status=row["status"],
        container_status=row["container_status"],
        last_active=row["last_active"],
        created_at=row["created_at"],
    )


def create_session(session: Session) -> None:
    get_db().execute(
        """
        INSERT INTO sessions
          (id, agent_group_id, messaging_group_id, thread_id, agent_provider,
           status, container_status, last_active, created_at)
        VALUES
          (:id, :agent_group_id, :messaging_group_id, :thread_id, :agent_provider,
           :status, :container_status, :last_active, :created_at)
        """,
        {
            "id": session.id,
            "agent_group_id": session.agent_group_id,
            "messaging_group_id": session.messaging_group_id,
            "thread_id": session.thread_id,
            "agent_provider": session.agent_provider,
            "status": session.status,
            "container_status": session.container_status,
            "last_active": session.last_active,
            "created_at": session.created_at,
        },
    )
    get_db().commit()


def get_session(id: str) -> Optional[Session]:
    row = get_db().execute("SELECT * FROM sessions WHERE id = ?", (id,)).fetchone()
    return _row_to_session(row) if row else None


def find_session_for_agent(
    agent_group_id: str,
    messaging_group_id: str,
    thread_id: Optional[str],
) -> Optional[Session]:
    if thread_id is not None:
        row = get_db().execute(
            "SELECT * FROM sessions WHERE agent_group_id = ? "
            "AND messaging_group_id = ? AND thread_id = ? AND status = 'active'",
            (agent_group_id, messaging_group_id, thread_id),
        ).fetchone()
    else:
        row = get_db().execute(
            "SELECT * FROM sessions WHERE agent_group_id = ? "
            "AND messaging_group_id = ? AND thread_id IS NULL AND status = 'active'",
            (agent_group_id, messaging_group_id),
        ).fetchone()
    return _row_to_session(row) if row else None


def find_session_by_agent_group(agent_group_id: str) -> Optional[Session]:

    row = get_db().execute(
        "SELECT * FROM sessions WHERE agent_group_id = ? AND status = 'active' "
        "ORDER BY created_at DESC LIMIT 1",
        (agent_group_id,),
    ).fetchone()
    return _row_to_session(row) if row else None

def get_running_sessions() -> list[Session]:
    rows = get_db().execute(
        "SELECT * FROM sessions WHERE container_status IN ('running', 'idle')"
    ).fetchall()
    return [_row_to_session(r) for r in rows]


def get_active_sessions() -> list[Session]:
    rows = get_db().execute(
        "SELECT * FROM sessions WHERE status = 'active'"
    ).fetchall()
    return [_row_to_session(r) for r in rows]


def update_session(
    id: str,
    *,
    status: Optional[str] = None,
    container_status: Optional[str] = None,
    last_active: Optional[str] = None,
    agent_provider: Optional[str] = None,
) -> None:
    fields: list[str] = []
    values: dict = {"id": id}
    if status is not None:
        fields.append("status = :status")
        values["status"] = status
    if container_status is not None:
        fields.append("container_status = :container_status")
        values["container_status"] = container_status
    if last_active is not None:
        fields.append("last_active = :last_active")
        values["last_active"] = last_active
    if agent_provider is not None:
        fields.append("agent_provider = :agent_provider")
        values["agent_provider"] = agent_provider
    if not fields:
        return
    get_db().execute(
        f"UPDATE sessions SET {', '.join(fields)} WHERE id = :id", values
    )
    get_db().commit()
