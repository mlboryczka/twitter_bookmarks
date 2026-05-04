"""Fetch + extract the body of articles linked from a tweet.

For each tweet, we look at `entities.urls[]` (already stored from the X API)
and fetch each `expanded_url` that isn't itself an x.com / twitter.com link.
We extract the main content with `trafilatura` (best-in-class Python article
extractor) and concatenate up to MAX_TOTAL_CHARS of cleaned body text into
`tweets.article_text`.

Failures are silent — paywalls, login walls, dead links, robots.txt blocks
all just leave `article_text` as None. We log the per-URL outcome but never
crash the caller.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import trafilatura
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from twitter_bookmarks.db.models import Bookmark, Tweet
from twitter_bookmarks.db.session import session_scope

logger = logging.getLogger(__name__)

# Per-URL caps and timeouts
PER_URL_TIMEOUT_S = 15.0
PER_URL_MAX_BYTES = 2_000_000  # 2 MB
MAX_TOTAL_CHARS = 3000  # cap stored article_text per tweet

# Look like a real browser — most paywalled/cloudflare-protected sites
# 403 the obvious "twitter_bookmarks/0.1" UA. Pretend to be desktop Safari.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4 Safari/605.1.15"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# Hosts we never try to fetch — they're either the tweet itself or
# require auth, captcha, etc.
SKIP_HOSTS = {
    "x.com",
    "www.x.com",
    "twitter.com",
    "www.twitter.com",
    "mobile.twitter.com",
    "t.co",  # we use expanded_url not t.co
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
}


def _expanded_urls(tweet_entities: dict[str, Any] | None) -> list[str]:
    """Pull expanded URLs from a tweet's entities, excluding skip hosts."""
    if not tweet_entities:
        return []
    urls: list[str] = []
    for entry in tweet_entities.get("urls") or []:
        u = entry.get("expanded_url") or entry.get("unwound_url") or entry.get("url")
        if not u:
            continue
        # Strip query strings? Keep them — articles often need them.
        host = u.split("/")[2] if "://" in u else ""
        host = host.lower()
        if host in SKIP_HOSTS:
            continue
        urls.append(u)
    return urls


def _lxml_fallback(html: str) -> str | None:
    """Extract visible text when trafilatura gives up.

    Job postings, event pages, GitHub READMEs, forms — these have content
    we want but trafilatura's article-detection heuristics reject them.
    Pulls text from <main>/<article> if present, else from all <p>/<li>.
    """
    try:
        from lxml import html as lxml_html
    except ImportError:
        return None
    try:
        tree = lxml_html.fromstring(html)
    except Exception:
        return None

    # Strip noise.
    for tag in tree.xpath(
        "//script | //style | //nav | //footer | //header | //aside | //form"
    ):
        tag.drop_tree()

    # Prefer <main> or <article> if present.
    candidates = tree.xpath("//main") or tree.xpath("//article")
    if candidates:
        text = candidates[0].text_content()
    else:
        # Fall back to concatenating all p/li text on the page.
        chunks = [
            el.text_content()
            for el in tree.xpath("//p | //li | //h1 | //h2 | //h3")
        ]
        text = "\n".join(c.strip() for c in chunks if c and c.strip())

    cleaned = "\n".join(
        line.strip() for line in text.splitlines() if line.strip()
    )
    return cleaned if len(cleaned) > 200 else None


async def _fetch_one(
    client: httpx.AsyncClient, url: str
) -> str | None:
    """Fetch one URL and return cleaned body text, or None on failure."""
    try:
        response = await client.get(url, follow_redirects=True)
    except httpx.HTTPError as exc:
        logger.info("Skip %s: %s", url, exc)
        return None
    if response.status_code >= 400:
        logger.info("Skip %s: HTTP %d", url, response.status_code)
        return None
    if len(response.content) > PER_URL_MAX_BYTES:
        logger.info("Skip %s: %d bytes (too large)", url, len(response.content))
        return None
    html = response.text
    try:
        extracted = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=False,
            no_fallback=False,
            with_metadata=False,
            url=url,
        )
    except Exception:
        logger.exception("trafilatura failed on %s", url)
        extracted = None

    if extracted and len(extracted.strip()) > 200:
        return extracted.strip()

    # trafilatura missed it (job page, event page, repo, form). Try lxml.
    fallback = _lxml_fallback(html)
    if fallback:
        logger.info("lxml fallback used for %s (%d chars)", url, len(fallback))
        return fallback

    return None


