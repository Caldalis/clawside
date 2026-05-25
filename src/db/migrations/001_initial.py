
from __future__ import annotations

import sqlite3

from src.db.schema import CENTRAL_SCHEMA


def up(db: sqlite3.Connection) -> None:
    db.executescript(CENTRAL_SCHEMA)
