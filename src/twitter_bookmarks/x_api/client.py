"""Async HTTP client for the X API v2 with retry, rate limit, and cost logging."""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from twitter_bookmarks.db.models import ApiCall
from twitter_bookmarks.db.session import session_scope
from twitter_bookmarks.x_api.auth import XAuth

logger = logging.getLogger(__name__)

API_BASE = "https://api.x.com/2"


class XApiError(Exception):
    """Raised on non-recoverable X API errors."""


class XClient:
    """Thin wrapper around `httpx.AsyncClient` for X API v2 requests.

    Behaviors:
      - Adds the bearer token to every request.
      - Logs cost and tweets_returned to the `api_calls` table.
      - Retries on 429/5xx with exponential backoff, honoring
        `x-rate-limit-reset` when present.
      - Proactively sleeps when `x-rate-limit-remaining` < 5.
    """

    def __init__(self, auth: XAuth | None = None) -> None:
        self.auth = auth or XAuth()
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            timeout=60.0,
            headers={"User-Agent": "twitter_bookmarks/0.1.0"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> XClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        max_retries: int = 4,
        log_endpoint: str | None = None,
    ) -> dict[str, Any]:
        """Make an authenticated request with retry and rate-limit handling.

        Errors are logged to `api_calls` with status_code and a snippet.
        Successful responses are not auto-logged — endpoints log their own
        cost row once they've computed `cost_usd` from the response body.
        """
        backoff = 2.0
        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            token = await self.auth.get_access_token()
            try:
                response = await self._client.request(
                    method,
                    path,
                    params=params,
                    json=json,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                last_error = exc
                logger.warning("HTTP error on %s %s: %s", method, path, exc)
                if attempt < max_retries:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise XApiError(f"HTTP error after retries: {exc}") from exc

            await self._handle_rate_limits(response)

            if response.status_code in (429, 500, 502, 503, 504):
                last_error = XApiError(
                    f"{response.status_code} from {path}: {response.text[:200]}"
                )
                logger.warning(
                    "Retryable %s from %s, attempt %d/%d",
                    response.status_code,
                    path,
                    attempt + 1,
                    max_retries,
                )
                if attempt < max_retries:
                    reset = response.headers.get("x-rate-limit-reset")
                    if response.status_code == 429 and reset:
                        wait = max(0.0, float(reset) - time.time()) + 1.0
                        logger.info("Rate-limited; sleeping %.1fs until reset", wait)
                        await asyncio.sleep(wait)
                    else:
                        await asyncio.sleep(backoff)
                        backoff *= 2
                    continue

            if response.status_code >= 400:
                await self._log_call(
                    endpoint=log_endpoint or path,
                    status_code=response.status_code,
                    cost_usd=0.0,
                    tweets_returned=None,
                    notes=response.text[:500],
                )
                raise XApiError(
                    f"X API error {response.status_code}: {response.text[:500]}"
                )

            return response.json() if response.content else {}

        raise XApiError(f"Exhausted retries for {path}: {last_error}")

    async def _handle_rate_limits(self, response: httpx.Response) -> None:
        """Sleep proactively when we're close to exhausting a window."""
        remaining = response.headers.get("x-rate-limit-remaining")
        reset = response.headers.get("x-rate-limit-reset")
        if remaining is None or reset is None:
            return
        try:
            remaining_int = int(remaining)
            reset_float = float(reset)
        except ValueError:
            return
        if remaining_int < 5:
            wait = max(0.0, reset_float - time.time()) + 1.0
            if wait > 0:
                logger.info(
                    "Rate-limit guard: %d remaining; sleeping %.1fs",
                    remaining_int,
                    wait,
                )
                await asyncio.sleep(wait)

    async def _log_call(
        self,
        *,
        endpoint: str,
        status_code: int | None,
        cost_usd: float,
        tweets_returned: int | None,
        notes: str | None = None,
    ) -> None:
        try:
            async with session_scope() as session:
                await log_api_call(
                    session,
                    service="x_api",
                    endpoint=endpoint,
                    status_code=status_code,
                    cost_usd=cost_usd,
                    tweets_returned=tweets_returned,
                    notes=notes,
                )
        except Exception:
            logger.exception("Failed to log API call")


async def log_api_call(
    session: AsyncSession,
    *,
    service: str,
    endpoint: str,
    status_code: int | None,
    cost_usd: float,
    tweets_returned: int | None = None,
    notes: str | None = None,
) -> None:
    """Insert a row into `api_calls`. Used by every external call site."""
    session.add(
        ApiCall(
            service=service,
            endpoint=endpoint,
            status_code=status_code,
            cost_usd=Decimal(str(round(cost_usd, 6))),
            tweets_returned=tweets_returned,
            notes=notes,
        )
    )
