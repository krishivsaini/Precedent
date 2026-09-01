EXPECTED_TABLES = {
    "payments", "bank_lines", "ledger_entries", "webhook_events", "exceptions",
    "resolutions", "precedents", "audit_log", "idempotency",
}


def test_all_tables_from_the_data_model_exist(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    table_names = {row["name"] for row in rows}
    assert EXPECTED_TABLES <= table_names


def test_foreign_keys_are_enforced(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_init_db_is_idempotent(conn):
    from precedent.adapters.storage.db import init_db

    init_db(conn)  # calling twice must not raise
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    assert {row["name"] for row in rows} >= EXPECTED_TABLES
