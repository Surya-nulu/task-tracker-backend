import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.database import engine
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.routers import auth, health

settings = get_settings()
log = logging.getLogger(__name__)

OPENAPI_TAGS = [
    {"name": "health", "description": "Liveness and dependency checks."},
    {"name": "auth", "description": "Registration, login and the current user."},
    # auth / tasks / alarms tags arrive in Phases 3-5.
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log.info("startup complete (environment=%s)", settings.ENVIRONMENT)
    yield  # Phase 12 starts the Redis subscriber task here.
    log.info("shutting down")
    await engine.dispose()


def create_app() -> FastAPI:
    configure_logging()

    app = FastAPI(
        title="Task & Alarm API",
        version="0.3.0",
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
        # Swagger is dev-only; Phase 18 hardens this further.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)
    app.include_router(health.router, prefix=settings.API_PREFIX)
    app.include_router(auth.router, prefix=settings.API_PREFIX)

    return app


app = create_app()