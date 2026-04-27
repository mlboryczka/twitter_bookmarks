"""Bookmark browse and recategorize routes. Built out in Phase 7.

The recategorize endpoint is needed by the setup wizard (Phase 6) so it
ships here so the setup-review screen's HTMX dropdowns work.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from twitter_bookmarks.classify.feedback import record_feedback
from twitter_bookmarks.db.models import (
    Author,
    BookmarkClassification,
    Category,
    Tweet,
)
from twitter_bookmarks.db.session import get_session
from twitter_bookmarks.taxonomy.finalizer import list_categories
from twitter_bookmarks.web.auth import AuthedUser

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="src/twitter_bookmarks/web/templates")


@router.post(
    "/bookmarks/{tweet_id}/recategorize", response_class=HTMLResponse
)
async def recategorize(
    request: Request,
    tweet_id: str,
    category_slug: Annotated[str, Form()],
    _user: AuthedUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    """Move a bookmark to a new category and record the correction.

    Returns the refreshed `_card.html` partial so HTMX can swap it in.
    """
    target = (
        await session.execute(
            select(Category).where(Category.slug == category_slug)
        )
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(404, f"Unknown category slug: {category_slug}")

    await record_feedback(session, tweet_id, target.id)

    # Re-render the card.
    row = (
        await session.execute(
            select(
                BookmarkClassification.tweet_id,
                BookmarkClassification.gist,
                BookmarkClassification.sub_tags,
                BookmarkClassification.is_user_corrected,
                Tweet.text,
                Tweet.created_at,
                Author.username,
                Category.slug,
                Category.name,
            )
            .join(Tweet, Tweet.tweet_id == BookmarkClassification.tweet_id)
            .join(Author, Author.author_id == Tweet.author_id)
            .join(Category, Category.id == BookmarkClassification.category_id)
            .where(BookmarkClassification.tweet_id == tweet_id)
        )
    ).first()
    if row is None:
        raise HTTPException(404, f"Bookmark {tweet_id} not found")
    (
        tid,
        gist,
        sub_tags,
        corrected,
        text,
        created_at,
        username,
        cat_slug,
        cat_name,
    ) = row
    bookmark = {
        "tweet_id": tid,
        "gist": gist,
        "sub_tags": sub_tags or [],
        "is_user_corrected": corrected,
        "text_excerpt": (text[:200] + "…") if text and len(text) > 200 else text,
        "created_at": created_at,
        "author_username": username,
        "category_slug": cat_slug,
        "category_name": cat_name,
    }

    cats = await list_categories(session)
    html = templates.get_template("bookmarks/_card.html").render(
        bookmark=bookmark, all_categories=cats, request=request
    )
    return HTMLResponse(f'<div id="card-{tweet_id}">{html}</div>')
