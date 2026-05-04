"""Manually compose (and optionally send) a digest for a given date range.

Usage:
    uv run python scripts/compose_digest_now.py \\
        --start 2026-04-13 --end 2026-04-20 [--skip-send]

Date semantics: half-open interval [start, end). To cover the week of
Apr 13 through Apr 19, pass --start 2026-04-13 --end 2026-04-20.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date

sys.path.insert(0, "src")

from twitter_bookmarks.db.session import session_scope  # noqa: E402
from twitter_bookmarks.digest.composer import compose_digest  # noqa: E402
from twitter_bookmarks.digest.sender import send_digest  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--start", type=date.fromisoformat, required=True, help="period start (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--end",
        type=date.fromisoformat,
        required=True,
        help="period end exclusive (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--skip-send", action="store_true", help="compose but do not email"
    )
    args = parser.parse_args()

    # Stream progress logs so the user sees what's happening (this script
    # can take 5-10 minutes when baselines need refreshing).
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    print(
        f"Composing digest covering {args.start} → {args.end} (exclusive). "
        "This will:",
        flush=True,
    )
    print("  1. Refresh stale baselines (Opus, ~1-2 min for 8 categories)", flush=True)
    print("  2. Per-category synthesis (Sonnet, ~30s-1min)", flush=True)
    print("  3. Per-category model-update (Opus, ~2-3 min)", flush=True)
    print("  4. Render markdown + HTML", flush=True)
    print("Watch logs for progress.\n", flush=True)

    async with session_scope() as session:
        digest = await compose_digest(session, args.start, args.end)
        await session.flush()
        digest_id = digest.id
        bookmark_count = digest.bookmark_count
        subject = digest.subject

    print(f"\nComposed digest #{digest_id}: {subject}", flush=True)
    print(f"  bookmarks: {bookmark_count}", flush=True)
    print(f"  view at: http://127.0.0.1:8001/digest/{digest_id}", flush=True)

    if args.skip_send:
        print("Skipping send (--skip-send).", flush=True)
        return

    async with session_scope() as session:
        from sqlalchemy import select

        from twitter_bookmarks.db.models import Digest

        digest = (
            await session.execute(select(Digest).where(Digest.id == digest_id))
        ).scalar_one()
        await send_digest(digest)
        print(f"Sent digest #{digest_id} (status={digest.email_status})", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
