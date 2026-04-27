"""Smoke test: hit /users/me and confirm an api_calls row was written."""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "src")

from sqlalchemy import desc, select  # noqa: E402

from twitter_bookmarks.db.models import ApiCall  # noqa: E402
from twitter_bookmarks.db.session import session_scope  # noqa: E402
from twitter_bookmarks.x_api.client import XClient  # noqa: E402
from twitter_bookmarks.x_api.endpoints import get_me  # noqa: E402


async def main() -> None:
    async with XClient() as client:
        me = await get_me(client)

    user = me.get("data", {})
    print(f"Authenticated as @{user.get('username')} (id={user.get('id')})")

    async with session_scope() as session:
        result = await session.execute(
            select(ApiCall).order_by(desc(ApiCall.called_at)).limit(1)
        )
        last = result.scalar_one_or_none()

    if last is None:
        sys.exit("FAIL: no api_calls row written")

    print(
        f"OK: api_calls row #{last.id} -> {last.service} {last.endpoint} "
        f"status={last.status_code} cost=${last.cost_usd}"
    )


if __name__ == "__main__":
    asyncio.run(main())
