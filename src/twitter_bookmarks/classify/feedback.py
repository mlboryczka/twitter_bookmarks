"""User-correction feedback: persistence and few-shot assembly for the classifier."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from twitter_bookmarks.config import get_settings
from twitter_bookmarks.db.models import (
    BookmarkClassification,
    Category,
    ClassifierFeedback,
)
from twitter_bookmarks.ingest.threads import thread_text_for_bookmark

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeedbackExample:
    """One few-shot example assembled from a past correction."""

    tweet_id: str
    text: str
    correct_category_slug: str
    correct_category_name: str
    wrong_category_slug: str | None
    wrong_category_name: str | None


async def record_feedback(
    session: AsyncSession,
    tweet_id: str,
    correct_category_id: int,
) -> ClassifierFeedback:
    """Persist a correction.

    Reads the current classification (if any), updates it to point at the
    new category, marks `is_user_corrected=true`, sets `previous_category_id`,
    and appends a row to `classifier_feedback` for use as a future
    few-shot.
    """
    classification = await session.get(BookmarkClassification, tweet_id)
    predicted_category_id: int | None = None
    if classification is not None:
        predicted_category_id = classification.category_id
        if predicted_category_id == correct_category_id:
            # No-op correction — don't pollute feedback.
            logger.info(
                "Skip feedback: bookmark %s already in category %d",
                tweet_id,
                correct_category_id,
            )
            return ClassifierFeedback(
                tweet_id=tweet_id,
                predicted_category_id=predicted_category_id,
                correct_category_id=correct_category_id,
                thread_text_snapshot="",
            )
        classification.previous_category_id = predicted_category_id
        classification.category_id = correct_category_id
        classification.is_user_corrected = True

    snapshot = await thread_text_for_bookmark(tweet_id)
    feedback = ClassifierFeedback(
        tweet_id=tweet_id,
        predicted_category_id=predicted_category_id,
        correct_category_id=correct_category_id,
        thread_text_snapshot=snapshot,
    )
    session.add(feedback)
    await session.flush()
    logger.info(
        "Recorded feedback: bookmark %s -> category %d (was %s)",
        tweet_id,
        correct_category_id,
        predicted_category_id,
    )
    return feedback


async def get_recent_few_shots(
    session: AsyncSession, n: int | None = None
) -> list[FeedbackExample]:
    """Return the most recent N feedback rows as few-shot examples."""
    settings = get_settings()
    limit = n if n is not None else settings.FEEDBACK_FEW_SHOT_COUNT
    if limit <= 0:
        return []

    stmt = (
        select(ClassifierFeedback)
        .order_by(desc(ClassifierFeedback.corrected_at))
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()

    # Resolve category names. Pre-fetch all referenced categories to avoid N+1.
    category_ids = {row.correct_category_id for row in rows} | {
        row.predicted_category_id for row in rows if row.predicted_category_id
    }
    categories = {
        c.id: c
        for c in (
            await session.execute(
                select(Category).where(Category.id.in_(category_ids))
            )
        ).scalars()
    }

    examples: list[FeedbackExample] = []
    for row in rows:
        correct = categories.get(row.correct_category_id)
        wrong = (
            categories.get(row.predicted_category_id)
            if row.predicted_category_id is not None
            else None
        )
        if correct is None:
            continue
        examples.append(
            FeedbackExample(
                tweet_id=row.tweet_id,
                text=row.thread_text_snapshot,
                correct_category_slug=correct.slug,
                correct_category_name=correct.name,
                wrong_category_slug=wrong.slug if wrong else None,
                wrong_category_name=wrong.name if wrong else None,
            )
        )
    return examples
