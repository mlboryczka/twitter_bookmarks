"""Claude Sonnet proposes a taxonomy from the user's bookmarked corpus.

The proposal is generated via tool use so we get a structured response back.
We sample large corpora to keep the prompt manageable while still capturing
topical drift over time (every-Nth chronological sampling).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from twitter_bookmarks.config import get_settings
from twitter_bookmarks.db.models import (
    Author,
    Bookmark,
    BookmarkThread,
    TaxonomyProposal,
    Tweet,
)
from twitter_bookmarks.db.session import session_scope
from twitter_bookmarks.x_api.client import log_api_call

logger = logging.getLogger(__name__)

# Cap how many bookmarks we send to Sonnet. Above this, we down-sample.
MAX_CORPUS_SIZE = 200
# Anthropic pricing as of April 2026 — used to log the cost row. Confirm
# at https://www.anthropic.com/api before relying on for billing.
SONNET_INPUT_PER_MTOK = 3.00
SONNET_OUTPUT_PER_MTOK = 15.00

PROPOSE_TOOL = {
    "name": "propose_taxonomy",
    "description": (
        "Record the proposed taxonomy of categories for the user's bookmark "
        "corpus. Always return between 6 and 12 categories. Categories must "
        "be MECE (mutually exclusive, collectively exhaustive) over the "
        "shown bookmarks, substantive (each category should plausibly fit "
        "at least 3-5 bookmarks), and reusable concepts (e.g. 'AI / ML', "
        "not 'AI in 2026'). Avoid sentiment- or quality-based categories "
        "like 'interesting' or 'must-read'. A single catch-all 'misc' is "
        "permitted only when genuinely needed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "categories": {
                "type": "array",
                "minItems": 6,
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "properties": {
                        "slug": {
                            "type": "string",
                            "description": (
                                "kebab-case immutable identifier, e.g. "
                                "'ai-ml', 'product-design'"
                            ),
                        },
                        "name": {
                            "type": "string",
                            "description": "Display name, title-cased.",
                        },
                        "description": {
                            "type": "string",
                            "description": (
                                "1-2 sentences describing what belongs here. "
                                "This is reused verbatim in the classifier "
                                "prompt, so make it concrete and bounded."
                            ),
                        },
                        "rationale": {
                            "type": "string",
                            "description": (
                                "Why this category exists *for this user*: "
                                "what patterns in their bookmarks led you to "
                                "propose it."
                            ),
                        },
                        "example_tweet_ids": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 5,
                            "items": {"type": "string"},
                            "description": (
                                "3-5 tweet IDs from the shown corpus that "
                                "would clearly belong in this category."
                            ),
                        },
                    },
                    "required": [
                        "slug",
                        "name",
                        "description",
                        "rationale",
                        "example_tweet_ids",
                    ],
                },
            },
            "overall_rationale": {
                "type": "string",
                "description": (
                    "A short paragraph framing the taxonomy as a whole — "
                    "what's the user's broad area of interest, how do the "
                    "categories relate to each other."
                ),
            },
        },
        "required": ["categories", "overall_rationale"],
    },
}


class ProposedCategory(BaseModel):
    """One proposed category in a taxonomy."""

    slug: str
    name: str
    description: str
    rationale: str
    example_tweet_ids: list[str] = Field(default_factory=list)


class TaxonomyProposalPayload(BaseModel):
    """Structured response from the proposer."""

    categories: list[ProposedCategory]
    overall_rationale: str


def _sample_bookmarks(
    rows: list[tuple[str, str, str, str]],
) -> list[tuple[str, str, str, str]]:
    """Down-sample chronologically ordered rows to MAX_CORPUS_SIZE.

    Each row is (tweet_id, author_username, text, created_at_iso). Rows are
    expected to already be sorted by created_at; we keep every k-th entry
    where k = ceil(len(rows) / MAX_CORPUS_SIZE) so we still see drift over
    time.
    """
    if len(rows) <= MAX_CORPUS_SIZE:
        return list(rows)
    step = (len(rows) + MAX_CORPUS_SIZE - 1) // MAX_CORPUS_SIZE
    return rows[::step][:MAX_CORPUS_SIZE]


async def _load_corpus(session: AsyncSession) -> list[tuple[str, str, str, str]]:
    """Return (tweet_id, username, text, created_at) for every bookmark.

    Uses thread text when available, leaf tweet text otherwise. Sorted
    chronologically (oldest first) so down-sampling captures drift.
    """
    stmt = (
        select(
            Bookmark.tweet_id,
            Author.username,
            func.coalesce(BookmarkThread.full_thread_text, Tweet.text),
            Tweet.created_at,
        )
        .join(Tweet, Tweet.tweet_id == Bookmark.tweet_id)
        .join(Author, Author.author_id == Tweet.author_id)
        .outerjoin(
            BookmarkThread, BookmarkThread.bookmark_tweet_id == Bookmark.tweet_id
        )
        .order_by(Tweet.created_at.asc())
    )
    result = await session.execute(stmt)
    return [
        (tid, uname, text or "", ca.isoformat() if ca else "")
        for tid, uname, text, ca in result.all()
    ]


def _format_corpus(rows: list[tuple[str, str, str, str]]) -> str:
    parts = []
    for tweet_id, username, text, _ in rows:
        # Trim very long thread texts to keep tokens bounded.
        snippet = text.strip()
        if len(snippet) > 1200:
            snippet = snippet[:1200].rstrip() + "…"
        parts.append(f"---\nid: {tweet_id}\nauthor: @{username}\ntext: {snippet}")
    return "\n\n".join(parts)


def _system_prompt() -> str:
    return (
        "You are designing a personal taxonomy of bookmark categories for a "
        "single user, based on the X (Twitter) bookmarks they've actually "
        "saved. Your goal is to propose 6-12 categories that carve up THEIR "
        "interests cleanly, not a generic taxonomy of internet topics.\n\n"
        "Hard rules:\n"
        "- 6-12 categories total. No more, no fewer.\n"
        "- Each category must plausibly fit 3 or more of the shown bookmarks.\n"
        "- Categories must be MECE: every shown bookmark fits exactly one.\n"
        "- Avoid sentiment-based or quality-based categories ('interesting', "
        "'must-read', 'thought-provoking').\n"
        "- Categories should be reusable concepts ('AI / ML' not "
        "'AI in 2026'; 'Macro & Markets' not 'inflation 2024').\n"
        "- A single catch-all 'misc' is allowed only if genuinely needed.\n"
        "- Slugs are kebab-case and become permanent identifiers.\n\n"
        "Use the propose_taxonomy tool to record your final answer. Do not "
        "produce any prose response — only call the tool."
    )


def _estimate_cost(usage: dict[str, int] | Any) -> float:
    if not usage:
        return 0.0
    in_tokens = getattr(usage, "input_tokens", None) or usage.get("input_tokens", 0)
    out_tokens = getattr(usage, "output_tokens", None) or usage.get(
        "output_tokens", 0
    )
    return (
        in_tokens / 1_000_000 * SONNET_INPUT_PER_MTOK
        + out_tokens / 1_000_000 * SONNET_OUTPUT_PER_MTOK
    )


async def propose_taxonomy(session: AsyncSession) -> TaxonomyProposalPayload:
    """Generate a fresh taxonomy proposal and persist it as a draft.

    The draft is stored in `taxonomy_proposals` and returned. The caller
    (the setup wizard) presents it to the user for editing; on finalize,
    `taxonomy.finalizer.finalize_taxonomy` writes the user-edited list to
    the `categories` table and marks the proposal `finalized`.
    """
    settings = get_settings()
    rows = await _load_corpus(session)
    if not rows:
        raise RuntimeError("No bookmarks found; cannot propose a taxonomy")

    sample = _sample_bookmarks(rows)
    corpus = _format_corpus(sample)
    user_message = (
        f"Here are {len(sample)} of my {len(rows)} bookmarks "
        "(sampled chronologically). Propose a taxonomy I can use to "
        "categorize all of them.\n\n"
        f"BOOKMARKS:\n{corpus}"
    )

    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    response = await client.messages.create(
        model=settings.SONNET_MODEL,
        max_tokens=4096,
        system=_system_prompt(),
        tools=[PROPOSE_TOOL],
        tool_choice={"type": "tool", "name": "propose_taxonomy"},
        messages=[{"role": "user", "content": user_message}],
    )

    cost = _estimate_cost(response.usage)
    async with session_scope() as cost_session:
        await log_api_call(
            cost_session,
            service="anthropic",
            endpoint=f"messages:{settings.SONNET_MODEL}:propose_taxonomy",
            status_code=200,
            cost_usd=cost,
            tweets_returned=len(sample),
            notes=(
                f"input_tokens={getattr(response.usage, 'input_tokens', '?')} "
                f"output_tokens={getattr(response.usage, 'output_tokens', '?')}"
            ),
        )

    tool_input: dict[str, Any] | None = None
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "propose_taxonomy":
            tool_input = block.input  # type: ignore[assignment]
            break
    if tool_input is None:
        raise RuntimeError(
            "Sonnet did not call propose_taxonomy. Response: "
            + json.dumps([b.model_dump() for b in response.content])[:1000]
        )

    payload = TaxonomyProposalPayload.model_validate(tool_input)

    # Mark any previous draft as superseded so /setup/proposal always sees
    # exactly one active draft.
    await _supersede_drafts(session)
    session.add(
        TaxonomyProposal(
            proposal_json=payload.model_dump(),
            status="draft",
        )
    )

    logger.info(
        "Proposed taxonomy with %d categories (corpus_total=%d, sampled=%d)",
        len(payload.categories),
        len(rows),
        len(sample),
    )
    return payload


async def _supersede_drafts(session: AsyncSession) -> None:
    drafts = (
        await session.execute(
            select(TaxonomyProposal).where(TaxonomyProposal.status == "draft")
        )
    ).scalars().all()
    for draft in drafts:
        draft.status = "superseded"


async def get_active_draft(session: AsyncSession) -> TaxonomyProposal | None:
    """Return the most recent draft proposal, if any."""
    result = await session.execute(
        select(TaxonomyProposal)
        .where(TaxonomyProposal.status == "draft")
        .order_by(desc(TaxonomyProposal.created_at))
        .limit(1)
    )
    return result.scalar_one_or_none()
