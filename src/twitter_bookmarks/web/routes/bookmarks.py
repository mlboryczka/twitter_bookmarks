"""Bookmark browse and recategorize routes."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from twitter_bookmarks.classify.feedback import record_feedback
from twitter_bookmarks.db.models import (
    Author,
    Bookmark,
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

PAGE_SIZE = 25


def _bookmark_dict(row: Any, cat: Category | None) -> dict[str, Any]:
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
    excerpt = (text[:200] + "…") if text and len(text) > 200 else text
    return {
        "tweet_id": tid,
        "gist": gist,
        "sub_tags": sub_tags or [],
        "is_user_corrected": corrected,
        "text_excerpt": excerpt,
        "created_at": created_at,
        "author_username": username,
        "category_slug": cat_slug,
        "category_name": cat_name,
    }


@router.get("/bookmarks", response_class=HTMLResponse)
async def browse(
    request: Request,
    _user: AuthedUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    category: str | None = Query(default=None),
    sub_tag: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
) -> HTMLResponse:
    """List classified bookmarks; filter by category slug or sub-tag."""
    cats = await list_categories(session)
    by_slug = {c.slug: c for c in cats}

    stmt = (
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
        .join(Bookmark, Bookmark.tweet_id == Tweet.tweet_id)
        .join(Author, Author.author_id == Tweet.author_id)
        .join(Category, Category.id == BookmarkClassification.category_id)
        .order_by(desc(Bookmark.bookmarked_at))
    )
    if category:
        stmt = stmt.where(Category.slug == category)
    if sub_tag:
        stmt = stmt.where(BookmarkClassification.sub_tags.any(sub_tag))

    total = (
        await session.execute(
            select(func.count()).select_from(stmt.subquery())
        )
    ).scalar_one()

    stmt = stmt.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
    rows = (await session.execute(stmt)).all()
    bookmarks = [_bookmark_dict(r, by_slug.get(r[7])) for r in rows]

    return templates.TemplateResponse(
        request,
        "bookmarks/browse.html",
        {
            "bookmarks": bookmarks,
            "all_categories": cats,
            "category": category,
            "sub_tag": sub_tag,
            "page": page,
            "total": total,
            "page_size": PAGE_SIZE,
            "has_next": page * PAGE_SIZE < total,
            "has_prev": page > 1,
        },
    )


@router.get("/tags", response_class=HTMLResponse)
async def tags(
    request: Request,
    _user: AuthedUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    """Show all sub-tags with bookmark counts."""
    stmt = (
        select(
            func.unnest(BookmarkClassification.sub_tags).label("tag"),
            func.count().label("n"),
        )
        .group_by("tag")
        .order_by(desc("n"))
    )
    rows = (await session.execute(stmt)).all()
    return templates.TemplateResponse(
        request, "bookmarks/tags.html", {"tags": rows}
    )


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
    """Move a bookmark to a new category and record the correction."""
    target = (
        await session.execute(
            select(Category).where(Category.slug == category_slug)
        )
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(404, f"Unknown category slug: {category_slug}")

    await record_feedback(session, tweet_id, target.id)

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
    bookmark = _bookmark_dict(row, target)

    cats = await list_categories(session)
    html = templates.get_template("bookmarks/_card.html").render(
        bookmark=bookmark, all_categories=cats, request=request
    )
    return HTMLResponse(f'<div id="card-{tweet_id}">{html}</div>')
