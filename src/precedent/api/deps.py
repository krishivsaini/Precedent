import sqlite3
from typing import Iterator

from fastapi import Request

from precedent.adapters.storage.db import connect


def get_connection(request: Request) -> Iterator[sqlite3.Connection]:
    """One connection per request, and one transaction per request.

    Repositories never commit (see `adapters.storage.repositories`), so the request is
    the transaction boundary: everything a handler writes commits together when the
    handler returns, or rolls back entirely if it raises. A handler that writes several
    rows can't leave half of them behind.
    """
    conn = connect(request.app.state.db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
