from contextlib import asynccontextmanager

from fastapi import FastAPI

from precedent.adapters.storage.db import connect, init_db
from precedent.api.approvals import router as approvals_router
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
    app.include_router(webhooks_router)
    app.include_router(approvals_router)
    app.include_router(ui_router)
    return app


app = create_app()
