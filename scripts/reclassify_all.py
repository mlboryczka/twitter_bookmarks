"""Bump classifier_version and reclassify every bookmark.

Use after editing the static prompt or static few-shots in
`classify/classifier.py`. Deletes all existing classifications, leaves
feedback rows intact (they still serve as few-shots), then runs the
classifier worker over the whole bookmark backfill.
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "src")

from sqlalchemy import delete  # noqa: E402

from twitter_bookmarks.classify.classifier import (  # noqa: E402
    classify_unclassified_bookmarks,
)
from twitter_bookmarks.db.models import BookmarkClassification  # noqa: E402
from twitter_bookmarks.db.session import session_scope  # noqa: E402


async def main() -> None:
    async with session_scope() as session:
        deleted = await session.execute(delete(BookmarkClassification))
        print(f"Deleted {deleted.rowcount} classification rows")

    print("Reclassifying...")
    async with session_scope() as session:
        count = await classify_unclassified_bookmarks(session, batch_size=25)
    print(f"Classified {count} bookmarks")


if __name__ == "__main__":
    asyncio.run(main())
