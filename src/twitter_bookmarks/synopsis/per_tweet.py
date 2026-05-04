"""Per-bookmark structured synopsis written by Sonnet.

For each bookmark, Sonnet produces a 3-sentence synopsis with a fixed
structure: Claim → Evidence/example → Why-it-matters. The output replaces
the existing 1-line `gist` field on `bookmark_classifications` so the
browse view + digest both pick it up automatically.

The input to Sonnet is the tweet text (or full thread text) plus any
`article_text` we extracted via trafilatura. Bookmarks with no article and
very short tweet text get a degenerate "no synopsizable content" stub
rather than a hallucinated one.
"""

from __future__ import annotations

import asyncio
import logging
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
    BookmarkThread,
    Tweet,
)
from twitter_bookmarks.db.session import session_scope
from twitter_bookmarks.x_api.client import log_api_call

logger = logging.getLogger(__name__)

# Sonnet pricing (per MTok). Same source as proposer.py.
SONNET_INPUT_PER_MTOK = 3.00
SONNET_OUTPUT_PER_MTOK = 15.00

SYNOPSIZE_TOOL = {
    "name": "record_synopsis",
    "description": (
        "Record a 3-sentence structured synopsis of one bookmark. The three "
        "sentences must be: (1) Claim — what the bookmark argues or shows; "
        "(2) Evidence — the concrete example, datum, or detail it cites; "
        "(3) Why it matters — the implication, contrast, or open question. "
        "If the bookmark is too thin to synopsize honestly (e.g., URL-only "
        "with no extracted article), set has_content=false and leave the "
        "fields empty."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "has_content": {
                "type": "boolean",
                "description": (
                    "False when the bookmark has too little content to "
                    "produce a non-hallucinated synopsis."
                ),
            },
            "claim": {
                "type": "string",
                "description": (
                    "One sentence: what the bookmark argues, claims, or shows."
                ),
            },
            "evidence": {
                "type": "string",
                "description": (
                    "One sentence: the concrete example, datum, quote, or "
                    "detail the bookmark uses to support the claim."
                ),
            },
            "why_it_matters": {
                "type": "string",
                "description": (
                    "One sentence: the implication, contrast with prior "
                    "thinking, or open question this raises."
                ),
            },
        },
        "required": ["has_content", "claim", "evidence", "why_it_matters"],
    },
}


class _ToolPayload(BaseModel):
    has_content: bool
    claim: str = ""
    evidence: str = ""
    why_it_matters: str = ""


def _system_prompt() -> str:
    return (
        "You are summarizing one X (Twitter) bookmark into a 3-sentence "
        "structured synopsis: Claim, Evidence, Why-it-matters. Be concrete "
        "and specific — quote a detail or number when the source provides "
        "one. Avoid filler ('this tweet discusses', 'an interesting take "
        "on', 'the author argues that'). Avoid hedging ('may suggest', "
        "'could imply'). If the bookmark is just a URL with no extracted "
        "article body, you do not have enough information to write an "
        "honest synopsis — call record_synopsis with has_content=false.\n\n"
        "Always respond by calling the record_synopsis tool. Do not "
        "produce any prose response — only call the tool."
    )


