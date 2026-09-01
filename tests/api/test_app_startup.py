import os
import subprocess
import sys
import tempfile


def test_importing_the_app_module_creates_no_database_file():
    # Regression: `create_app()` used to call `init_db` directly, so merely importing
    # `precedent.api.main` (uvicorn's entrypoint, but also any test or tooling import)
    # created a stray precedent.db in whatever directory happened to be the CWD.
    workdir = tempfile.mkdtemp()
    result = subprocess.run(
        [sys.executable, "-c", "import precedent.api.main"],
        cwd=workdir, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert os.listdir(workdir) == [], f"import created files: {os.listdir(workdir)}"


def test_schema_is_created_on_startup(tmp_path):
    from fastapi.testclient import TestClient

    from precedent.adapters.storage.db import connect
    from precedent.api.main import create_app

    db_path = str(tmp_path / "startup.db")
    app = create_app(db_path=db_path)

    with TestClient(app):
        conn = connect(db_path)
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        conn.close()

    assert "webhook_events" in tables
    assert "precedents" in tables
