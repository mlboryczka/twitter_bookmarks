"""Walk every bookmarked tweet and fetch + extract any linked articles.

Usage:
    uv run python scripts/enrich_with_articles.py [--force] [--concurrency N]

Stores cleaned article body text in `tweets.article_text` (capped at 3000
chars). Tweets without URLs in their entities are skipped. Failures
(paywalls, dead links, robots blocks) are silent.

By default, tweets that already have article_text are skipped. Pass
--force to re-fetch all of them.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

sys.path.insert(0, "src")

from twitter_bookmarks.ingest.articles import enrich_all_bookmarks  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="re-fetch already-enriched tweets")
    parser.add_argument("--concurrency", type=int, default=5)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    summary = await enrich_all_bookmarks(force=args.force, concurrency=args.concurrency)
    print(
        f"\nDone. Candidates: {summary['candidates']}  "
        f"Enriched: {summary['enriched']}  "
        f"Skipped (no usable content): {summary['skipped']}"
    )


if __name__ == "__main__":
    asyncio.run(main())
