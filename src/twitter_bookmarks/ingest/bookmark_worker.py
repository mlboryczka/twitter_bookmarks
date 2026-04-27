"""Bookmark pull worker. Built out in Phase 3."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def run_bookmark_pull(full_backfill: bool = False) -> dict[str, int]:
    """Stub. Implemented in Phase 3."""
    raise NotImplementedError("bookmark_worker.run_bookmark_pull (Phase 3)")


async def run_incremental_pull_job() -> None:
    """Scheduler entry point. Stub until Phase 3."""
    raise NotImplementedError("bookmark_worker.run_incremental_pull_job (Phase 3)")
