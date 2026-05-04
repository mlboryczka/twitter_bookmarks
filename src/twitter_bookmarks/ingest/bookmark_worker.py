"""Bookmark pull worker.

`run_bookmark_pull(full_backfill=False)` pulls bookmarks page-by-page from the
X API and upserts them into the database. In incremental mode it stops when a
page contains only tweet_ids we already have; in backfill mode it walks until
the API runs out of pages or hits the 800-bookmark cap.

After ingestion, threads are reconstructed for any newly-bookmarked tweet
that's part of a self-thread.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from twitter_bookmarks.db.models import (
    Author,
    Bookmark,
    Tweet,
    WorkerState,
)
from twitter_bookmarks.db.session import session_scope
from twitter_bookmarks.ingest.threads import reconstruct_thread
from twitter_bookmarks.setup_state import get_setup_phase, set_setup_phase
from twitter_bookmarks.x_api.client import XClient
from twitter_bookmarks.x_api.endpoints import get_bookmarks

logger = logging.getLogger(__name__)

PAGINATION_KEY = "bookmark_pagination_token"
LAST_PULL_KEY = "last_bookmark_pull"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def _upsert_authors(
    session: AsyncSession, users: list[dict[str, Any]]
) -> None:
    if not users:
        return
    rows = [
        {
            "author_id": u["id"],
            "username": u.get("username", ""),
            "name": u.get("name", ""),
            "description": u.get("description"),
            "verified": bool(u.get("verified", False)),
            "public_metrics": u.get("public_metrics") or {},
            "fetched_at": func.now(),
        }
        for u in users
    ]
    stmt = pg_insert(Author).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Author.author_id],
        set_={
            "username": stmt.excluded.username,
            "name": stmt.excluded.name,
            "description": stmt.excluded.description,
            "verified": stmt.excluded.verified,
            "public_metrics": stmt.excluded.public_metrics,
            "fetched_at": func.now(),
        },
    )
    await session.execute(stmt)


async def _upsert_tweets(
    session: AsyncSession, tweets: list[dict[str, Any]]
) -> list[str]:
    """Upsert tweets, returning the IDs of rows that were newly inserted."""
    if not tweets:
        return []

    incoming_ids = [t["id"] for t in tweets]
    existing = set(
        (
            await session.execute(
                select(Tweet.tweet_id).where(Tweet.tweet_id.in_(incoming_ids))
            )
        ).scalars()
    )
    new_ids = [tid for tid in incoming_ids if tid not in existing]

    rows = [
        {
            "tweet_id": t["id"],
            "author_id": t["author_id"],
            "text": t.get("text", ""),
            "created_at": _parse_iso(t.get("created_at"))
            or datetime.fromtimestamp(0),
            "lang": t.get("lang"),
            "public_metrics": t.get("public_metrics") or {},
            "entities": t.get("entities"),
            "conversation_id": t.get("conversation_id"),
            "in_reply_to_user_id": t.get("in_reply_to_user_id"),
            "referenced_tweets": t.get("referenced_tweets"),
            "raw_json": t,
        }
        for t in tweets
    ]
    stmt = pg_insert(Tweet).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Tweet.tweet_id],
        set_={
            "text": stmt.excluded.text,
            "public_metrics": stmt.excluded.public_metrics,
            "entities": stmt.excluded.entities,
            "referenced_tweets": stmt.excluded.referenced_tweets,
            "raw_json": stmt.excluded.raw_json,
        },
    )
    await session.execute(stmt)
    return new_ids


async def _upsert_bookmarks(
    session: AsyncSession,
    tweet_ids_in_order: list[str],
    bookmarked_at: datetime,
) -> list[str]:
    """Insert bookmark rows for tweets we haven't seen as bookmarks yet.

    Returns the list of tweet_ids that were newly bookmarked.
    The X bookmarks endpoint does not expose a per-bookmark timestamp; we
    use the time of ingestion as a stand-in.
    """
    if not tweet_ids_in_order:
        return []

    existing = set(
        (
            await session.execute(
                select(Bookmark.tweet_id).where(
                    Bookmark.tweet_id.in_(tweet_ids_in_order)
                )
            )
        ).scalars()
    )
    new_ids = [tid for tid in tweet_ids_in_order if tid not in existing]
    if not new_ids:
        return []

    rows = [
        {
            "tweet_id": tid,
            "bookmarked_at": bookmarked_at,
            "thread_root_id": None,
            "has_full_thread": False,
        }
        for tid in new_ids
    ]
    stmt = pg_insert(Bookmark).values(rows).on_conflict_do_nothing(
        index_elements=[Bookmark.tweet_id]
    )
    await session.execute(stmt)
    return new_ids


async def _read_pagination_token(session: AsyncSession) -> str | None:
    result = await session.execute(
        select(WorkerState).where(WorkerState.key == PAGINATION_KEY)
    )
    row = result.scalar_one_or_none()
    if not row:
        return None
    return (row.value or {}).get("token")


async def _write_pagination_token(
    session: AsyncSession, token: str | None
) -> None:
    payload = {"token": token}
    stmt = pg_insert(WorkerState).values(key=PAGINATION_KEY, value=payload)
    stmt = stmt.on_conflict_do_update(
        index_elements=[WorkerState.key],
        set_={"value": payload, "updated_at": func.now()},
    )
    await session.execute(stmt)


async def run_bookmark_pull(full_backfill: bool = False) -> dict[str, int]:
    """Pull bookmarks from X and persist them.

    Args:
        full_backfill: when True, walk all pages and ignore early-exit
            conditions; when False, stop once a page contains only
            already-known bookmarks.

    Returns a summary dict with counts.
    """
    pages = 0
    new_tweets = 0
    new_bookmarks = 0
    new_bookmark_ids: list[str] = []
    pagination_token: str | None = None

    if full_backfill:
        async with session_scope() as session:
            await _write_pagination_token(session, None)
    else:
        async with session_scope() as session:
            pagination_token = await _read_pagination_token(session)

    bookmarked_at = datetime.utcnow().astimezone()

    async with XClient() as client:
        while True:
            payload = await get_bookmarks(client, pagination_token=pagination_token)
            pages += 1
            tweets = payload.get("data") or []
            includes = payload.get("includes") or {}
            users = includes.get("users") or []
            included_tweets = includes.get("tweets") or []

            async with session_scope() as session:
                await _upsert_authors(session, users)
                # Ingest expansion tweets first so referenced ancestors exist.
                if included_tweets:
                    await _upsert_tweets(session, included_tweets)
                page_new_tweets = await _upsert_tweets(session, tweets)
                new_tweets += len(page_new_tweets)

                page_bookmark_ids = [t["id"] for t in tweets]
                page_new_bookmarks = await _upsert_bookmarks(
                    session, page_bookmark_ids, bookmarked_at
                )
                new_bookmarks += len(page_new_bookmarks)
                new_bookmark_ids.extend(page_new_bookmarks)

                meta = payload.get("meta") or {}
                next_token = meta.get("next_token")
                await _write_pagination_token(
                    session, next_token if full_backfill else None
                )

            logger.info(
                "Pulled page %d: %d tweets, %d new tweets, %d new bookmarks",
                pages,
                len(tweets),
                len(page_new_tweets),
                len(page_new_bookmarks),
            )

            if not next_token:
                break
            pagination_token = next_token
            if not full_backfill and not page_new_bookmarks:
                logger.info(
                    "Incremental pull: no new bookmarks on page %d, stopping",
                    pages,
                )
                break

    threads_reconstructed = 0
    articles_enriched = 0
    if new_bookmark_ids:
        async with XClient() as client:
            for tweet_id in new_bookmark_ids:
                try:
                    reconstructed = await reconstruct_thread(client, tweet_id)
                    if reconstructed:
                        threads_reconstructed += 1
                except Exception:
                    logger.exception("Thread reconstruction failed for %s", tweet_id)

        # Fetch + extract any linked articles for new bookmarks.
        from twitter_bookmarks.ingest.articles import enrich_tweet

        for tweet_id in new_bookmark_ids:
            try:
                async with session_scope() as session:
                    if await enrich_tweet(session, tweet_id):
                        articles_enriched += 1
            except Exception:
                logger.exception("Article enrichment failed for %s", tweet_id)

    async with session_scope() as session:
        stmt = pg_insert(WorkerState).values(
            key=LAST_PULL_KEY,
            value={
                "completed_at": datetime.utcnow().isoformat(),
                "pages": pages,
                "new_tweets": new_tweets,
                "new_bookmarks": new_bookmarks,
                "threads_reconstructed": threads_reconstructed,
                "articles_enriched": articles_enriched,
            },
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[WorkerState.key],
            set_={"value": stmt.excluded.value, "updated_at": func.now()},
        )
        await session.execute(stmt)

    return {
        "pages": pages,
        "new_tweets": new_tweets,
        "new_bookmarks": new_bookmarks,
        "threads_reconstructed": threads_reconstructed,
        "articles_enriched": articles_enriched,
    }


async def run_setup_backfill_job() -> None:
    """Background task for the setup wizard's 'fetching' phase."""
    try:
        async with session_scope() as session:
            phase = await get_setup_phase(session)
        if phase != "fetching_bookmarks":
            logger.info("Setup backfill skipped: phase=%s", phase)
            return

        summary = await run_bookmark_pull(full_backfill=True)
        logger.info("Setup backfill complete: %s", summary)

        async with session_scope() as session:
            await set_setup_phase(session, "awaiting_taxonomy_review")
    except Exception:
        logger.exception("Setup backfill failed")
        raise


