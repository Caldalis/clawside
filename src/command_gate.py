from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from src.db.connection import get_db, has_table


FILTERED_COMMANDS: set[str] = {
    "/help",
    "/login",
    "/logout",
    "/doctor",
    "/config",
    "/remote-control",
}

ADMIN_COMMANDS: set[str] = {
    "/clear",
    "/compact",
    "/context",
    "/cost",
    "/files",
}


@dataclass
class GateResult:
    action: str            # 'pass' | 'filter' | 'deny'（动作类型）
    command: str = ""      # 'deny' 时填充（以及 'filter' 时用于日志）


def gate_command(content_json: str, user_id: Optional[str], agent_group_id: str) -> GateResult:

    try:
        parsed = json.loads(content_json)
        text = (parsed.get("text") if isinstance(parsed, dict) else None) or ""
        text = text.strip()
    except (ValueError, TypeError):
        text = (content_json or "").strip()

    if not text.startswith("/"):
        return GateResult(action="pass")

    command = text.split()[0].lower() if text.split() else text.lower()

    if command in FILTERED_COMMANDS:
        return GateResult(action="filter", command=command)

    if command in ADMIN_COMMANDS:
        if _is_admin(user_id, agent_group_id):
            return GateResult(action="pass")
        return GateResult(action="deny", command=command)

    return GateResult(action="pass")


def _is_admin(user_id: Optional[str], agent_group_id: str) -> bool:

    if user_id is None:
        return False
    db = get_db()
    if not has_table(db, "user_roles"):
        return True
    row = db.execute(
        """
        SELECT 1 FROM user_roles
        WHERE user_id = ?
          AND role IN ('owner', 'admin')
          AND agent_group_id = ?
        LIMIT 1
        """,
        (user_id, agent_group_id),
    ).fetchone()
    return row is not None
