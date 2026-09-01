import sqlite3

import pytest

from precedent.adapters.storage.db import connect, init_db


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = connect(":memory:")
    init_db(connection)
    yield connection
    connection.close()