async def run_incremental_pull_job() -> None:
    """Scheduler entry point: incremental pull when in operating mode only."""
    async with session_scope() as session:
        phase = await get_setup_phase(session)
    if phase != "complete":
        logger.debug("Skipping scheduled pull, setup_phase=%s", phase)
        return

    try:
        summary = await run_bookmark_pull(full_backfill=False)
        logger.info("Incremental pull: %s", summary)
    except Exception:
        logger.exception("Incremental pull failed")

    # Classification of newly-ingested bookmarks runs immediately after.
    try:
        from twitter_bookmarks.classify.classifier import (
            classify_unclassified_bookmarks,
        )

        async with session_scope() as session:
            classified = await classify_unclassified_bookmarks(session)
        logger.info("Classified %d new bookmarks", classified)
    except Exception:
        logger.exception("Post-pull classification failed")

    # Synopsize new bookmarks so they're ready for the weekly digest.
    try:
        from twitter_bookmarks.synopsis.per_tweet import (
            synopsize_recent_bookmarks,
        )

        # age_days=2 catches anything ingested in the last couple of days
        # without re-synopsizing the whole month every pull.
        s_summary = await synopsize_recent_bookmarks(age_days=2)
        logger.info("Synopsized %s", s_summary)
    except Exception:
        logger.exception("Post-pull synopsis failed")
