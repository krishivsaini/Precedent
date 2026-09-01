"""Connection management. SQLite chosen over Postgres — spec §3: provisioning cost buys
nothing at this volume, and a reviewer must be able to clone and run in ~60s (NFR-3)."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from precedent.adapters.storage.schema import SCHEMA_SQL


def connect(db_path: str | Path = ":memory:") -> sqlite3.Connection:
    """Open a connection with row access by column name and foreign keys enforced.

    `check_same_thread=False`: FastAPI may run a sync dependency in a worker thread
    different from the one that constructed it (notably under Starlette's TestClient,
    which proxies through an anyio thread portal). Each caller here still gets its own
    connection, opened and closed within a single request — this relaxation does not
    imply sharing one connection across concurrent requests or threads.
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables/indexes if they don't already exist. Safe to call repeatedly."""
    conn.executescript(SCHEMA_SQL)
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Commit on success, roll back on any exception."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
