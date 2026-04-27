"""FastAPI app factory. Built out across phases 6-8."""

from __future__ import annotations

import logging

from fastapi import FastAPI

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Construct the FastAPI application.

    Routes and lifespan hooks (scheduler startup/shutdown) are wired up in
    later phases.
    """
    app = FastAPI(title="twitter_bookmarks", version="0.1.0")
    return app
