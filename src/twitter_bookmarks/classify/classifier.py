"""Per-bookmark classification by Claude Haiku via tool use.

The classifier reads the live taxonomy on every call so taxonomy edits
(during setup) flow through immediately. It assembles its prompt as:

  1. System: role and rules
  2. Live taxonomy (slug + name + description)
  3. Static few-shot examples (hardcoded below)
  4. Dynamic few-shots: most recent N user corrections
  5. The bookmark to classify

Each classification is recorded in `bookmark_classifications` along with
the classifier_version so we know which version produced it (for the
reclassify-on-prompt-change workflow).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from twitter_bookmarks.classify.feedback import FeedbackExample, get_recent_few_shots
from twitter_bookmarks.config import get_settings
from twitter_bookmarks.db.models import (
    Author,
    Bookmark,
    BookmarkClassification,
    BookmarkThread,
    Category,
    Tweet,
)
from twitter_bookmarks.db.session import session_scope
from twitter_bookmarks.x_api.client import log_api_call

logger = logging.getLogger(__name__)

# Anthropic Haiku pricing as of April 2026. Confirm at
# https://www.anthropic.com/api before relying on for billing.
HAIKU_INPUT_PER_MTOK = 1.00
HAIKU_OUTPUT_PER_MTOK = 5.00

CLASSIFY_TOOL_NAME = "record_classification"

CLASSIFY_TOOL_TEMPLATE = {
    "name": CLASSIFY_TOOL_NAME,
    "description": (
        "Record the chosen category and 1-2 sentence gist for the bookmark. "
        "Pick exactly one category by its slug from the provided taxonomy. "
        "Sub-tags should be 2-5 lowercase free-form keywords. Reasoning is "
        "1-2 sentences explaining why this category fits."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "category_slug": {
                "type": "string",
                "description": "The exact slug of the chosen category.",
            },
            "sub_tags": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 0,
                "maxItems": 8,
                "description": (
                    "2-5 lowercase free-form keywords that further "
                    "describe the bookmark."
                ),
            },
            "gist": {
                "type": "string",
                "description": "1-2 sentence summary of what the bookmark says.",
            },
            "reasoning": {
                "type": "string",
                "description": "1-2 sentences justifying the category choice.",
            },
        },
        "required": ["category_slug", "sub_tags", "gist", "reasoning"],
    },
}


# Hardcoded static few-shots to prime the classifier even when no user
# feedback exists yet. These categories are illustrative — the live
# taxonomy in the prompt is what's actually authoritative.
STATIC_FEW_SHOTS = [
    {
        "text": (
            "Just shipped a paper on scaling laws for chain-of-thought "
            "reasoning. Turns out you can predict accuracy from FLOPs."
        ),
        "category_slug": "ai-ml",
        "gist": (
            "Author shipped a paper showing chain-of-thought reasoning "
            "accuracy is predictable from training compute."
        ),
        "sub_tags": ["scaling-laws", "reasoning", "papers"],
        "reasoning": (
            "Direct ML research content about model capability scaling."
        ),
    },
    {
        "text": (
            "The Fed kept rates flat. Powell's signal: more data needed "
            "before any cut. Markets priced 30% chance of June cut."
        ),
        "category_slug": "macro-markets",
        "gist": (
            "Fed held rates; Powell wants more data, markets pricing "
            "30% June cut probability."
        ),
        "sub_tags": ["fed", "interest-rates", "markets"],
        "reasoning": "Macro economic policy and market reaction.",
    },
    {
        "text": (
            "If your form has more than 7 fields and the user is on mobile, "
            "you've already lost. Cut every field that isn't a hard yes."
        ),
        "category_slug": "product-design",
        "gist": (
            "Mobile forms with >7 fields have poor completion; ruthlessly "
            "trim non-essential fields."
        ),
        "sub_tags": ["forms", "ux", "mobile"],
        "reasoning": "Product/UX design heuristic.",
    },
]


@dataclass(frozen=True)
class ClassificationResult:
    tweet_id: str
    category_id: int
    category_slug: str
    sub_tags: list[str]
    gist: str
    reasoning: str
    classifier_version: str


class _ToolPayload(BaseModel):
    """Validate the structured response from Haiku."""

    category_slug: str
    sub_tags: list[str] = Field(default_factory=list)
    gist: str
    reasoning: str

    @field_validator("sub_tags", mode="before")
    @classmethod
    def _coerce_sub_tags(cls, v: Any) -> Any:
        # Haiku sometimes ignores the array schema and returns a single
        # comma-separated string. Split it rather than crash the classifier.
        if isinstance(v, str):
            return [t.strip() for t in v.split(",") if t.strip()]
        return v


def _format_taxonomy(categories: list[Category]) -> str:
    parts = []
    for c in categories:
        parts.append(f"- {c.slug} ({c.name}): {c.description}")
    return "\n".join(parts)


def _format_static_few_shots(allowed_slugs: set[str]) -> str:
    """Render only those static few-shots whose slug exists in the live taxonomy."""
    parts = []
    for ex in STATIC_FEW_SHOTS:
        if ex["category_slug"] not in allowed_slugs:
            continue
        parts.append(
            f"BOOKMARK:\n{ex['text']}\n\n"
            f"CORRECT CLASSIFICATION:\n"
            f"  category_slug: {ex['category_slug']}\n"
            f"  sub_tags: {ex['sub_tags']}\n"
            f"  gist: {ex['gist']}\n"
            f"  reasoning: {ex['reasoning']}"
        )
    return "\n\n---\n\n".join(parts)


def _format_feedback_few_shots(examples: list[FeedbackExample]) -> str:
    if not examples:
        return ""
    parts = []
    for ex in examples:
        text = ex.text.strip()
        if len(text) > 800:
            text = text[:800].rstrip() + "…"
        line = (
            f"BOOKMARK:\n{text}\n\n"
            f"CORRECT CATEGORY: {ex.correct_category_slug} ({ex.correct_category_name})"
        )
        if ex.wrong_category_slug:
            line += (
                f"\nNOT: {ex.wrong_category_slug} ({ex.wrong_category_name}) — "
                "this was the prior misclassification the user corrected."
            )
        parts.append(line)
    return "\n\n---\n\n".join(parts)


def _system_prompt() -> str:
    return (
        "You are a classifier that assigns each X (Twitter) bookmark to "
        "exactly one category from the user's locked taxonomy. Pick the "
        "single best fit by slug — do not invent new slugs. Then write "
        "2-5 lowercase sub-tags, a 1-2 sentence gist of the bookmark's "
        "content, and 1-2 sentences of reasoning for the category choice.\n\n"
        "Always respond by calling the record_classification tool. Do not "
        "produce any prose response — only call the tool."
    )


def _build_user_message(
    *,
    taxonomy: list[Category],
    feedback_examples: list[FeedbackExample],
    bookmark_text: str,
    author_username: str,
    article_text: str | None = None,
) -> str:
    allowed = {c.slug for c in taxonomy}
    parts = [
        "TAXONOMY:",
        _format_taxonomy(taxonomy),
        "",
        "STATIC EXAMPLES (illustrative; categories may differ from yours):",
        _format_static_few_shots(allowed) or "(none applicable to this taxonomy)",
    ]
    if feedback_examples:
        parts.extend(
            [
                "",
                "USER CORRECTIONS (recent — apply these patterns):",
                _format_feedback_few_shots(feedback_examples),
            ]
        )
    parts.extend(
        [
            "",
            "BOOKMARK TO CLASSIFY:",
            f"author: @{author_username}",
            f"text: {bookmark_text}",
        ]
    )
    if article_text:
        article_snippet = article_text.strip()
        if len(article_snippet) > 1500:
            article_snippet = article_snippet[:1500].rstrip() + "…"
        parts.append(f"linked_article: {article_snippet}")
    return "\n".join(parts)


def _estimate_cost(usage: Any) -> float:
    if not usage:
        return 0.0
    in_tokens = getattr(usage, "input_tokens", 0)
    out_tokens = getattr(usage, "output_tokens", 0)
    return (
        in_tokens / 1_000_000 * HAIKU_INPUT_PER_MTOK
        + out_tokens / 1_000_000 * HAIKU_OUTPUT_PER_MTOK
    )


async def _load_categories(session: AsyncSession) -> list[Category]:
    return list(
        (
            await session.execute(
                select(Category).order_by(Category.sort_order, Category.id)
            )
        )
        .scalars()
        .all()
    )


async def classify_bookmark(
    session: AsyncSession,
    tweet_id: str,
) -> ClassificationResult:
    """Classify a single bookmark, persisting the result.

    Pulls the live taxonomy and recent user corrections on each call.
    Persists into `bookmark_classifications` (upsert) and returns the
    chosen category.
    """
    settings = get_settings()
    categories = await _load_categories(session)
    if not categories:
        raise RuntimeError("No categories defined; finalize the taxonomy first")
    by_slug = {c.slug: c for c in categories}
    allowed_slug_list = ", ".join(by_slug.keys())

    # Pull bookmark text (thread or leaf), article body, and author.
    stmt = (
        select(
            Author.username,
            Tweet.text,
            BookmarkThread.full_thread_text,
            Tweet.article_text,
        )
        .select_from(Bookmark)
        .join(Tweet, Tweet.tweet_id == Bookmark.tweet_id)
        .join(Author, Author.author_id == Tweet.author_id)
        .outerjoin(
            BookmarkThread, BookmarkThread.bookmark_tweet_id == Bookmark.tweet_id
        )
        .where(Bookmark.tweet_id == tweet_id)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        raise RuntimeError(f"Bookmark {tweet_id} not found")
    username, leaf_text, thread_text, article_text = row
    text = thread_text or leaf_text or ""

    feedback_examples = await get_recent_few_shots(session)

    tool_def = dict(CLASSIFY_TOOL_TEMPLATE)
    tool_def["description"] = (
        f"{CLASSIFY_TOOL_TEMPLATE['description']} "
        f"Allowed slugs: {allowed_slug_list}."
    )

    user_msg = _build_user_message(
        taxonomy=categories,
        feedback_examples=feedback_examples,
        bookmark_text=text,
        author_username=username or "unknown",
        article_text=article_text,
    )

    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    response = await client.messages.create(
        model=settings.HAIKU_MODEL,
        max_tokens=512,
        system=_system_prompt(),
        tools=[tool_def],
        tool_choice={"type": "tool", "name": CLASSIFY_TOOL_NAME},
        messages=[{"role": "user", "content": user_msg}],
    )

    cost = _estimate_cost(response.usage)
    async with session_scope() as cost_session:
        await log_api_call(
            cost_session,
            service="anthropic",
            endpoint=f"messages:{settings.HAIKU_MODEL}:classify",
            status_code=200,
            cost_usd=cost,
            tweets_returned=1,
        )

    tool_input: dict[str, Any] | None = None
    for block in response.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and block.name == CLASSIFY_TOOL_NAME
        ):
            tool_input = block.input  # type: ignore[assignment]
            break
    if tool_input is None:
        raise RuntimeError(
            "Haiku did not call record_classification. Response: "
            + json.dumps([b.model_dump() for b in response.content])[:500]
        )

    payload = _ToolPayload.model_validate(tool_input)
    if payload.category_slug not in by_slug:
        # Fallback: pick the closest match by string equality, else the
        # first category. We log this as a misuse but do not crash.
        logger.warning(
            "Classifier returned unknown slug %r for bookmark %s; "
            "falling back to first category",
            payload.category_slug,
            tweet_id,
        )
        chosen = categories[0]
    else:
        chosen = by_slug[payload.category_slug]

    # Upsert the classification.
    existing = await session.get(BookmarkClassification, tweet_id)
    if existing is None:
        session.add(
            BookmarkClassification(
                tweet_id=tweet_id,
                category_id=chosen.id,
                sub_tags=[t.lower() for t in payload.sub_tags],
                gist=payload.gist,
                classifier_reasoning=payload.reasoning,
                classifier_version=settings.CLASSIFIER_VERSION,
                is_user_corrected=False,
                previous_category_id=None,
            )
        )
    else:
        existing.category_id = chosen.id
        existing.sub_tags = [t.lower() for t in payload.sub_tags]
        existing.gist = payload.gist
        existing.classifier_reasoning = payload.reasoning
        existing.classifier_version = settings.CLASSIFIER_VERSION
        existing.is_user_corrected = False

    return ClassificationResult(
        tweet_id=tweet_id,
        category_id=chosen.id,
        category_slug=chosen.slug,
        sub_tags=[t.lower() for t in payload.sub_tags],
        gist=payload.gist,
        reasoning=payload.reasoning,
        classifier_version=settings.CLASSIFIER_VERSION,
    )


async def classify_unclassified_bookmarks(
    session: AsyncSession,
    batch_size: int = 25,
) -> int:
    """Classify all bookmarks lacking a row in bookmark_classifications.

    Returns the number of bookmarks classified. Honors the `has_full_thread`
    flag — only bookmarks whose text has been finalized are eligible.
    """
    stmt = (
        select(Bookmark.tweet_id)
        .outerjoin(
            BookmarkClassification,
            BookmarkClassification.tweet_id == Bookmark.tweet_id,
        )
        .where(Bookmark.has_full_thread.is_(True))
        .where(BookmarkClassification.tweet_id.is_(None))
        .order_by(desc(Bookmark.bookmarked_at))
        .limit(batch_size)
    )

    classified = 0
    while True:
        ids = list((await session.execute(stmt)).scalars().all())
        if not ids:
            break
        for tweet_id in ids:
            try:
                await classify_bookmark(session, tweet_id)
                classified += 1
            except Exception:
                logger.exception("Classifier failed on bookmark %s", tweet_id)
        await session.flush()
        # If we got fewer than batch_size, we're done.
        if len(ids) < batch_size:
            break
    return classified


async def run_setup_classify_job() -> None:
    """Background task for the setup wizard's 'classifying_backfill' phase."""
    from twitter_bookmarks.setup_state import get_setup_phase, set_setup_phase

    async with session_scope() as session:
        phase = await get_setup_phase(session)
    if phase != "classifying_backfill":
        logger.info("Setup classification skipped: phase=%s", phase)
        return

    try:
        async with session_scope() as session:
            count = await classify_unclassified_bookmarks(session)
        logger.info("Setup classification complete: %d bookmarks", count)
        async with session_scope() as session:
            await set_setup_phase(session, "awaiting_classification_review")
    except Exception:
        logger.exception("Setup classification failed")
        raise
