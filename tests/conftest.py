import sqlite3

import pytest

from precedent.adapters.storage.db import connect, init_db


@pytest.fixture(autouse=True)
def _no_write_key(monkeypatch):
    """Keep the developer's own `.env` out of the test suite.

    `config.py` calls `load_dotenv()` at import, so a `PRECEDENT_WRITE_KEY` set for a real
    deployment lands in `os.environ` and the middleware gates every write — turning a
    correct local `.env` into thirty-three failures on an unchanged checkout. Tests must
    describe the code, not the machine they run on.

    Cleared rather than set: unset is the shipped default, so this is the configuration
    every other test should be written against. `tests/api/test_write_key.py` opts back in
    explicitly, which is the only place the key is the subject rather than the environment.
    """
    monkeypatch.delenv("PRECEDENT_WRITE_KEY", raising=False)


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = connect(":memory:")
    init_db(connection)
    yield connection
    connection.close()
