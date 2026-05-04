"""Sonnet-driven weekly digest composition.

For each non-empty category in the period, Sonnet writes a 2-4 paragraph
synthesis that calls out themes and notable threads of thought, referencing
bookmarks by @username. The synthesis goes into `digest_sections.synthesis`;
the bookmark list is preserved on the same row for rendering.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any

from anthropic import AsyncAnthropic
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from twitter_bookmarks.config import get_settings
from twitter_bookmarks.db.models import (
    Author,
    Bookmark,
    BookmarkClassification,
    Category,
    Digest,
    DigestSection,
    Tweet,
)
from twitter_bookmarks.db.session import session_scope
from twitter_bookmarks.x_api.client import log_api_call

logger = logging.getLogger(__name__)

# Pricing — see proposer.py for source.
SONNET_INPUT_PER_MTOK = 3.00
SONNET_OUTPUT_PER_MTOK = 15.00

SYNTHESIS_TOOL = {
    "name": "record_synthesis",
    "description": (
        "Record a 2-4 paragraph synthesis of the bookmarks in this "
        "category for the week. Call out themes and notable threads of "
        "thought; reference specific bookmarks by @username when "
        "appropriate. Be concrete, not generic. Avoid filler like "
        "'this week was full of interesting content.'"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "synthesis": {
                "type": "string",
                "description": (
                    "2-4 paragraph synthesis in markdown. Use @username "
                    "to reference specific bookmarks."
                ),
            }
        },
        "required": ["synthesis"],
    },
}


class _SynthesisPayload(BaseModel):
    synthesis: str


def _format_bookmarks_for_synthesis(rows: list[dict[str, Any]]) -> str:
    parts = []
    for r in rows:
        excerpt = r["text"][:280] + "…" if len(r["text"]) > 280 else r["text"]
        parts.append(
            f"- @{r['username']}: {r['gist']}\n"
            f"  sub_tags: {', '.join(r['sub_tags']) or '(none)'}\n"
            f"  excerpt: {excerpt}"
        )
    return "\n\n".join(parts)


def _system_prompt() -> str:
    from twitter_bookmarks.synopsis._style import WRITING_STYLE

    return (
        "You are writing one section of a weekly digest of the user's X "
        "bookmarks for one specific category. Your job is to find the "
        "themes and through-lines across the week's bookmarks — not to "
        "summarize each one.\n\n"
        "Output: 2-4 short paragraphs in markdown. Lead with the most "
        "specific concrete claim from the week's bookmarks. Reference "
        "@usernames when discussing a specific person's argument. If two "
        "bookmarks disagree, surface the disagreement and name both.\n\n"
        f"{WRITING_STYLE}\n\n"
        "Always respond by calling the record_synthesis tool. Do not "
        "produce any prose response — only call the tool."
    )


def _estimate_cost(usage: Any) -> float:
    if not usage:
        return 0.0
    in_tokens = getattr(usage, "input_tokens", 0)
    out_tokens = getattr(usage, "output_tokens", 0)
    return (
        in_tokens / 1_000_000 * SONNET_INPUT_PER_MTOK
        + out_tokens / 1_000_000 * SONNET_OUTPUT_PER_MTOK
    )


async def _synthesize_category(
    client: AsyncAnthropic,
    category: Category,
    bookmarks: list[dict[str, Any]],
) -> str:
    settings = get_settings()
    user_msg = (
        f"CATEGORY: {category.name} ({category.slug})\n"
        f"DESCRIPTION: {category.description}\n\n"
        f"BOOKMARKS THIS WEEK ({len(bookmarks)}):\n"
        f"{_format_bookmarks_for_synthesis(bookmarks)}"
    )
    response = await client.messages.create(
        model=settings.SONNET_MODEL,
        max_tokens=1024,
        system=_system_prompt(),
        tools=[SYNTHESIS_TOOL],
        tool_choice={"type": "tool", "name": "record_synthesis"},
        messages=[{"role": "user", "content": user_msg}],
    )

    cost = _estimate_cost(response.usage)
    async with session_scope() as cost_session:
        await log_api_call(
            cost_session,
            service="anthropic",
            endpoint=f"messages:{settings.SONNET_MODEL}:digest_synthesis",
            status_code=200,
            cost_usd=cost,
            tweets_returned=len(bookmarks),
            notes=f"category={category.slug}",
        )

    for block in response.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and block.name == "record_synthesis"
        ):
            payload = _SynthesisPayload.model_validate(block.input)
            return payload.synthesis
    raise RuntimeError(
        "Sonnet did not call record_synthesis. Got: "
        + json.dumps([b.model_dump() for b in response.content])[:500]
    )


async def _load_period_bookmarks(
    session: AsyncSession,
    period_start: date,
    period_end: date,
) -> list[dict[str, Any]]:
    """Pull all classified bookmarks bookmarked in [period_start, period_end]."""
    stmt = (
        select(
            BookmarkClassification.tweet_id,
            BookmarkClassification.category_id,
            BookmarkClassification.gist,
            BookmarkClassification.sub_tags,
            Tweet.text,
            Author.username,
        )
        .join(Tweet, Tweet.tweet_id == BookmarkClassification.tweet_id)
        .join(Bookmark, Bookmark.tweet_id == Tweet.tweet_id)
        .join(Author, Author.author_id == Tweet.author_id)
        .where(Bookmark.bookmarked_at >= period_start)
        .where(Bookmark.bookmarked_at < period_end)
        .order_by(Bookmark.bookmarked_at.desc())
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "tweet_id": tid,
            "category_id": cid,
            "gist": gist,
            "sub_tags": sub_tags or [],
            "text": text or "",
            "username": username,
        }
        for tid, cid, gist, sub_tags, text, username in rows
    ]


async def compose_digest(
    session: AsyncSession,
    period_start: date,
    period_end: date,
) -> Digest:
    """Compose a digest covering the half-open interval [period_start, period_end).

    Persists the digest with `email_status='pending'`. The caller (sender)
    is responsible for delivery and updating that field.
    """
    settings = get_settings()
    bookmarks = await _load_period_bookmarks(session, period_start, period_end)
    categories = list(
        (
            await session.execute(
                select(Category).order_by(Category.sort_order, Category.id)
            )
        ).scalars()
    )

    by_category: dict[int, list[dict[str, Any]]] = {}
    for b in bookmarks:
        by_category.setdefault(b["category_id"], []).append(b)

    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    sections: list[DigestSection] = []
    digest = Digest(
        period_start=period_start,
        period_end=period_end,
        composed_at=datetime.now(timezone.utc),
        sent_at=None,
        email_status="pending",
        email_message_id=None,
        subject=f"Weekly bookmarks digest: {period_start} → {period_end}",
        html_body="",
        markdown_body="",
        bookmark_count=len(bookmarks),
    )
    session.add(digest)
    await session.flush()

    # Refresh stale baselines first so the model-update tier reflects
    # the latest interpretation of the user's older bookmarks.
    try:
        from twitter_bookmarks.synopsis.baseline import refresh_all_baselines

        refresh_summary = await refresh_all_baselines()
        logger.info("Baseline refresh: %s", refresh_summary)
    except Exception:
        logger.exception("Baseline refresh failed — proceeding without")

    from twitter_bookmarks.synopsis.baseline import latest_baseline
    from twitter_bookmarks.synopsis.model_update import generate_model_update

    for cat in categories:
        cat_bookmarks = by_category.get(cat.id) or []
        if not cat_bookmarks:
            continue
        try:
            synthesis = await _synthesize_category(client, cat, cat_bookmarks)
        except Exception:
            logger.exception(
                "Synthesis failed for category %s, falling back to a stub",
                cat.slug,
            )
            synthesis = (
                f"_(Synthesis failed; {len(cat_bookmarks)} bookmarks in this "
                "category are listed below.)_"
            )

        # Model-update tier: only for categories opted in AND with a baseline.
        model_update_text: str | None = None
        baseline_view_id: int | None = None
        if cat.enable_model_update:
            try:
                baseline = await latest_baseline(session, cat.id)
                if baseline is not None:
                    update_input = [
                        {
                            "tweet_id": b["tweet_id"],
                            "username": b["username"],
                            "gist": b["gist"] or "",
                            "text_excerpt": (b["text"] or "")[:400],
                        }
                        for b in cat_bookmarks
                    ]
                    model_update_text = await generate_model_update(
                        session, cat, baseline, update_input
                    )
                    baseline_view_id = baseline.id
                else:
                    logger.info(
                        "No baseline for %s — skipping model-update tier",
                        cat.slug,
                    )
            except Exception:
                logger.exception(
                    "Model-update synthesis failed for %s; section will "
                    "render with synopses only",
                    cat.slug,
                )

        section = DigestSection(
            digest_id=digest.id,
            category_id=cat.id,
            synthesis=synthesis,
            model_update_text=model_update_text,
            baseline_view_id=baseline_view_id,
            bookmark_tweet_ids=[b["tweet_id"] for b in cat_bookmarks],
        )
        session.add(section)
        sections.append(section)

    # Render and store both formats so we don't re-render later.
    from twitter_bookmarks.digest.renderer import render_html, render_markdown

    rendered_md = await render_markdown(session, digest, sections)
    rendered_html = await render_html(session, digest, sections)
    digest.markdown_body = rendered_md
    digest.html_body = rendered_html

    logger.info(
        "Composed digest %d covering %s → %s with %d sections",
        digest.id,
        period_start,
        period_end,
        len(sections),
    )
    return digest
