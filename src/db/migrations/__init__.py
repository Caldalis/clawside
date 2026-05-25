from __future__ import annotations

import importlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

_m001 = importlib.import_module("src.db.migrations.001_initial")

@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up: Callable[[sqlite3.Connection], None]

MIGRATIONS: list[Migration] = [
    Migration(version=1, name="initial", up=_m001.up),
]

def run_migrations(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
          version INTEGER PRIMARY KEY,
          name    TEXT NOT NULL,
          applied TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_schema_version_name
          ON schema_version(name);
        """
    )
    db.commit()
    applied = {
        row["name"]
        for row in db.execute("SELECT name FROM schema_version").fetchall()
    }
    pending = [m for m in MIGRATIONS if m.name not in applied]
    if not pending:
        return
    for m in pending:
        try:
            m.up(db)
            now = datetime.now(timezone.utc).isoformat()
            next_version = db.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM schema_version"
            ).fetchone()["v"]
            db.execute(
                "INSERT INTO schema_version (version, name, applied) VALUES (?, ?, ?)",
                (next_version, m.name, now),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
