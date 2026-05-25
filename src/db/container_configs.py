from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from src.db.connection import get_db


@dataclass
class ContainerConfigRow:
    agent_group_id: str
    config: dict
    cli_scope: str       # 'disabled' | 'group' | 'global'（CLI 作用域）
    updated_at: str


def _row_to_config(row) -> ContainerConfigRow:
    return ContainerConfigRow(
        agent_group_id=row["agent_group_id"],
        config=json.loads(row["config"]) if row["config"] else {},
        cli_scope=row["cli_scope"],
        updated_at=row["updated_at"],
    )

def get_container_config(agent_group_id: str) -> Optional[ContainerConfigRow]:
    row = get_db().execute(
        "SELECT * FROM container_configs WHERE agent_group_id = ?",
        (agent_group_id,),
    ).fetchone()
    return _row_to_config(row) if row else None

def upsert_container_config(
    agent_group_id: str,
    config: dict,
    cli_scope: str = "group",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    db = get_db()
    db.execute(
        """
        INSERT INTO container_configs (agent_group_id, config, cli_scope, updated_at)
        VALUES (:agent_group_id, :config, :cli_scope, :updated_at)
        ON CONFLICT(agent_group_id) DO UPDATE SET
          config     = excluded.config,
          cli_scope  = excluded.cli_scope,
          updated_at = excluded.updated_at
        """,
        {
            "agent_group_id": agent_group_id,
            "config": json.dumps(config),
            "cli_scope": cli_scope,
            "updated_at": now,
        },
    )
    db.commit()


def update_container_config(
    agent_group_id: str,
    *,
    config: Optional[dict] = None,
    cli_scope: Optional[str] = None,
) -> None:

    fields: list[str] = []
    values: dict = {"agent_group_id": agent_group_id}
    if config is not None:
        fields.append("config = :config")
        values["config"] = json.dumps(config)
    if cli_scope is not None:
        fields.append("cli_scope = :cli_scope")
        values["cli_scope"] = cli_scope
    if not fields:
        return
    fields.append("updated_at = :updated_at")
    values["updated_at"] = datetime.now(timezone.utc).isoformat()
    get_db().execute(
        f"UPDATE container_configs SET {', '.join(fields)} "
        f"WHERE agent_group_id = :agent_group_id",
        values,
    )
    get_db().commit()
