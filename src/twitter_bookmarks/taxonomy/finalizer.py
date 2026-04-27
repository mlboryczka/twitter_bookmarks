"""Persist the user-edited proposal as the locked taxonomy + classifications.

The proposal already contains every bookmark assigned to a category (Sonnet
did this in one call). On finalize we:
  1. Write the categories table.
  2. Write a bookmark_classifications row for every assigned bookmark, using
     Sonnet's gist + a synthetic reasoning string + classifier_version
     "v1-sonnet-proposal". sub_tags start empty; they'll be filled in lazily
     when the user moves a bookmark or when Haiku reclassifies later.
  3. Write a classifier_feedback row for every bookmark that the user moved
     between Sonnet's original assignment and the final state. The text
     snapshot is the bookmark's thread text (or leaf tweet text).
  4. Set setup_phase = 'complete' (we skip the post-finalize Haiku run since
     classifications already exist).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field
from slugify import slugify
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from twitter_bookmarks.config import get_settings
from twitter_bookmarks.db.models import (
    Author,
    Bookmark,
    BookmarkClassification,
    BookmarkThread,
    Category,
    ClassifierFeedback,
    TaxonomyProposal,
    Tweet,
)
from twitter_bookmarks.setup_state import set_setup_phase

logger = logging.getLogger(__name__)


class CategoryEdit(BaseModel):
    """A user-supplied category from the proposal-edit form."""

    slug: str = Field(default="")
    name: str
    description: str
    sort_order: int = 0


def _ensure_slug(name: str, existing: set[str]) -> str:
    """Generate a unique kebab-case slug from a display name."""
    base = slugify(name) or "category"
    candidate = base
    counter = 2
    while candidate in existing:
        candidate = f"{base}-{counter}"
        counter += 1
    existing.add(candidate)
    return candidate


async def finalize_taxonomy(
    session: AsyncSession,
    proposal: TaxonomyProposal,
    edited_categories: list[CategoryEdit],
) -> list[Category]:
    """Lock the taxonomy and persist all classifications + feedback in one shot.

    `edited_categories` is the user's edits to category names/descriptions
    (in proposal slug order, with `slug` matching the proposal). The
    bookmark→category mapping comes from `proposal.proposal_json` directly,
    after any user moves recorded via the move endpoint.
    """
    if not edited_categories:
        raise ValueError("Cannot finalize an empty taxonomy")

    settings = get_settings()
    proposal_data: dict[str, Any] = proposal.proposal_json or {}
    proposal_categories: list[dict[str, Any]] = proposal_data.get("categories", [])
    moves: list[dict[str, Any]] = proposal_data.get("moves", [])

    # Map original slug → edited (slug, name, description, sort_order).
    edits_by_slug: dict[str, CategoryEdit] = {}
    used_slugs: set[str] = set()
    for idx, edit in enumerate(edited_categories):
        if not edit.name.strip():
            continue
        original_slug = edit.slug.strip()
        # Re-slug if the user blanked it (e.g. for a brand-new category).
        if not original_slug:
            new_slug = _ensure_slug(edit.name, used_slugs)
            edit = CategoryEdit(
                slug=new_slug,
                name=edit.name,
                description=edit.description,
                sort_order=edit.sort_order or idx,
            )
        else:
            if original_slug in used_slugs:
                raise ValueError(f"Duplicate slug: {original_slug}")
            used_slugs.add(original_slug)
        edits_by_slug[edit.slug] = edit

    # Wipe + re-create categories. Cascade through dependent rows first so
    # foreign keys don't block (re-runs of setup hit this path).
    await session.execute(delete(ClassifierFeedback))
    await session.execute(delete(BookmarkClassification))
    await session.execute(delete(Category))
    await session.flush()

    # Insert categories in the user-supplied order. Keep a slug → row map.
    cat_rows: dict[str, Category] = {}
    for idx, edit in enumerate(edited_categories):
        if not edit.name.strip():
            continue
        slug = edit.slug.strip() or _ensure_slug(edit.name, used_slugs)
        cat = Category(
            slug=slug,
            name=edit.name.strip(),
            description=edit.description.strip(),
            sort_order=edit.sort_order or idx,
        )
        session.add(cat)
        cat_rows[slug] = cat
    await session.flush()  # make ids available

    # Pre-fetch text snapshots for any bookmarks Sonnet placed (for feedback rows).
    moved_tweet_ids = {m["tweet_id"] for m in moves}
    snapshots: dict[str, str] = {}
    if moved_tweet_ids:
        snap_stmt = (
            select(
                Tweet.tweet_id,
                Tweet.text,
                BookmarkThread.full_thread_text,
            )
            .outerjoin(
                BookmarkThread,
                BookmarkThread.bookmark_tweet_id == Tweet.tweet_id,
            )
            .where(Tweet.tweet_id.in_(moved_tweet_ids))
        )
        for tid, leaf_text, thread_text in (
            await session.execute(snap_stmt)
        ).all():
            snapshots[tid] = thread_text or leaf_text or ""

    # Write classifications from the proposal JSON.
    classification_count = 0
    for cat_payload in proposal_categories:
        slug = cat_payload.get("slug", "")
        if slug not in cat_rows:
            # User may have deleted/renamed this category. Skip — bookmarks
            # in deleted categories effectively become unclassified and will
            # be picked up by the classifier on next run.
            logger.warning(
                "Skipping classifications for missing category slug %s", slug
            )
            continue
        cat_id = cat_rows[slug].id
        for bm in cat_payload.get("bookmarks", []):
            tweet_id = bm.get("tweet_id")
            if not tweet_id:
                continue
            session.add(
                BookmarkClassification(
                    tweet_id=tweet_id,
                    category_id=cat_id,
                    sub_tags=[],
                    gist=bm.get("gist") or "",
                    classifier_reasoning=(
                        "Initial assignment by Sonnet during taxonomy proposal."
                    ),
                    classifier_version=f"{settings.CLASSIFIER_VERSION}-sonnet-proposal",
                    is_user_corrected=False,
                    previous_category_id=None,
                )
            )
            classification_count += 1

    # Apply user moves: each move is a (from_slug, to_slug) pair.
    # Per spec, the user-moved row should be marked is_user_corrected=True
    # and write a classifier_feedback row.
    feedback_count = 0
    for move in moves:
        tweet_id = move.get("tweet_id")
        from_slug = move.get("from_slug")
        to_slug = move.get("to_slug")
        feedback_text = move.get("feedback") or ""
        if not tweet_id or not to_slug:
            continue

        # The classification we already wrote should be at the to_slug.
        # Mark it corrected and set previous_category_id to the from_slug
        # category (if it still exists).
        classification = await session.get(BookmarkClassification, tweet_id)
        if classification is not None:
            classification.is_user_corrected = True
            from_cat = cat_rows.get(from_slug) if from_slug else None
            classification.previous_category_id = (
                from_cat.id if from_cat else None
            )

        # Always record feedback for the few-shot loop.
        to_cat = cat_rows.get(to_slug)
        if to_cat is None:
            continue
        from_cat = cat_rows.get(from_slug) if from_slug else None
        snapshot = snapshots.get(tweet_id, "")
        if feedback_text:
            snapshot = f"{snapshot}\n\nUSER NOTE: {feedback_text}".strip()

        session.add(
            ClassifierFeedback(
                tweet_id=tweet_id,
                predicted_category_id=from_cat.id if from_cat else None,
                correct_category_id=to_cat.id,
                thread_text_snapshot=snapshot,
            )
        )
        feedback_count += 1

    # Mark proposal finalized.
    proposal.status = "finalized"
    proposal.finalized_at = datetime.now(timezone.utc)

    # Skip the post-finalize Haiku run — classifications already exist.
    await set_setup_phase(session, "complete")

    logger.info(
        "Finalized taxonomy: %d categories, %d classifications, %d feedback rows",
        len(cat_rows),
        classification_count,
        feedback_count,
    )
    return list(cat_rows.values())


async def list_categories(session: AsyncSession) -> list[Category]:
    """Return the locked taxonomy in sort order."""
    result = await session.execute(
        select(Category).order_by(Category.sort_order, Category.id)
    )
    return list(result.scalars().all())
