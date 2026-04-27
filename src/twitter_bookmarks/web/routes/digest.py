"""Digest archive routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from twitter_bookmarks.db.models import Digest, DigestSection
from twitter_bookmarks.db.session import get_session
from twitter_bookmarks.web.auth import AuthedUser

router = APIRouter()
templates = Jinja2Templates(directory="src/twitter_bookmarks/web/templates")


@router.get("/digest", response_class=HTMLResponse)
async def archive(
    request: Request,
    _user: AuthedUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    rows = (
        await session.execute(
            select(Digest).order_by(desc(Digest.period_start))
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request, "digest/archive.html", {"digests": rows}
    )


@router.get("/digest/{digest_id}", response_class=HTMLResponse)
async def detail(
    request: Request,
    digest_id: int,
    _user: AuthedUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    digest = await session.get(Digest, digest_id)
    if digest is None:
        raise HTTPException(404, "Digest not found")
    sections = (
        await session.execute(
            select(DigestSection)
            .where(DigestSection.digest_id == digest_id)
            .order_by(DigestSection.id)
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request,
        "digest/detail.html",
        {"digest": digest, "sections": sections},
    )
