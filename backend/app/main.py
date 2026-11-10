"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from app.api.auth import Authenticator, build_authenticator, get_principal
from app.api.errors import install_error_handlers
from app.api.middleware import RequestContextMiddleware
from app.api.routes import (
    audit,
    catalog,
    cluster_live,
    clusters,
    credentials,
    health,
    machines,
    network_storage,
    operations,
    provider_connections,
    system,
    workloads,
)
from app.core.config import AuthMode, Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import get_session_factory
from app.modules.organizations.service import ensure_default_tenancy, ensure_dev_principal
from app.orchestration.runtime import load_workflows

logger = logging.getLogger(__name__)


def _bootstrap(settings: Settings) -> Authenticator:
    with get_session_factory()() as session:
        tenancy = ensure_default_tenancy(session)
        dev_principal = ensure_dev_principal(session, tenancy) if settings.auth_mode is AuthMode.DEV else None
    return build_authenticator(settings, dev_principal)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    load_workflows()
    app.state.authenticator = await run_in_threadpool(_bootstrap, settings)
    if settings.auth_mode is AuthMode.DEV:
        logger.warning("AUTH_MODE=dev: all requests run as the local development administrator")
    logger.info("%s %s started (%s)", settings.app_name, settings.version, settings.environment.value)
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    app = FastAPI(
        title=f"{settings.app_name} API",
        version=settings.version,
        description=(
            "Infrastructure provisioning and Kubernetes management platform. "
            "All long-running work is performed by asynchronous operations; see /operations."
        ),
        lifespan=lifespan,
        docs_url=f"{settings.api_prefix}/docs",
        redoc_url=None,
        openapi_url=f"{settings.api_prefix}/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "Authorization", "Idempotency-Key", "X-Request-ID", "Last-Event-ID"],
    )
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)

    # Unauthenticated: health probes only.
    app.include_router(health.router, prefix=settings.api_prefix)

    protected = APIRouter(dependencies=[Depends(get_principal)])
    for module in (
        system,
        catalog,
        operations,
        credentials,
        machines,
        network_storage,
        provider_connections,
        cluster_live,
        workloads,
        clusters,
        audit,
    ):
        protected.include_router(module.router)
    app.include_router(protected, prefix=settings.api_prefix)
    return app


app = create_app()
