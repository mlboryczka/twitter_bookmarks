"""Regenerate the 'current view' baseline for every eligible category.

By default, only refreshes baselines older than BASELINE_REFRESH_DAYS.
Pass --force to refresh every eligible category regardless of age.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

sys.path.insert(0, "src")

from twitter_bookmarks.synopsis.baseline import refresh_all_baselines  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force",
        action="store_true",
        help="refresh every eligible baseline, even fresh ones",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    summary = await refresh_all_baselines(force=args.force)
    print(
        f"\nDone. Refreshed: {summary['refreshed']}  "
        f"Skipped (still fresh): {summary['skipped']}  "
        f"Thin or failed: {summary['thin_or_failed']}"
    )


if __name__ == "__main__":
    asyncio.run(main())