def _format_synopsis(payload: _ToolPayload) -> str:
    """Render the three structured fields back into one stored string."""
    if not payload.has_content:
        return "(no synopsizable content — bookmark links out without an extractable article)"
    return (
        f"**Claim.** {payload.claim.strip()} "
        f"**Evidence.** {payload.evidence.strip()} "
        f"**Why it matters.** {payload.why_it_matters.strip()}"
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


async def synopsize_bookmark(
    session: AsyncSession,
    tweet_id: str,
    *,
    overwrite: bool = False,
) -> str | None:
    """Generate the structured synopsis for one bookmark and store it.

    Returns the rendered synopsis string, or None if the bookmark has no
    classification row to update or `overwrite=False` and one is already
    populated with a Sonnet-generated synopsis.
    """
    settings = get_settings()

    classification = await session.get(BookmarkClassification, tweet_id)
    if classification is None:
        return None

    # Skip if we've already written a synopsis for this version (unless
    # caller asked to overwrite).
    target_version = f"{settings.CLASSIFIER_VERSION}-synopsis-v1"
    if not overwrite and classification.classifier_version == target_version:
        return classification.gist

    row = (
        await session.execute(
            select(
                Author.username,
                Tweet.text,
                BookmarkThread.full_thread_text,
                Tweet.article_text,
            )
            .join(Tweet, Tweet.tweet_id == Bookmark.tweet_id)
            .join(Author, Author.author_id == Tweet.author_id)
            .outerjoin(
                BookmarkThread,
                BookmarkThread.bookmark_tweet_id == Bookmark.tweet_id,
            )
            .where(Bookmark.tweet_id == tweet_id)
        )
    ).first()
    if row is None:
        return None
    username, leaf_text, thread_text, article_text = row
    text = (thread_text or leaf_text or "").strip()

    parts = [f"author: @{username or 'unknown'}", f"tweet: {text}"]
    if article_text:
        snippet = article_text.strip()
        if len(snippet) > 4000:
            snippet = snippet[:4000].rstrip() + "…"
        parts.append(f"linked_article:\n{snippet}")
    user_msg = "BOOKMARK:\n" + "\n\n".join(parts)

    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    response = await client.messages.create(
        model=settings.SONNET_MODEL,
        max_tokens=512,
        system=_system_prompt(),
        tools=[SYNOPSIZE_TOOL],
        tool_choice={"type": "tool", "name": "record_synopsis"},
        messages=[{"role": "user", "content": user_msg}],
    )

    cost = _estimate_cost(response.usage)
    async with session_scope() as cost_session:
        await log_api_call(
            cost_session,
            service="anthropic",
            endpoint=f"messages:{settings.SONNET_MODEL}:synopsize",
            status_code=200,
            cost_usd=cost,
            tweets_returned=1,
        )

    tool_input: dict[str, Any] | None = None
    for block in response.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and block.name == "record_synopsis"
        ):
            tool_input = block.input  # type: ignore[assignment]
            break
    if tool_input is None:
        logger.warning("Sonnet did not call record_synopsis for %s", tweet_id)
        return None

    payload = _ToolPayload.model_validate(tool_input)
    rendered = _format_synopsis(payload)
    classification.gist = rendered
    classification.classifier_version = target_version
    return rendered


async def synopsize_recent_bookmarks(
    *,
    age_days: int | None = None,
    walk_all: bool = False,
    overwrite: bool = False,
    concurrency: int = 3,
) -> dict[str, int]:
    """Synopsize bookmarks in the rolling interpretation window.

    By default, walks bookmarks whose `bookmarked_at` is within
    BASELINE_AGE_DAYS (override with `age_days`). Pass `walk_all=True` to
    cover every bookmark regardless of age (initial backfill).
    """
    from datetime import datetime, timedelta, timezone

    from twitter_bookmarks.db.models import Bookmark

    settings = get_settings()
    if age_days is None:
        age_days = settings.BASELINE_AGE_DAYS
    cutoff = datetime.now(timezone.utc) - timedelta(days=age_days)

    async with session_scope() as session:
        stmt = (
            select(BookmarkClassification.tweet_id)
            .join(
                Bookmark,
                Bookmark.tweet_id == BookmarkClassification.tweet_id,
            )
        )
        if not walk_all:
            stmt = stmt.where(Bookmark.bookmarked_at >= cutoff)
        ids = list((await session.execute(stmt)).scalars().all())

    if not ids:
        return {"candidates": 0, "synopsized": 0, "skipped": 0}

    sem = asyncio.Semaphore(concurrency)
    synopsized = 0
    skipped = 0

    async def _worker(tweet_id: str) -> None:
        nonlocal synopsized, skipped
        async with sem:
            try:
                async with session_scope() as s:
                    result = await synopsize_bookmark(
                        s, tweet_id, overwrite=overwrite
                    )
                if result is None:
                    skipped += 1
                else:
                    synopsized += 1
            except Exception:
                logger.exception("synopsize_bookmark failed for %s", tweet_id)
                skipped += 1

    await asyncio.gather(*(_worker(tid) for tid in ids))
    return {"candidates": len(ids), "synopsized": synopsized, "skipped": skipped}
