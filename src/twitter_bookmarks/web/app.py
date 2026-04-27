"""FastAPI app factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession

from twitter_bookmarks.config import get_settings
from twitter_bookmarks.db.session import get_session
from twitter_bookmarks.scheduler import build_scheduler
from twitter_bookmarks.setup_state import get_setup_phase
from twitter_bookmarks.web.auth import AuthedUser
from twitter_bookmarks.web.routes import bookmarks as bookmarks_routes
from twitter_bookmarks.web.routes import digest as digest_routes
from twitter_bookmarks.web.routes import ops as ops_routes
from twitter_bookmarks.web.routes import setup as setup_routes

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):  # noqa: ARG001 — FastAPI signature
    settings = get_settings()
    logging.basicConfig(
        level=settings.LOG_LEVEL,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    scheduler = build_scheduler()
    scheduler.start()
    logger.info("Scheduler started")
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler shut down")


def create_app() -> FastAPI:
    """Construct and return the FastAPI application."""
    app = FastAPI(
        title="twitter_bookmarks",
        version="0.1.0",
        lifespan=_lifespan,
    )

    app.mount(
        "/static",
        StaticFiles(directory="src/twitter_bookmarks/web/static"),
        name="static",
    )

    app.include_router(setup_routes.router)
    app.include_router(bookmarks_routes.router)
    app.include_router(digest_routes.router)
    app.include_router(ops_routes.router)

    @app.get("/")
    async def index(
        _user: AuthedUser,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> RedirectResponse:
        """Route the user to the screen matching their current setup phase."""
        phase = await get_setup_phase(session)
        target = {
            "not_started": "/setup/welcome",
            "fetching_bookmarks": "/setup/fetching",
            "awaiting_taxonomy_review": "/setup/proposal",
            "classifying_backfill": "/setup/classifying",
            "awaiting_classification_review": "/setup/review",
            "complete": "/digest",
        }[phase]
        return RedirectResponse(target, status_code=303)

    return app
