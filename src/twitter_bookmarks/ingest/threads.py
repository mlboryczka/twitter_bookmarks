"""Self-thread reconstruction.

A "self-thread" is a chain of replies all authored by the same user. We walk
backwards via `referenced_tweets` (type=replied_to) until either:
  * the current tweet has no replied_to reference, or
  * the parent tweet's author differs from the bookmarked tweet's author, or
  * we hit the per-thread cap (20 ancestors).

For each ancestor tweet not yet in the `tweets` table, we hydrate it via
GET /2/tweets and upsert it. Then we write a `bookmark_threads` row with the
ordered list of tweet_ids and the concatenated text, and flip
`bookmarks.has_full_thread` to true.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from twitter_bookmarks.db.models import (
    Author,
    Bookmark,
    BookmarkThread,
    Tweet,
)
from twitter_bookmarks.db.session import session_scope
from twitter_bookmarks.x_api.client import XClient
from twitter_bookmarks.x_api.endpoints import get_tweets

logger = logging.getLogger(__name__)

MAX_THREAD_DEPTH = 20


def _replied_to_id(referenced: list[dict[str, Any]] | None) -> str | None:
    """Return the parent tweet id from a `referenced_tweets` payload."""
    if not referenced:
        return None
    for ref in referenced:
        if ref.get("type") == "replied_to":
            return ref.get("id")
    return None


async def _hydrate_tweet(client: XClient, tweet_id: str) -> dict[str, Any] | None:
    """Fetch a single tweet by ID and upsert it. Returns the raw tweet dict."""
    payload = await get_tweets(client, [tweet_id])
    data = payload.get("data") or []
    if not data:
        return None
    includes = payload.get("includes") or {}
    users = includes.get("users") or []

    tweet = data[0]
    async with session_scope() as session:
        if users:
            user_rows = [
                {
                    "author_id": u["id"],
                    "username": u.get("username", ""),
                    "name": u.get("name", ""),
                    "description": u.get("description"),
                    "verified": bool(u.get("verified", False)),
                    "public_metrics": u.get("public_metrics") or {},
                }
                for u in users
            ]
            stmt = pg_insert(Author).values(user_rows)
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

        created_at = tweet.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))

        tweet_row = {
            "tweet_id": tweet["id"],
            "author_id": tweet["author_id"],
            "text": tweet.get("text", ""),
            "created_at": created_at or datetime.fromtimestamp(0),
            "lang": tweet.get("lang"),
            "public_metrics": tweet.get("public_metrics") or {},
            "entities": tweet.get("entities"),
            "conversation_id": tweet.get("conversation_id"),
            "in_reply_to_user_id": tweet.get("in_reply_to_user_id"),
            "referenced_tweets": tweet.get("referenced_tweets"),
            "raw_json": tweet,
        }
        stmt = pg_insert(Tweet).values([tweet_row]).on_conflict_do_update(
            index_elements=[Tweet.tweet_id],
            set_={
                "text": tweet_row["text"],
                "public_metrics": tweet_row["public_metrics"],
                "referenced_tweets": tweet_row["referenced_tweets"],
                "raw_json": tweet_row["raw_json"],
            },
        )
        await session.execute(stmt)
    return tweet


async def _load_tweet_or_hydrate(
    client: XClient, tweet_id: str
) -> dict[str, Any] | None:
    """Return a tweet dict from DB if known, otherwise hydrate via the API."""
    async with session_scope() as session:
        row = await session.get(Tweet, tweet_id)
        if row is not None:
            return {
                "id": row.tweet_id,
                "author_id": row.author_id,
                "text": row.text,
                "referenced_tweets": row.referenced_tweets,
            }
    return await _hydrate_tweet(client, tweet_id)


async def reconstruct_thread(client: XClient, bookmark_tweet_id: str) -> bool:
    """Reconstruct the self-thread ending in this bookmarked tweet.

    Returns True if a multi-tweet thread was assembled, False if the tweet
    has no parent of the same author (i.e. it stands alone).
    """
    async with session_scope() as session:
        leaf = await session.get(Tweet, bookmark_tweet_id)
        if leaf is None:
            logger.warning(
                "reconstruct_thread: leaf tweet %s missing from DB",
                bookmark_tweet_id,
            )
            return False
        author_id = leaf.author_id
        leaf_dict = {
            "id": leaf.tweet_id,
            "author_id": leaf.author_id,
            "text": leaf.text,
            "referenced_tweets": leaf.referenced_tweets,
        }

    chain: list[dict[str, Any]] = [leaf_dict]
    current = leaf_dict

    for _ in range(MAX_THREAD_DEPTH):
        parent_id = _replied_to_id(current.get("referenced_tweets"))
        if not parent_id:
            break
        parent = await _load_tweet_or_hydrate(client, parent_id)
        if parent is None:
            break
        if parent.get("author_id") != author_id:
            break
        chain.append(parent)
        current = parent

    # Walk root → leaf for natural reading order.
    chain.reverse()
    thread_ids = [t["id"] for t in chain]
    full_text = "\n\n".join(t.get("text", "") for t in chain)
    is_multi_tweet = len(chain) > 1

    async with session_scope() as session:
        # We always write a bookmark_threads row — even single-tweet — so
        # downstream consumers (classifier, digest) have a single canonical
        # source for "the bookmark text". `has_full_thread` then means
        # "thread reconstruction has been attempted and bookmark text is
        # finalized," gating classification.
        stmt = pg_insert(BookmarkThread).values(
            [
                {
                    "bookmark_tweet_id": bookmark_tweet_id,
                    "thread_tweet_ids": thread_ids,
                    "full_thread_text": full_text,
                }
            ]
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[BookmarkThread.bookmark_tweet_id],
            set_={
                "thread_tweet_ids": thread_ids,
                "full_thread_text": full_text,
                "reconstructed_at": func.now(),
            },
        )
        await session.execute(stmt)

        await session.execute(
            update(Bookmark)
            .where(Bookmark.tweet_id == bookmark_tweet_id)
            .values(
                has_full_thread=True,
                thread_root_id=thread_ids[0] if is_multi_tweet else None,
            )
        )

    if is_multi_tweet:
        logger.info(
            "Reconstructed %d-tweet thread for bookmark %s",
            len(chain),
            bookmark_tweet_id,
        )
    return is_multi_tweet


async def thread_text_for_bookmark(tweet_id: str) -> str:
    """Return the best-available text for a bookmark.

    If a reconstructed thread exists, return its full text; otherwise
    return the leaf tweet's text. This is the canonical input to the
    classifier and to digest synthesis.
    """
    async with session_scope() as session:
        thread_row = await session.execute(
            select(BookmarkThread).where(
                BookmarkThread.bookmark_tweet_id == tweet_id
            )
        )
        thread = thread_row.scalar_one_or_none()
        if thread:
            return thread.full_thread_text
        tweet = await session.get(Tweet, tweet_id)
        return tweet.text if tweet else ""
