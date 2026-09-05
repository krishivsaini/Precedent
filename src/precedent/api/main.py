import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from precedent.adapters.storage.db import connect, init_db
from precedent.api.approvals import router as approvals_router
from precedent.api.knowledge import router as knowledge_router
from precedent.api.remediation import router as remediation_router
from precedent.api.remediation_ui import router as remediation_ui_router
from precedent.api.ui import router as ui_router
from precedent.api.webhooks import router as webhooks_router


def create_app(db_path: str = "precedent.db") -> FastAPI:
    """Build the app. Schema creation happens on startup, not here.

    `create_app` must stay free of I/O: a module-level `app = create_app()` is how
    uvicorn loads this, so any file touching done here would run on mere *import* —
    creating a stray `precedent.db` in whatever directory a test, script, or tooling
    import happened to run from.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = connect(app.state.db_path)
        try:
            init_db(conn)
        finally:
            conn.close()
        yield

    app = FastAPI(title="Precedent", lifespan=lifespan)
    app.state.db_path = db_path

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        """Liveness only — deliberately does not open the database.

        A health check that touches SQLite would take the container out of rotation for a
        transient lock, which is the one failure this app recovers from on its own.
        """
        return {"status": "ok"}

    app.include_router(webhooks_router)
    app.include_router(approvals_router)
    app.include_router(remediation_router)
    # The HTML routers go last: their paths are the broadest, and a JSON route must
    # never be shadowed by a screen that happens to share a prefix.
    app.include_router(ui_router)
    app.include_router(knowledge_router)
    app.include_router(remediation_ui_router)
    return app


#: The container mounts its database outside the image (see `deploy/`), so the path has to
#: come from the environment. The default keeps `uvicorn precedent.api.main:app` in a clone
#: behaving exactly as before — it is read here rather than inside `create_app` so the
#: function's default argument stays the contract the tests rely on.
app = create_app(os.environ.get("PRECEDENT_DB_PATH", "precedent.db"))
