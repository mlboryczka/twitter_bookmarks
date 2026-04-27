"""Entry point: starts the FastAPI app under uvicorn."""

from __future__ import annotations

import logging

import uvicorn

from twitter_bookmarks.config import get_settings


def main() -> None:
    """Run the FastAPI application via uvicorn."""
    settings = get_settings()
    logging.basicConfig(
        level=settings.LOG_LEVEL,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(
        "twitter_bookmarks.web.app:create_app",
        host=settings.APP_HOST,
        port=settings.APP_PORT,
        factory=True,
        log_level=settings.LOG_LEVEL.lower(),
    )


if __name__ == "__main__":
    main()
