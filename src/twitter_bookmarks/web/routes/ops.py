"""Health and cost-tracking dashboard routes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from twitter_bookmarks.db.models import (
    ApiCall,
    Bookmark,
    BookmarkClassification,
    ClassifierFeedback,
    Digest,
    WorkerState,
)
from twitter_bookmarks.db.session import get_session
from twitter_bookmarks.ingest.bookmark_worker import LAST_PULL_KEY
from twitter_bookmarks.setup_state import get_setup_phase
from twitter_bookmarks.web.auth import AuthedUser

router = APIRouter()
templates = Jinja2Templates(directory="src/twitter_bookmarks/web/templates")


async def _build_health(session: AsyncSession) -> dict[str, Any]:
    db_ok = True
    try:
        await session.execute(select(1))
    except SQLAlchemyError:
        db_ok = False

    phase = await get_setup_phase(session)
    bookmarks_total = (
        await session.execute(select(func.count()).select_from(Bookmark))
    ).scalar_one()
    classified_total = (
        await session.execute(
            select(func.count()).select_from(BookmarkClassification)
        )
    ).scalar_one()
    feedback_recent = (
        await session.execute(
            select(func.count()).select_from(ClassifierFeedback)
            .where(
                ClassifierFeedback.corrected_at
                > datetime.now(timezone.utc) - timedelta(days=30)
            )
        )
    ).scalar_one()
    last_pull = (
        await session.execute(
            select(WorkerState).where(WorkerState.key == LAST_PULL_KEY)
        )
    ).scalar_one_or_none()
    last_digest = (
        await session.execute(
            select(Digest).order_by(desc(Digest.composed_at)).limit(1)
        )
    ).scalar_one_or_none()

    cost_24h = (
        await session.execute(
            select(func.coalesce(func.sum(ApiCall.cost_usd), 0)).where(
                ApiCall.called_at > datetime.now(timezone.utc) - timedelta(hours=24)
            )
        )
    ).scalar_one()
    cost_total = (
        await session.execute(
            select(func.coalesce(func.sum(ApiCall.cost_usd), 0))
        )
    ).scalar_one()

    return {
        "db_ok": db_ok,
        "setup_phase": phase,
        "bookmarks_total": bookmarks_total,
        "classified_total": classified_total,
        "feedback_count_30d": feedback_recent,
        "last_pull": last_pull.value if last_pull else None,
        "last_digest_sent": last_digest.sent_at.isoformat()
        if last_digest and last_digest.sent_at
        else None,
        "cost_24h_usd": float(cost_24h),
        "cost_total_usd": float(cost_total),
    }


@router.get("/health")
async def health(
    request: Request,
    _user: AuthedUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    """Return a JSON health summary, or HTML if the client wants it."""
    payload = await _build_health(session)
    accept = request.headers.get("accept", "")
    if "text/html" in accept and "application/json" not in accept:
        return templates.TemplateResponse(
            request, "ops/health.html", {"health": payload}
        )
    return payload


@router.get("/health.html", response_class=HTMLResponse)
async def health_html(
    request: Request,
    _user: AuthedUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    payload = await _build_health(session)
    return templates.TemplateResponse(
        request, "ops/health.html", {"health": payload}
    )
