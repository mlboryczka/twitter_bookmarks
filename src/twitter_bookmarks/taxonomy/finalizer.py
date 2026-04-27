"""Persist the user-edited taxonomy into the categories table."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from pydantic import BaseModel, Field
from slugify import slugify
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from twitter_bookmarks.db.models import Category, TaxonomyProposal
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
    edited_categories: list[CategoryEdit],
) -> list[Category]:
    """Lock in the user-edited taxonomy.

    Wipes existing categories (and their dependents — caller is expected
    to have wiped classifications/feedback first via reset_setup if this
    is a re-run), inserts the new list, marks the active draft proposal
    `finalized`, and advances `setup_phase` to `classifying_backfill`.
    """
    if not edited_categories:
        raise ValueError("Cannot finalize an empty taxonomy")

    # Replace any existing categories. In a fresh setup this is a no-op;
    # for a re-run, callers should already have cleared classifications
    # and feedback (see scripts/reset_setup.py) so the FK delete is safe.
    await session.execute(delete(Category))

    used_slugs: set[str] = set()
    rows: list[Category] = []
    for idx, edit in enumerate(edited_categories):
        if not edit.name.strip():
            continue
        slug = edit.slug.strip() or _ensure_slug(edit.name, used_slugs)
        if not edit.slug.strip():
            # _ensure_slug already added; record either way
            used_slugs.add(slug)
        else:
            if slug in used_slugs:
                raise ValueError(f"Duplicate slug: {slug}")
            used_slugs.add(slug)
        cat = Category(
            slug=slug,
            name=edit.name.strip(),
            description=edit.description.strip(),
            sort_order=edit.sort_order or idx,
        )
        session.add(cat)
        rows.append(cat)
    await session.flush()

    # Mark active drafts finalized.
    drafts = (
        await session.execute(
            select(TaxonomyProposal).where(TaxonomyProposal.status == "draft")
        )
    ).scalars().all()
    for draft in drafts:
        draft.status = "finalized"
        draft.finalized_at = datetime.now(timezone.utc)

    await set_setup_phase(session, "classifying_backfill")

    logger.info(
        "Finalized taxonomy with %d categories: %s",
        len(rows),
        ", ".join(c.slug for c in rows),
    )
    return rows


async def list_categories(session: AsyncSession) -> list[Category]:
    """Return the locked taxonomy in sort order."""
    result = await session.execute(
        select(Category).order_by(Category.sort_order, Category.id)
    )
    return list(result.scalars().all())
