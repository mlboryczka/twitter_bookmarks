"""Setup wizard routes.

The wizard advances `setup_phase` from `not_started` → `fetching_bookmarks`
→ `awaiting_taxonomy_review` → `complete`. Sonnet's proposal both proposes
the taxonomy and classifies every shown bookmark in one call, so the user
reviews the full grouping (with per-bookmark move/feedback) on the
proposal page before locking. There's no separate Haiku run before
finalize.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from twitter_bookmarks.db.models import (
    Author,
    Bookmark,
    BookmarkClassification,
    BookmarkThread,
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
)
from twitter_bookmarks.taxonomy.proposer import (
    TaxonomyProposalPayload,
    apply_move,
    get_active_draft,
    propose_taxonomy,
)
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
        "classifying_backfill": "/setup/proposal",
        "awaiting_classification_review": "/setup/proposal",
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


@router.get("/setup/fetching", response_class=HTMLResponse)
async def fetching(request: Request, _user: AuthedUser) -> HTMLResponse:
    return templates.TemplateResponse(request, "setup/fetching.html", {})


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

    # Use HTMX out-of-band redirect via header — the inner-div approach we
    # had before missed the data-redirect attribute on the swap target.
    headers = {}
    if phase in ("awaiting_taxonomy_review", "complete"):
        headers["HX-Redirect"] = target

    html = (
        f"<div{redirect_attr}>"
        f"<p><strong>Phase:</strong> {phase}</p>"
        f"<p>{bookmark_count} bookmarks ingested · {classified_count} classified</p>"
        f"{last_pull_summary}"
        "</div>"
    )
    return HTMLResponse(html, headers=headers)


async def _proposal_view_data(
    session: AsyncSession,
    payload_dict: dict[str, Any],
) -> dict[str, Any]:
    """Hydrate a proposal_json dict with author + tweet text for rendering."""
    cats = payload_dict.get("categories") or []
    all_tweet_ids = [
        bm.get("tweet_id")
        for cat in cats
        for bm in cat.get("bookmarks", [])
        if bm.get("tweet_id")
    ]
    text_lookup: dict[str, dict[str, Any]] = {}
    if all_tweet_ids:
        rows = (
            await session.execute(
                select(
                    Tweet.tweet_id,
                    Tweet.text,
                    Tweet.created_at,
                    Author.username,
                    BookmarkThread.full_thread_text,
                )
                .join(Author, Author.author_id == Tweet.author_id)
                .outerjoin(
                    BookmarkThread,
                    BookmarkThread.bookmark_tweet_id == Tweet.tweet_id,
                )
                .where(Tweet.tweet_id.in_(all_tweet_ids))
            )
        ).all()
        for tid, text, created_at, username, thread_text in rows:
            full = thread_text or text or ""
            excerpt = full.strip()
            if len(excerpt) > 280:
                excerpt = excerpt[:280].rstrip() + "…"
            text_lookup[tid] = {
                "text_excerpt": excerpt,
                "created_at": created_at,
                "author_username": username,
            }

    rendered_cats = []
    for cat in cats:
        bookmarks = []
        for bm in cat.get("bookmarks", []):
            tid = bm.get("tweet_id")
            meta = text_lookup.get(tid, {})
            bookmarks.append(
                {
                    "tweet_id": tid,
                    "gist": bm.get("gist") or "",
                    "text_excerpt": meta.get("text_excerpt", ""),
                    "created_at": meta.get("created_at"),
                    "author_username": meta.get("author_username", "unknown"),
                }
            )
        rendered_cats.append(
            {
                "slug": cat.get("slug", ""),
                "name": cat.get("name", ""),
                "description": cat.get("description", ""),
                "rationale": cat.get("rationale", ""),
                "bookmarks": bookmarks,
            }
        )
    return {
        "categories": rendered_cats,
        "overall_rationale": payload_dict.get("overall_rationale", ""),
        "total_bookmarks": sum(len(c["bookmarks"]) for c in rendered_cats),
    }


@router.get("/setup/proposal", response_class=HTMLResponse)
async def proposal(
    request: Request,
    _user: AuthedUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    phase = await get_setup_phase(session)
    if phase == "complete":
        return RedirectResponse("/", status_code=303)
    if phase in ("not_started", "fetching_bookmarks"):
        return RedirectResponse(_phase_to_path(phase), status_code=303)

    draft = await get_active_draft(session)
    if draft is None:
        await propose_taxonomy(session)
        await session.commit()
        draft = await get_active_draft(session)

    proposal_dict = (draft.proposal_json if draft else {}) or {}
    view = await _proposal_view_data(session, proposal_dict)

    return templates.TemplateResponse(
        request,
        "setup/proposal.html",
        {"proposal": view},
    )


@router.post("/setup/proposal/regenerate")
async def proposal_regenerate(
    _user: AuthedUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RedirectResponse:
    await propose_taxonomy(session)
    return RedirectResponse("/setup/proposal", status_code=303)


@router.post("/setup/proposal/move", response_class=HTMLResponse)
async def proposal_move(
    request: Request,
    _user: AuthedUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    tweet_id: Annotated[str, Form()],
    to_slug: Annotated[str, Form()],
    feedback: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Move a bookmark to a different category. Updates proposal_json in DB."""
    if not to_slug.strip():
        return HTMLResponse("", status_code=204)

    draft = await get_active_draft(session)
    if draft is None:
        return HTMLResponse("No active draft", status_code=400)

    payload = TaxonomyProposalPayload.model_validate(draft.proposal_json or {})
    try:
        result = apply_move(payload, tweet_id, to_slug, feedback or None)
    except ValueError as exc:
        return HTMLResponse(f"Invalid move: {exc}", status_code=400)
    if result is None:
        return HTMLResponse("", status_code=204)

    draft.proposal_json = payload.model_dump()

    # HTMX: return empty body + tell the client to refresh the page so the
    # bookmark renders under its new category. Simpler than re-rendering
    # individual sections.
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/setup/proposal/finalize")
async def proposal_finalize(
    request: Request,
    _user: AuthedUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RedirectResponse:
    form = await request.form()
    slugs = form.getlist("slug[]")
    names = form.getlist("name[]")
    descriptions = form.getlist("description[]")
    edits: list[CategoryEdit] = []
    for i, name in enumerate(names):
        if not name.strip():
            continue
        edits.append(
            CategoryEdit(
                slug=slugs[i] if i < len(slugs) else "",
                name=name,
                description=descriptions[i] if i < len(descriptions) else "",
                sort_order=i,
            )
        )

    draft = await get_active_draft(session)
    if draft is None:
        return RedirectResponse("/setup/proposal", status_code=303)

    await finalize_taxonomy(session, draft, edits)
    return RedirectResponse("/", status_code=303)


# Legacy redirect: anything still pointing at /setup/review or
# /setup/classifying just goes back to /setup/proposal under the new flow.
@router.get("/setup/classifying", response_class=HTMLResponse)
async def classifying(_user: AuthedUser) -> RedirectResponse:
    return RedirectResponse("/setup/proposal", status_code=303)


@router.get("/setup/review", response_class=HTMLResponse)
async def review(_user: AuthedUser) -> RedirectResponse:
    return RedirectResponse("/setup/proposal", status_code=303)


@router.post("/setup/complete")
async def complete(
    _user: AuthedUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RedirectResponse:
    await set_setup_phase(session, "complete")
    return RedirectResponse("/", status_code=303)
