"""Application entrypoint: FastAPI app factory and lifecycle wiring."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import datetime as dt

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.middleware import SlowAPIMiddleware

from app import __version__
from app.api.routers import elder_care
from app.api.v1 import api_router
from app.config import settings
from app.core.handlers import register_exception_handlers
from app.core.limiter import limiter
from app.core.logging import configure_logging, get_logger
from app.core.responses import success_response
from app.database.connection import (
    close_mongo_connection,
    connect_to_mongo,
    init_database,
    ping,
)
from app.database.seed import run_seed
from app.services.scheduler import start_scheduler, stop_scheduler
from app.middleware import RequestContextMiddleware, SecurityHeadersMiddleware

logger = get_logger(__name__)

_DESCRIPTION = """
Backend API for **Srikrishna Seva Niketan**.

A production-grade, API-only backend powering the admin panel and the public
website: services, bookings, medical equipment rentals, careers, blogs, videos,
testimonials, FAQs, contact, settings/SEO, uploads, notifications and more.

All responses follow a consistent envelope::

    { "success": true, "message": "", "data": {}, "errors": null }
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup/shutdown: DB connect, Beanie init, seeding."""
    configure_logging(settings.DEBUG)
    logger.info("Starting application", extra={"env": settings.APP_ENV, "version": __version__})

    await connect_to_mongo()
    await init_database()
    # Run database seed and index verification in background tasks so the server
    # opens its port and responds to health checks immediately (<150ms).
    import asyncio
    from app.database.connection import ensure_indexes_background
    asyncio.create_task(run_seed())
    asyncio.create_task(ensure_indexes_background())
    start_scheduler()
    logger.info("Application startup complete")

    yield

    stop_scheduler()
    await close_mongo_connection()
    logger.info("Application shutdown complete")


def create_app() -> FastAPI:
    """Application factory."""
    app = FastAPI(
        title=settings.APP_NAME,
        description=_DESCRIPTION,
        version=__version__,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
        swagger_ui_parameters={"persistAuthorization": True},
    )

    # ---- Rate limiting ----
    app.state.limiter = limiter

    # ---- Middleware (order matters: last added runs first) ----
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_origin_regex=r"^https://.*\.vercel\.app$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Process-Time-ms"],
    )

    # ---- Exception handlers ----
    register_exception_handlers(app)

    # ---- Routes ----
    api_router.include_router(elder_care.router, prefix="/elder-care", tags=["elder-care"])
    app.include_router(api_router, prefix=settings.API_V1_PREFIX)

    @app.get("/", tags=["Health"], summary="Root")
    async def root() -> dict:
        return success_response(
            data={"name": settings.APP_NAME, "version": __version__, "docs": "/docs"},
            message="Srikrishna Seva Niketan API is running",
        )

    _HEALTH_HEADERS = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    async def _handle_health() -> JSONResponse:
        """Readiness probe checking database connectivity with bounded timeout (<=2.5s). Always returns 200."""
        db_ok = False
        try:
            db_ok = bool(await asyncio.wait_for(ping(), timeout=2.5))
        except Exception:
            db_ok = False

        status_str = "ok" if db_ok else "degraded"
        db_str = "connected" if db_ok else "disconnected"

        return JSONResponse(
            status_code=200,
            content={
                "status": status_str,
                "database": db_str,
                "version": __version__,
                "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
                "success": True,
                "message": "Service healthy" if db_ok else "Service degraded: database disconnected",
                "data": {
                    "status": status_str,
                    "database": db_str,
                    "version": __version__,
                },
                "errors": None,
            },
            headers=_HEALTH_HEADERS,
        )

    async def _handle_ping() -> JSONResponse:
        """Ultra-lightweight liveness probe (instant ~1ms, no DB access). Always returns 200."""
        return JSONResponse(
            status_code=200,
            content={
                "ping": "pong",
                "status": "ok",
                "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
                "success": True,
                "message": "pong",
                "data": {
                    "ping": "pong",
                    "status": "ok",
                },
                "errors": None,
            },
            headers=_HEALTH_HEADERS,
        )

    # 1. /health: Readiness probe checking database connectivity (strict <=2.5s timeout, always 200)
    @app.api_route(
        "/health",
        methods=["GET", "HEAD"],
        tags=["Health"],
        summary="Readiness probe with bounded database check",
    )
    @app.api_route(
        f"{settings.API_V1_PREFIX}/health",
        methods=["GET", "HEAD"],
        tags=["Health"],
        summary="Readiness probe (API v1 alias)",
    )
    @limiter.exempt
    async def health() -> JSONResponse:
        return await _handle_health()

    # 2. /ping: Ultra-lightweight liveness probe (instant ~1-2ms, no DB check, always 200)
    @app.api_route(
        "/ping",
        methods=["GET", "HEAD"],
        tags=["Health"],
        summary="Ultra-lightweight liveness probe",
    )
    @app.api_route(
        f"{settings.API_V1_PREFIX}/ping",
        methods=["GET", "HEAD"],
        tags=["Health"],
        summary="Ultra-lightweight liveness probe (API v1 alias)",
    )
    @limiter.exempt
    async def ping_endpoint() -> JSONResponse:
        return await _handle_ping()

    # Legacy /health/db compatibility (always 200, no-cache headers)
    @app.api_route(
        "/health/db",
        methods=["GET", "HEAD"],
        tags=["Health"],
        summary="Database health check",
    )
    @app.api_route(
        f"{settings.API_V1_PREFIX}/health/db",
        methods=["GET", "HEAD"],
        tags=["Health"],
        summary="Database health check (API v1 alias)",
    )
    @limiter.exempt
    async def health_db() -> JSONResponse:
        return await _handle_health()

    return app


app = create_app()
