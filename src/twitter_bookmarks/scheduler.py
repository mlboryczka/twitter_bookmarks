"""APScheduler job registration for bookmark pulls and weekly digest."""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from twitter_bookmarks.config import get_settings

logger = logging.getLogger(__name__)


def build_scheduler() -> AsyncIOScheduler:
    """Construct and configure the application scheduler.

    Returns the scheduler with jobs registered but not yet started. The
    caller is responsible for calling `.start()` on application startup.

    Jobs only fire when `setup_phase == 'complete'` — see the job functions
    themselves, which check this guard before doing any work.
    """
    from twitter_bookmarks.digest.worker import run_weekly_digest_job
    from twitter_bookmarks.ingest.bookmark_worker import run_incremental_pull_job

    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone=settings.TIMEZONE)

    scheduler.add_job(
        run_incremental_pull_job,
        trigger=IntervalTrigger(seconds=settings.BOOKMARK_PULL_INTERVAL),
        id="incremental_bookmark_pull",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        run_weekly_digest_job,
        trigger=CronTrigger(
            day_of_week=settings.DIGEST_DAY_OF_WEEK,
            hour=settings.DIGEST_HOUR,
            minute=0,
            timezone=settings.TIMEZONE,
        ),
        id="weekly_digest",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    logger.info(
        "Scheduler configured: pull every %ds, digest cron %s %d:00 (%s)",
        settings.BOOKMARK_PULL_INTERVAL,
        settings.DIGEST_DAY_OF_WEEK,
        settings.DIGEST_HOUR,
        settings.TIMEZONE,
    )
    return scheduler
