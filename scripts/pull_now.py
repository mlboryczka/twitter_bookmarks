"""Trigger an immediate bookmark pull + classify + synopsize + enrich.

Use when you've bookmarked new tweets on X and want them ingested without
waiting for the scheduled daily pull.
"""

from __future__ import annotations

import asyncio
import logging
import sys

sys.path.insert(0, "src")

from twitter_bookmarks.ingest.bookmark_worker import (  # noqa: E402
    run_incremental_pull_job,
)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    await run_incremental_pull_job()


if __name__ == "__main__":
    asyncio.run(main())
