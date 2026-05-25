from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from src.db.connection import get_db


@dataclass
class User:
    id: str
    kind: str
    display_name: Optional[str]
    created_at: str


def _row_to_user(row) -> User:
    return User(
        id=row["id"],
        kind=row["kind"],
        display_name=row["display_name"],
        created_at=row["created_at"],
    )

def upsert_user(
    id: str,
    kind: str,
    display_name: Optional[str] = None,
) -> User:

    now = datetime.now(timezone.utc).isoformat()
    db = get_db()
    db.execute(
        """
        INSERT INTO users (id, kind, display_name, created_at)
        VALUES (:id, :kind, :display_name, :created_at)
        ON CONFLICT(id) DO UPDATE SET
          display_name = COALESCE(excluded.display_name, users.display_name)
        """,
        {"id": id, "kind": kind, "display_name": display_name, "created_at": now},
    )
    db.commit()
    return get_user(id)  # type: ignore[return-value]

def get_user(id: str) -> Optional[User]:
    row = get_db().execute("SELECT * FROM users WHERE id = ?", (id,)).fetchone()
    return _row_to_user(row) if row else None

def list_users() -> list[User]:
    rows = get_db().execute("SELECT * FROM users ORDER BY id").fetchall()
    return [_row_to_user(r) for r in rows]
