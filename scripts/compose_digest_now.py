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

    async with session_scope() as session:
        digest = await compose_digest(session, args.start, args.end)
        await session.flush()
        digest_id = digest.id
        print(f"Composed digest #{digest_id}: {digest.subject}")
        print(f"  bookmarks: {digest.bookmark_count}")

    if args.skip_send:
        print("Skipping send (--skip-send).")
        return

    async with session_scope() as session:
        from sqlalchemy import select

        from twitter_bookmarks.db.models import Digest

        digest = (
            await session.execute(select(Digest).where(Digest.id == digest_id))
        ).scalar_one()
        await send_digest(digest)
        print(f"Sent digest #{digest_id} (status={digest.email_status})")


if __name__ == "__main__":
    asyncio.run(main())
