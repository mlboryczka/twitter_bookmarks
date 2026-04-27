"""Claude Sonnet proposes a taxonomy AND classifies every bookmark in one call.

The proposal is generated via tool use so we get a structured response back.
For each proposed category, Sonnet returns the full list of bookmarks it
believes belong there along with a 1-2 sentence gist per bookmark. The
user reviews the result on /setup/proposal and can move bookmarks between
categories before finalizing — each move becomes classifier_feedback.
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

# Cap how many bookmarks we send to Sonnet. Above this, we down-sample —
# but if we down-sample, we can only classify the sampled subset (others
# are deferred to Haiku).
MAX_CORPUS_SIZE = 200
# Anthropic pricing as of April 2026 — used to log the cost row. Confirm
# at https://www.anthropic.com/api before relying on for billing.
SONNET_INPUT_PER_MTOK = 3.00
SONNET_OUTPUT_PER_MTOK = 15.00

PROPOSE_TOOL = {
    "name": "propose_taxonomy",
    "description": (
        "Record the proposed taxonomy AND assign EVERY shown bookmark to "
        "exactly one category. Always return between 6 and 12 categories. "
        "Categories must be MECE (mutually exclusive, collectively "
        "exhaustive) over the shown bookmarks, substantive (each plausibly "
        "fits 3+ bookmarks), and reusable concepts (e.g. 'AI / ML', not "
        "'AI in 2026'). Avoid sentiment- or quality-based categories like "
        "'interesting' or 'must-read'. A single catch-all 'misc' is "
        "permitted only when genuinely needed. EVERY bookmark id shown to "
        "you must appear in exactly one category's bookmarks array."
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
                                "Why this category exists for this user — "
                                "what patterns in their bookmarks led you "
                                "to propose it."
                            ),
                        },
                        "bookmarks": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "tweet_id": {
                                        "type": "string",
                                        "description": (
                                            "An id from the BOOKMARKS list "
                                            "shown to you. Must be a real id."
                                        ),
                                    },
                                    "gist": {
                                        "type": "string",
                                        "description": (
                                            "1-2 sentence summary of what "
                                            "this bookmark says. Concrete, "
                                            "specific, no filler."
                                        ),
                                    },
                                },
                                "required": ["tweet_id", "gist"],
                            },
                            "description": (
                                "Every bookmark you assign to this category. "
                                "Each bookmark id must appear in exactly one "
                                "category across the whole response."
                            ),
                        },
                    },
                    "required": [
                        "slug",
                        "name",
                        "description",
                        "rationale",
                        "bookmarks",
                    ],
                },
            },
            "overall_rationale": {
                "type": "string",
                "description": (
                    "Short paragraph framing the taxonomy as a whole — the "
                    "user's broad area of interest, how the categories "
                    "relate to each other."
                ),
            },
        },
        "required": ["categories", "overall_rationale"],
    },
}


class ProposedBookmark(BaseModel):
    """One bookmark assigned to a category in the proposal."""

    tweet_id: str
    gist: str


class ProposedCategory(BaseModel):
    """One proposed category with its assigned bookmarks."""

    slug: str
    name: str
    description: str
    rationale: str
    bookmarks: list[ProposedBookmark] = Field(default_factory=list)


class TaxonomyProposalPayload(BaseModel):
    """Structured response from the proposer."""

    categories: list[ProposedCategory]
    overall_rationale: str
    # User edits applied on top of the original Sonnet output. Each move
    # becomes a classifier_feedback row at finalize time.
    moves: list[dict[str, Any]] = Field(default_factory=list)


def _sample_bookmarks(
    rows: list[tuple[str, str, str, str]],
) -> list[tuple[str, str, str, str]]:
    """Down-sample chronologically ordered rows to MAX_CORPUS_SIZE."""
    if len(rows) <= MAX_CORPUS_SIZE:
        return list(rows)
    step = (len(rows) + MAX_CORPUS_SIZE - 1) // MAX_CORPUS_SIZE
    return rows[::step][:MAX_CORPUS_SIZE]


async def _load_corpus(session: AsyncSession) -> list[tuple[str, str, str, str]]:
    """Return (tweet_id, username, text, created_at) for every bookmark."""
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
        snippet = text.strip()
        if len(snippet) > 1200:
            snippet = snippet[:1200].rstrip() + "…"
        parts.append(f"---\nid: {tweet_id}\nauthor: @{username}\ntext: {snippet}")
    return "\n\n".join(parts)


def _system_prompt() -> str:
    return (
        "You are designing a personal taxonomy of bookmark categories for a "
        "single user, AND classifying every one of their bookmarks into the "
        "taxonomy. Your taxonomy must carve up THEIR interests cleanly, not "
        "be a generic taxonomy of internet topics.\n\n"
        "Hard rules:\n"
        "- 6-12 categories total. No more, no fewer.\n"
        "- Each category must plausibly fit 3+ of the shown bookmarks.\n"
        "- Categories must be MECE: every shown bookmark fits exactly one.\n"
        "- Every bookmark id from the BOOKMARKS list must appear in "
        "exactly one category's bookmarks array. Do not skip any.\n"
        "- Avoid sentiment-based or quality-based categories ('interesting', "
        "'must-read', 'thought-provoking').\n"
        "- Categories should be reusable concepts ('AI / ML' not 'AI in "
        "2026'; 'Macro & Markets' not 'inflation 2024').\n"
        "- A single catch-all 'misc' is allowed only if genuinely needed.\n"
        "- Slugs are kebab-case and become permanent identifiers.\n"
        "- For each bookmark, write a 1-2 sentence gist that's concrete and "
        "specific — no filler, no 'this tweet discusses…'\n\n"
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
    """Generate a taxonomy + classify every shown bookmark in one Sonnet call.

    The draft is stored in `taxonomy_proposals` and returned. The caller
    (the setup wizard) presents it to the user for review on /setup/proposal;
    on finalize, `taxonomy.finalizer.finalize_taxonomy` writes the categories
    + bookmark_classifications + classifier_feedback rows.
    """
    settings = get_settings()
    rows = await _load_corpus(session)
    if not rows:
        raise RuntimeError("No bookmarks found; cannot propose a taxonomy")

    sample = _sample_bookmarks(rows)
    sample_ids = {r[0] for r in sample}
    corpus = _format_corpus(sample)
    user_message = (
        f"Here are {len(sample)} of my {len(rows)} bookmarks. Propose a "
        "taxonomy AND assign every shown bookmark to exactly one category. "
        "Use the tweet ids exactly as shown.\n\n"
        f"BOOKMARKS:\n{corpus}"
    )

    # Output cap: 99 bookmarks * ~150 tokens (gist + tweet_id) ~= 15k.
    # Plus category metadata ~= 2k. Cap at 16k tokens output to be safe.
    max_tokens = max(4096, len(sample) * 200 + 2000)

    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    response = await client.messages.create(
        model=settings.SONNET_MODEL,
        max_tokens=min(max_tokens, 16000),
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

    # Dedupe: Sonnet sometimes lists the same tweet_id in multiple
    # categories. Keep the first occurrence so the proposal page (and
    # later finalize) sees each bookmark exactly once.
    seen_tweet_ids: set[str] = set()
    duplicates_removed = 0
    for cat in payload.categories:
        kept: list[ProposedBookmark] = []
        for bm in cat.bookmarks:
            if bm.tweet_id in seen_tweet_ids:
                duplicates_removed += 1
                continue
            seen_tweet_ids.add(bm.tweet_id)
            kept.append(bm)
        cat.bookmarks = kept
    if duplicates_removed:
        logger.warning(
            "Removed %d duplicate tweet_id assignments from Sonnet's output",
            duplicates_removed,
        )

    # Sanity check: warn (don't fail) if Sonnet skipped or invented bookmarks.
    assigned: dict[str, str] = {}
    for cat in payload.categories:
        for bm in cat.bookmarks:
            assigned[bm.tweet_id] = cat.slug
    missing = sample_ids - set(assigned.keys())
    extra = set(assigned.keys()) - sample_ids
    if missing:
        logger.warning(
            "Sonnet skipped %d bookmarks; will land them in 'misc' on finalize: %s",
            len(missing),
            list(missing)[:5],
        )
    if extra:
        logger.warning(
            "Sonnet returned %d unknown ids; ignoring: %s",
            len(extra),
            list(extra)[:5],
        )

    # Backfill missing bookmarks into a 'misc' category if needed.
    if missing:
        misc_cat = next(
            (c for c in payload.categories if c.slug == "misc"), None
        )
        if misc_cat is None:
            misc_cat = ProposedCategory(
                slug="misc",
                name="Misc",
                description=(
                    "Bookmarks that didn't fit cleanly into the other "
                    "categories — review and reassign or split."
                ),
                rationale="Catch-all for unassigned bookmarks.",
                bookmarks=[],
            )
            payload.categories.append(misc_cat)
        for tid in missing:
            misc_cat.bookmarks.append(
                ProposedBookmark(tweet_id=tid, gist="(no gist generated)")
            )

    await _supersede_drafts(session)
    session.add(
        TaxonomyProposal(
            proposal_json=payload.model_dump(),
            status="draft",
        )
    )

    logger.info(
        "Proposed taxonomy with %d categories, %d bookmarks classified "
        "(corpus_total=%d, sampled=%d)",
        len(payload.categories),
        sum(len(c.bookmarks) for c in payload.categories),
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


def apply_move(
    payload: TaxonomyProposalPayload,
    tweet_id: str,
    to_slug: str,
    feedback: str | None = None,
) -> tuple[str, str] | None:
    """Move a bookmark to a new category in the in-memory payload.

    Returns (from_slug, to_slug) on success (or None if tweet_id not found
    or already in the target category). Records the move in payload.moves
    so finalize can produce classifier_feedback rows.
    """
    target = next((c for c in payload.categories if c.slug == to_slug), None)
    if target is None:
        raise ValueError(f"Unknown target category slug: {to_slug}")

    moved_bookmark: ProposedBookmark | None = None
    from_slug: str | None = None
    for cat in payload.categories:
        for i, bm in enumerate(cat.bookmarks):
            if bm.tweet_id == tweet_id:
                if cat.slug == to_slug:
                    return None
                from_slug = cat.slug
                moved_bookmark = cat.bookmarks.pop(i)
                break
        if moved_bookmark is not None:
            break

    if moved_bookmark is None or from_slug is None:
        return None

    target.bookmarks.append(moved_bookmark)
    payload.moves.append(
        {
            "tweet_id": tweet_id,
            "from_slug": from_slug,
            "to_slug": to_slug,
            "feedback": feedback or "",
        }
    )
    return (from_slug, to_slug)


def apply_merge(
    payload: TaxonomyProposalPayload,
    source_slug: str,
    target_slug: str,
) -> int:
    """Merge `source_slug`'s bookmarks into `target_slug`, drop `source_slug`.

    Returns the number of bookmarks moved. Merges don't write feedback
    rows — Sonnet split too finely, the bookmarks weren't miscategorized.
    Raises ValueError if either slug is unknown or they're the same.
    """
    if source_slug == target_slug:
        raise ValueError("Source and target slugs must differ")
    source = next((c for c in payload.categories if c.slug == source_slug), None)
    target = next((c for c in payload.categories if c.slug == target_slug), None)
    if source is None:
        raise ValueError(f"Unknown source slug: {source_slug}")
    if target is None:
        raise ValueError(f"Unknown target slug: {target_slug}")

    moved = len(source.bookmarks)
    target.bookmarks.extend(source.bookmarks)
    source.bookmarks = []
    payload.categories = [c for c in payload.categories if c.slug != source_slug]
    return moved
