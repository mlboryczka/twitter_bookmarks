"""Wipe taxonomy + classifications + proposals; reset setup_phase to 'not_started'.

Bookmarks themselves and ingested tweets are kept — re-running setup
should be fast on the second pass since the corpus is already local.

Use this when you want to redo the taxonomy: re-run setup, get a fresh
proposal, finalize, and reclassify the existing corpus under the new
taxonomy.
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "src")

from sqlalchemy import delete  # noqa: E402

from twitter_bookmarks.db.models import (  # noqa: E402
    BookmarkClassification,
    Category,
    ClassifierFeedback,
    TaxonomyProposal,
)
from twitter_bookmarks.db.session import session_scope  # noqa: E402
from twitter_bookmarks.setup_state import set_setup_phase  # noqa: E402


async def main() -> None:
    async with session_scope() as session:
        # Order matters for FKs.
        for model in (
            ClassifierFeedback,
            BookmarkClassification,
            TaxonomyProposal,
            Category,
        ):
            r = await session.execute(delete(model))
            print(f"Deleted {r.rowcount} rows from {model.__tablename__}")

        await set_setup_phase(session, "not_started")
        print("setup_phase -> not_started")


if __name__ == "__main__":
    asyncio.run(main())
