"""OrderFlow ASGI application factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.database import build_engine, build_session_factory
from app.models import Base
from app.routers import api, web

logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.auto_create_tables:
        Base.metadata.create_all(bind=app.state.engine)
    yield
    app.state.engine.dispose()


def create_app(database_url: str | None = None) -> FastAPI:
    application = FastAPI(
        title="OrderFlow API",
        description="A reliable order and inventory platform demonstrating transactional inventory, idempotent order creation, and a database-backed transactional outbox.",
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    application.state.engine = build_engine(database_url or settings.database_url)
    application.state.session_factory = build_session_factory(application.state.engine)
    application.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        same_site="lax",
        https_only=settings.session_https_only,
        max_age=60 * 60 * 12,
    )
    static_dir = Path(__file__).parent / "static"
    application.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    application.include_router(web.router)
    application.include_router(api.router)

    @application.get("/healthz", tags=["Operations"])
    def healthz():
        return {"status": "ok", "service": "orderflow"}

    @application.get("/readyz", tags=["Operations"])
    def readyz(request: Request):
        try:
            from sqlalchemy import text

            with request.app.state.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return {"status": "ready", "database": "ok"}
        except SQLAlchemyError:
            return JSONResponse({"status": "not_ready", "database": "unavailable"}, status_code=503)

    return application


app = create_app()
