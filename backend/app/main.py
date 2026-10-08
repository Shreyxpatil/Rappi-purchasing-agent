"""FastAPI app factory. Run with: uvicorn app.main:create_app --factory (no work happens at import time)."""

import logging

from fastapi import FastAPI

from app import runner

from app.api import router
from app.config import get_settings
from app.db import create_schema, make_engine, make_session_factory
from app.logs import configure_logging


def create_app(database_url: str | None = None) -> FastAPI:
    configure_logging()
    app = FastAPI(title="Rappi AI Purchasing Agent")
    engine = make_engine(database_url or get_settings().database_url)
    create_schema(engine)
    app.state.session_factory = make_session_factory(engine)
    with app.state.session_factory() as s:
        orphans = runner.fail_orphaned_runs(s)
    if orphans:
        logging.getLogger("app").warning("marked runs %s FAILED: their task died with the previous server", orphans)
    app.include_router(router)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app

