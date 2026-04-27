"""Weekly digest scheduler entry point."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from twitter_bookmarks.config import get_settings
from twitter_bookmarks.db.session import session_scope
from twitter_bookmarks.digest.composer import compose_digest
from twitter_bookmarks.digest.sender import send_digest
from twitter_bookmarks.setup_state import get_setup_phase

logger = logging.getLogger(__name__)


def _last_complete_week(now: datetime) -> tuple[date, date]:
    """Return (Monday, next Monday) covering the previous week.

    The digest is sent every Monday at 8am local time. For a 'Monday now',
    the previous week is [Monday-7, Monday). We use a half-open interval
    to make boundary semantics unambiguous.
    """
    today = now.date()
    # Move back to last Monday.
    weekday = today.weekday()  # Monday = 0
    days_since_monday = weekday
    last_monday = today - timedelta(days=days_since_monday)
    period_end = last_monday  # half-open: [start, period_end)
    period_start = period_end - timedelta(days=7)
    return period_start, period_end


async def run_weekly_digest_job() -> None:
    """Scheduler entry point: only fires when setup is complete."""
    settings = get_settings()
    async with session_scope() as session:
        phase = await get_setup_phase(session)
    if phase != "complete":
        logger.debug("Skipping weekly digest, setup_phase=%s", phase)
        return

    tz = ZoneInfo(settings.TIMEZONE)
    period_start, period_end = _last_complete_week(datetime.now(tz))
    logger.info(
        "Running weekly digest for %s → %s", period_start, period_end
    )

    async with session_scope() as session:
        digest = await compose_digest(session, period_start, period_end)
        await session.flush()
        digest_id = digest.id

    # Send in a separate transaction so a send failure doesn't roll back
    # the composition (we want failed digests preserved for retry).
    async with session_scope() as session:
        from sqlalchemy import select

        from twitter_bookmarks.db.models import Digest

        digest = (
            await session.execute(
                select(Digest).where(Digest.id == digest_id)
            )
        ).scalar_one()
        try:
            await send_digest(digest)
        except Exception:
            logger.exception("Digest %d send raised", digest_id)
