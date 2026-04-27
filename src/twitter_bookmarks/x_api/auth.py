"""OAuth 2.0 token management for the X API.

The OAuth flow itself (browser, PKCE) is handled by `scripts/run_oauth_flow.py`.
This module is responsible only for the runtime concern of holding an access
token and refreshing it via the rotated refresh token.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import httpx

from twitter_bookmarks.config import get_settings

logger = logging.getLogger(__name__)

TOKEN_URL = "https://api.x.com/2/oauth2/token"


class XAuth:
    """Holds the current X API access token and rotates the refresh token.

    The refresh token rotates on every use; we persist the latest value to
    a file (`X_REFRESH_TOKEN_FILE`, mode 0600) so that a process restart
    doesn't lose access. The `.env` value is the bootstrap token only —
    after the first refresh, the file is the source of truth.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._access_token: str | None = None
        self._access_token_expires_at: float = 0.0
        self._lock = asyncio.Lock()

    def _refresh_token_path(self) -> Path:
        return Path(self._settings.X_REFRESH_TOKEN_FILE)

    def _load_refresh_token(self) -> str:
        path = self._refresh_token_path()
        if path.exists():
            token = path.read_text().strip()
            if token:
                return token
        return self._settings.X_REFRESH_TOKEN

    def _store_refresh_token(self, token: str) -> None:
        path = self._refresh_token_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token)
        os.chmod(path, 0o600)

    async def get_access_token(self) -> str:
        """Return a valid access token, refreshing if expired."""
        async with self._lock:
            if self._access_token and time.time() < self._access_token_expires_at - 60:
                return self._access_token
            await self._refresh()
            assert self._access_token is not None
            return self._access_token

    async def _refresh(self) -> None:
        refresh_token = self._load_refresh_token()
        if not refresh_token:
            raise RuntimeError(
                "No X refresh token available. Run scripts/run_oauth_flow.py first."
            )
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self._settings.X_CLIENT_ID,
                },
                auth=(self._settings.X_CLIENT_ID, self._settings.X_CLIENT_SECRET),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if response.status_code != 200:
            raise RuntimeError(
                f"X token refresh failed: {response.status_code} {response.text}"
            )
        payload = response.json()
        self._access_token = payload["access_token"]
        self._access_token_expires_at = time.time() + int(payload.get("expires_in", 7200))
        new_refresh = payload.get("refresh_token")
        if new_refresh and new_refresh != refresh_token:
            self._store_refresh_token(new_refresh)
            logger.info("X refresh token rotated")
