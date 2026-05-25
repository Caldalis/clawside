from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

from src.db.connection import get_db

CLI_LOCAL_USER_ID = "cli:local"

@dataclass
class AgentGroup:
    id: str
    name: str
    folder: str
    agent_provider: Optional[str]
    created_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _row_to_agent_group(row) -> AgentGroup:
    return AgentGroup(
        id=row["id"],
        name=row["name"],
        folder=row["folder"],
        agent_provider=row["agent_provider"],
        created_at=row["created_at"],
    )


def get_agent_group(id: str) -> Optional[AgentGroup]:
    row = get_db().execute("SELECT * FROM agent_groups WHERE id = ?", (id,)).fetchone()
    return _row_to_agent_group(row) if row else None

def get_agent_group_by_folder(folder: str) -> Optional[AgentGroup]:
    row = get_db().execute(
        "SELECT * FROM agent_groups WHERE folder = ?", (folder,)
    ).fetchone()
    return _row_to_agent_group(row) if row else None

def list_agent_groups() -> list[AgentGroup]:
    rows = get_db().execute("SELECT * FROM agent_groups ORDER BY name").fetchall()
    return [_row_to_agent_group(r) for r in rows]

def create_agent_group(group: AgentGroup) -> None:
    db = get_db()
    db.execute("BEGIN")
    try:
        db.execute(
            """
            INSERT INTO agent_groups (id, name, folder, agent_provider, created_at)
            VALUES (:id, :name, :folder, :agent_provider, :created_at)
            """,
            asdict(group),
        )

        db.execute(
            """
            INSERT OR IGNORE INTO user_roles
              (user_id, role, agent_group_id, granted_by, granted_at)
            VALUES (?, 'owner', ?, ?, ?)
            """,
            (CLI_LOCAL_USER_ID, group.id, CLI_LOCAL_USER_ID, _now()),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

def update_agent_group(
    id: str,
    *,
    name: Optional[str] = None,
    agent_provider: Optional[str] = None,
) -> None:
    fields: list[str] = []
    values: dict = {"id": id}
    if name is not None:
        fields.append("name = :name")
        values["name"] = name
    if agent_provider is not None:
        fields.append("agent_provider = :agent_provider")
        values["agent_provider"] = agent_provider
    if not fields:
        return
    get_db().execute(
        f"UPDATE agent_groups SET {', '.join(fields)} WHERE id = :id", values
    )
    get_db().commit()

def delete_agent_group(id: str) -> None:
    get_db().execute("DELETE FROM agent_groups WHERE id = ?", (id,))
    get_db().commit()
