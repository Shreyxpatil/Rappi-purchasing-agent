"""FastAPI app factory. Run with: uvicorn app.main:create_app --factory (no work happens at import time)."""

from fastapi import FastAPI

from app.api import router
from app.config import get_settings
from app.db import create_schema, make_engine, make_session_factory


def create_app(database_url: str | None = None) -> FastAPI:
    app = FastAPI(title="Rappi AI Purchasing Agent")
    engine = make_engine(database_url or get_settings().database_url)
    create_schema(engine)
    app.state.session_factory = make_session_factory(engine)
    app.include_router(router)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app