async def _collect_urls_with_quoted(
    session: AsyncSession, tweet: Tweet
) -> list[str]:
    """All fetchable URLs in a tweet, including its quoted/replied-to ancestors.

    A bookmark like "🔥 quote: https://x.com/foo/status/123" has no article
    URL on the leaf tweet — but the quoted tweet (id=123) often does. Walk
    `referenced_tweets` to surface those.
    """
    urls: list[str] = list(_expanded_urls(tweet.entities))

    refs = tweet.referenced_tweets or []
    ref_ids = [
        r.get("id")
        for r in refs
        if r.get("id") and r.get("type") in ("quoted", "replied_to")
    ]
    if ref_ids:
        rows = (
            await session.execute(
                select(Tweet).where(Tweet.tweet_id.in_(ref_ids))
            )
        ).scalars().all()
        for ref in rows:
            urls.extend(_expanded_urls(ref.entities))

    # Dedup, preserve order.
    seen: set[str] = set()
    unique: list[str] = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        unique.append(u)
    return unique


async def enrich_tweet(
    session: AsyncSession,
    tweet_id: str,
    *,
    force: bool = False,
) -> bool:
    """Populate `tweets.article_text` for one tweet.

    Returns True if any text was stored. If `force=False` (default), skips
    tweets that already have non-null article_text. Pulls URLs from the
    bookmark itself AND any quoted/replied-to tweets so reposted articles
    get fetched.
    """
    tweet = await session.get(Tweet, tweet_id)
    if tweet is None:
        return False
    if tweet.article_text and not force:
        return False
    urls = await _collect_urls_with_quoted(session, tweet)
    if not urls:
        return False

    parts: list[str] = []
    async with httpx.AsyncClient(
        timeout=PER_URL_TIMEOUT_S,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        for url in urls:
            body = await _fetch_one(client, url)
            if not body:
                continue
            parts.append(f"[from {url}]\n{body}")
            if sum(len(p) for p in parts) >= MAX_TOTAL_CHARS:
                break

    if not parts:
        return False
    combined = "\n\n---\n\n".join(parts)
    if len(combined) > MAX_TOTAL_CHARS:
        combined = combined[:MAX_TOTAL_CHARS].rstrip() + "…"
    tweet.article_text = combined
    return True


async def enrich_all_bookmarks(
    *,
    force: bool = False,
    concurrency: int = 5,
) -> dict[str, int]:
    """Enrich every bookmarked tweet that has any fetchable URL.

    "Fetchable URL" includes URLs on quoted/replied-to tweets, so a
    bookmark that just quotes another tweet still gets the original's
    article fetched. The actual URL inspection happens inside
    `enrich_tweet` (cheaper than walking it twice here).

    Runs `concurrency` fetches in parallel. Returns counts.
    """
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Tweet.tweet_id, Tweet.article_text)
                .join(Bookmark, Bookmark.tweet_id == Tweet.tweet_id)
            )
        ).all()

    candidates = [tid for tid, existing in rows if force or not existing]
    if not candidates:
        return {"candidates": 0, "enriched": 0, "skipped": 0}

    logger.info("Enriching up to %d bookmarks with article text", len(candidates))
    sem = asyncio.Semaphore(concurrency)
    enriched = 0
    skipped = 0

    async def _worker(tweet_id: str) -> None:
        nonlocal enriched, skipped
        async with sem:
            try:
                async with session_scope() as s:
                    ok = await enrich_tweet(s, tweet_id, force=force)
                if ok:
                    enriched += 1
                else:
                    skipped += 1
            except Exception:
                logger.exception("enrich_tweet failed for %s", tweet_id)
                skipped += 1

    await asyncio.gather(*(_worker(tid) for tid in candidates))
    return {
        "candidates": len(candidates),
        "enriched": enriched,
        "skipped": skipped,
    }
