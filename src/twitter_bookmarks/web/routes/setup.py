"""Setup wizard routes.

The wizard advances `setup_phase` from `not_started` → `fetching_bookmarks`
→ `awaiting_taxonomy_review` → `classifying_backfill` →
`awaiting_classification_review` → `complete`. The root `/` route
(registered in app.py) redirects to whichever screen matches the current
phase.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from twitter_bookmarks.classify.classifier import run_setup_classify_job
from twitter_bookmarks.db.models import (
    Author,
    Bookmark,
    BookmarkClassification,
    Category,
    Tweet,
    WorkerState,
)
from twitter_bookmarks.db.session import get_session, session_scope
from twitter_bookmarks.ingest.bookmark_worker import (
    LAST_PULL_KEY,
    run_setup_backfill_job,
)
from twitter_bookmarks.setup_state import (
    SetupPhase,
    get_setup_phase,
    set_setup_phase,
)
from twitter_bookmarks.taxonomy.finalizer import (
    CategoryEdit,
    finalize_taxonomy,
    list_categories,
)
from twitter_bookmarks.taxonomy.proposer import get_active_draft, propose_taxonomy
from twitter_bookmarks.web.auth import AuthedUser

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(
    directory="src/twitter_bookmarks/web/templates"
)


def _phase_to_path(phase: SetupPhase) -> str:
    return {
        "not_started": "/setup/welcome",
        "fetching_bookmarks": "/setup/fetching",
        "awaiting_taxonomy_review": "/setup/proposal",
        "classifying_backfill": "/setup/classifying",
        "awaiting_classification_review": "/setup/review",
        "complete": "/",
    }[phase]


@router.get("/setup/welcome", response_class=HTMLResponse)
async def welcome(
    request: Request,
    _user: AuthedUser,
) -> HTMLResponse:
    return templates.TemplateResponse(request, "setup/welcome.html", {})


@router.post("/setup/start")
async def start(
    background: BackgroundTasks,
    _user: AuthedUser,
) -> RedirectResponse:
    async with session_scope() as session:
        phase = await get_setup_phase(session)
        if phase == "not_started":
            await set_setup_phase(session, "fetching_bookmarks")
    background.add_task(_safe_run_backfill)
    return RedirectResponse("/setup/fetching", status_code=303)


async def _safe_run_backfill() -> None:
    try:
        await run_setup_backfill_job()
    except Exception:
        logger.exception("Setup backfill background task failed")


async def _safe_run_classify() -> None:
    try:
        await run_setup_classify_job()
    except Exception:
        logger.exception("Setup classification background task failed")


@router.get("/setup/fetching", response_class=HTMLResponse)
async def fetching(request: Request, _user: AuthedUser) -> HTMLResponse:
    return templates.TemplateResponse(request, "setup/fetching.html", {})


@router.get("/setup/classifying", response_class=HTMLResponse)
async def classifying(request: Request, _user: AuthedUser) -> HTMLResponse:
    return templates.TemplateResponse(request, "setup/classifying.html", {})


@router.get("/setup/status", response_class=HTMLResponse)
async def status(
    _user: AuthedUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    phase = await get_setup_phase(session)
    bookmark_count = (
        await session.execute(select(func.count()).select_from(Bookmark))
    ).scalar_one()
    classified_count = (
        await session.execute(
            select(func.count()).select_from(BookmarkClassification)
        )
    ).scalar_one()
    last_pull = (
        await session.execute(
            select(WorkerState).where(WorkerState.key == LAST_PULL_KEY)
        )
    ).scalar_one_or_none()

    target = _phase_to_path(phase)
    redirect_attr = (
        f' data-redirect="{target}"'
        if phase
        in (
            "awaiting_taxonomy_review",
            "awaiting_classification_review",
            "complete",
        )
        else ""
    )

    last_pull_summary = ""
    if last_pull and last_pull.value:
        v = last_pull.value
        last_pull_summary = (
            f"<p class='meta'>Last pull: {v.get('pages', 0)} pages, "
            f"{v.get('new_tweets', 0)} new tweets, "
            f"{v.get('threads_reconstructed', 0)} threads reconstructed</p>"
        )

    html = (
        f"<div{redirect_attr}>"
        f"<p><strong>Phase:</strong> {phase}</p>"
        f"<p>{bookmark_count} bookmarks ingested · {classified_count} classified</p>"
        f"{last_pull_summary}"
        "</div>"
    )
    return HTMLResponse(html)


@router.get("/setup/proposal", response_class=HTMLResponse)
async def proposal(
    request: Request,
    _user: AuthedUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    phase = await get_setup_phase(session)
    if phase != "awaiting_taxonomy_review":
        return RedirectResponse(_phase_to_path(phase), status_code=303)

    draft = await get_active_draft(session)
    if draft is None:
        # Generate one inline.
        await propose_taxonomy(session)
        await session.commit()
        draft = await get_active_draft(session)

    proposal_data = (draft.proposal_json if draft else {}) or {}
    cats = []
    for cat in proposal_data.get("categories", []):
        examples = await _fetch_examples(session, cat.get("example_tweet_ids", []))
        cats.append(
            {
                "slug": cat.get("slug", ""),
                "name": cat.get("name", ""),
                "description": cat.get("description", ""),
                "rationale": cat.get("rationale", ""),
                "examples": examples,
            }
        )

    return templates.TemplateResponse(
        request,
        "setup/proposal.html",
        {
            "proposal": {
                "categories": cats,
                "overall_rationale": proposal_data.get("overall_rationale", ""),
            }
        },
    )


async def _fetch_examples(
    session: AsyncSession, tweet_ids: list[str]
) -> list[dict[str, Any]]:
    if not tweet_ids:
        return []
    rows = (
        await session.execute(
            select(Tweet.tweet_id, Tweet.text, Author.username)
            .join(Author, Author.author_id == Tweet.author_id)
            .where(Tweet.tweet_id.in_(tweet_ids))
        )
    ).all()
    out = []
    for tid, text, username in rows:
        snippet = text[:200] + "…" if text and len(text) > 200 else text
        out.append({"tweet_id": tid, "text": snippet, "username": username})
    return out


@router.get("/setup/proposal/blank-row", response_class=HTMLResponse)
async def proposal_blank_row(
    request: Request,
    _user: AuthedUser,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "setup/_category_row.html", {"cat": None}
    )


@router.post("/setup/proposal/regenerate", response_class=HTMLResponse)
async def proposal_regenerate(
    _user: AuthedUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RedirectResponse:
    await propose_taxonomy(session)
    return RedirectResponse("/setup/proposal", status_code=303)


@router.post("/setup/proposal/finalize")
async def proposal_finalize(
    request: Request,
    background: BackgroundTasks,
    _user: AuthedUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RedirectResponse:
    form = await request.form()
    slugs = form.getlist("slug[]")
    names = form.getlist("name[]")
    descriptions = form.getlist("description[]")
    edits: list[CategoryEdit] = []
    for i, name in enumerate(names):
        edits.append(
            CategoryEdit(
                slug=slugs[i] if i < len(slugs) else "",
                name=name,
                description=descriptions[i] if i < len(descriptions) else "",
                sort_order=i,
            )
        )
    await finalize_taxonomy(session, edits)
    background.add_task(_safe_run_classify)
    return RedirectResponse("/setup/classifying", status_code=303)


@router.get("/setup/review", response_class=HTMLResponse)
async def review(
    request: Request,
    _user: AuthedUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    phase = await get_setup_phase(session)
    if phase != "awaiting_classification_review":
        return RedirectResponse(_phase_to_path(phase), status_code=303)

    cats = await list_categories(session)
    grouped: list[dict[str, Any]] = []
    for cat in cats:
        rows = (
            await session.execute(
                select(
                    BookmarkClassification.tweet_id,
                    BookmarkClassification.gist,
                    BookmarkClassification.sub_tags,
                    BookmarkClassification.is_user_corrected,
                    Tweet.text,
                    Tweet.created_at,
                    Author.username,
                )
                .join(Tweet, Tweet.tweet_id == BookmarkClassification.tweet_id)
                .join(Author, Author.author_id == Tweet.author_id)
                .where(BookmarkClassification.category_id == cat.id)
                .order_by(desc(Tweet.created_at))
            )
        ).all()
        bookmarks = [
            {
                "tweet_id": tid,
                "gist": gist,
                "sub_tags": sub_tags or [],
                "is_user_corrected": corrected,
                "text_excerpt": (text[:200] + "…") if text and len(text) > 200 else text,
                "created_at": created_at,
                "author_username": username,
                "category_slug": cat.slug,
                "category_name": cat.name,
            }
            for tid, gist, sub_tags, corrected, text, created_at, username in rows
        ]
        grouped.append(
            {
                "name": cat.name,
                "description": cat.description,
                "bookmarks": bookmarks,
            }
        )

    total = sum(len(c["bookmarks"]) for c in grouped)
    return templates.TemplateResponse(
        request,
        "setup/review.html",
        {
            "categories": grouped,
            "total": total,
            "all_categories": cats,
        },
    )


@router.post("/setup/complete")
async def complete(
    _user: AuthedUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RedirectResponse:
    await set_setup_phase(session, "complete")
    return RedirectResponse("/", status_code=303)
