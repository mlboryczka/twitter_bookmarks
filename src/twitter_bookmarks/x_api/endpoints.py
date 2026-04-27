"""Typed wrappers around the specific X API v2 endpoints we consume."""

from __future__ import annotations

from typing import Any

from twitter_bookmarks.config import get_settings
from twitter_bookmarks.db.session import session_scope
from twitter_bookmarks.x_api.client import XClient, log_api_call
from twitter_bookmarks.x_api.pricing import bookmark_cost, tweet_lookup_cost

# Fields we always request when fetching tweets. See:
# https://developer.x.com/en/docs/x-api/data-dictionary/object-model/tweet
TWEET_FIELDS = ",".join(
    [
        "id",
        "text",
        "author_id",
        "created_at",
        "lang",
        "public_metrics",
        "entities",
        "conversation_id",
        "in_reply_to_user_id",
        "referenced_tweets",
    ]
)
USER_FIELDS = ",".join(
    [
        "id",
        "name",
        "username",
        "description",
        "verified",
        "public_metrics",
    ]
)
EXPANSIONS = ",".join(
    [
        "author_id",
        "referenced_tweets.id",
        "referenced_tweets.id.author_id",
    ]
)


async def get_me(client: XClient) -> dict[str, Any]:
    """Return the authenticated user via /2/users/me."""
    payload = await client.request(
        "GET",
        "/users/me",
        params={"user.fields": USER_FIELDS},
    )
    async with session_scope() as session:
        await log_api_call(
            session,
            service="x_api",
            endpoint="/users/me",
            status_code=200,
            cost_usd=0.0,
            tweets_returned=0,
        )
    return payload


async def get_bookmarks(
    client: XClient,
    pagination_token: str | None = None,
    max_results: int = 100,
) -> dict[str, Any]:
    """Fetch one page of bookmarks for the authenticated user.

    See https://developer.x.com/en/docs/x-api/bookmarks/api-reference/get-users-id-bookmarks
    """
    settings = get_settings()
    user_id = settings.X_USER_NUMERIC_ID
    if not user_id:
        raise RuntimeError("X_USER_NUMERIC_ID is required to fetch bookmarks")

    params: dict[str, Any] = {
        "max_results": max_results,
        "tweet.fields": TWEET_FIELDS,
        "user.fields": USER_FIELDS,
        "expansions": EXPANSIONS,
    }
    if pagination_token:
        params["pagination_token"] = pagination_token

    endpoint = f"/users/{user_id}/bookmarks"
    payload = await client.request("GET", endpoint, params=params, log_endpoint=endpoint)

    tweets_returned = len(payload.get("data") or [])
    async with session_scope() as session:
        await log_api_call(
            session,
            service="x_api",
            endpoint=endpoint,
            status_code=200,
            cost_usd=bookmark_cost(tweets_returned),
            tweets_returned=tweets_returned,
        )
    return payload


async def get_tweets(client: XClient, ids: list[str]) -> dict[str, Any]:
    """Hydrate tweets by ID. Used to fetch ancestors for thread reconstruction.

    The X API caps `ids` at 100 per call.
    """
    if not ids:
        return {"data": [], "includes": {}}
    if len(ids) > 100:
        raise ValueError("get_tweets accepts at most 100 ids per call")

    payload = await client.request(
        "GET",
        "/tweets",
        params={
            "ids": ",".join(ids),
            "tweet.fields": TWEET_FIELDS,
            "user.fields": USER_FIELDS,
            "expansions": EXPANSIONS,
        },
    )
    tweets_returned = len(payload.get("data") or [])
    async with session_scope() as session:
        await log_api_call(
            session,
            service="x_api",
            endpoint="/tweets",
            status_code=200,
            cost_usd=tweet_lookup_cost(tweets_returned),
            tweets_returned=tweets_returned,
        )
    return payload
