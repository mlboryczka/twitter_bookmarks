"""X API pricing constants used to estimate cost-per-call.

Prices reflect the X developer tier for "Owned Reads" as of April 2026.
Confirm against https://developer.x.com/en/portal/products before relying
on these numbers for billing reconciliation.
"""

from __future__ import annotations

# Per-resource (per-tweet) costs in USD.
BOOKMARK_COST_PER_RESOURCE = 0.001
TWEET_LOOKUP_COST_PER_RESOURCE = 0.005


def bookmark_cost(tweets_returned: int) -> float:
    """USD cost for a bookmarks page that returned N tweets."""
    return tweets_returned * BOOKMARK_COST_PER_RESOURCE


def tweet_lookup_cost(tweets_returned: int) -> float:
    """USD cost for a /tweets lookup that returned N tweets."""
    return tweets_returned * TWEET_LOOKUP_COST_PER_RESOURCE
