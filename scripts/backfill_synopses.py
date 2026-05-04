"""Generate the structured per-tweet synopsis for every recent bookmark.

Walks bookmarks within BASELINE_AGE_DAYS (default 30) by default. Pass
--all to synopsize every bookmark regardless of age (initial run).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

sys.path.insert(0, "src")

from twitter_bookmarks.synopsis.per_tweet import synopsize_recent_bookmarks  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--all",
        action="store_true",
        help="synopsize every bookmark regardless of bookmarked_at",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="re-synopsize bookmarks that already have a synopsis at the current version",
    )
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    summary = await synopsize_recent_bookmarks(
        walk_all=args.all,
        overwrite=args.overwrite,
        concurrency=args.concurrency,
    )
    print(
        f"\nDone. Candidates: {summary['candidates']}  "
        f"Synopsized: {summary['synopsized']}  "
        f"Skipped: {summary['skipped']}"
    )


if __name__ == "__main__":
    asyncio.run(main())
