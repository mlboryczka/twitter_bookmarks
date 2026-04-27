"""Standalone OAuth 2.0 PKCE flow against the X API.

Run on your *local* machine — your X developer app must list
http://localhost:8765/callback as an authorized redirect URI.

Usage:
    uv run python scripts/run_oauth_flow.py

The script:
  1. Generates a PKCE code verifier and challenge
  2. Starts a local FastAPI server on :8765 that catches the callback
  3. Opens the X authorization URL in your browser
  4. Exchanges the auth code for tokens
  5. Prints the refresh token (paste into VPS .env as X_REFRESH_TOKEN)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import sys
import threading
import urllib.parse
import webbrowser

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

# Load .env from project root
sys.path.insert(0, "src")
from twitter_bookmarks.config import get_settings  # noqa: E402

AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
SCOPES = "tweet.read users.read bookmark.read offline.access"


def _pkce_pair() -> tuple[str, str]:
    """Return (verifier, challenge) for OAuth 2.0 PKCE S256."""
    verifier = (
        base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    )
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


async def _exchange(code: str, verifier: str) -> dict[str, str]:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.X_REDIRECT_URI,
                "client_id": settings.X_CLIENT_ID,
                "code_verifier": verifier,
            },
            auth=(settings.X_CLIENT_ID, settings.X_CLIENT_SECRET),
        )
    response.raise_for_status()
    return response.json()


def main() -> None:
    settings = get_settings()
    if not settings.X_CLIENT_ID or not settings.X_CLIENT_SECRET:
        sys.exit("X_CLIENT_ID and X_CLIENT_SECRET must be set in .env")

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)

    auth_url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": settings.X_CLIENT_ID,
            "redirect_uri": settings.X_REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )

    app = FastAPI()
    done = threading.Event()
    result: dict[str, str] = {}

    @app.get("/callback")
    async def callback(request: Request) -> HTMLResponse:  # noqa: D401
        params = dict(request.query_params)
        if params.get("state") != state:
            return HTMLResponse("State mismatch", status_code=400)
        code = params.get("code")
        if not code:
            return HTMLResponse(f"Error: {params}", status_code=400)
        try:
            tokens = await _exchange(code, verifier)
        except Exception as exc:
            return HTMLResponse(f"Token exchange failed: {exc}", status_code=500)
        result.update(tokens)
        done.set()
        return HTMLResponse(
            "<h2>Done. You can close this tab.</h2>"
            "<p>Refresh token printed in your terminal.</p>"
        )

    config = uvicorn.Config(
        app, host="127.0.0.1", port=8765, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)

    def _run() -> None:
        asyncio.run(server.serve())

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    print(f"\nOpening browser to:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    # Wait up to 5 minutes for the user to complete consent
    if not done.wait(timeout=300):
        sys.exit("Timed out waiting for OAuth callback")

    server.should_exit = True

    print("\n=== TOKENS ===")
    print(f"access_token:  {result.get('access_token', '')[:32]}…")
    print(f"refresh_token: {result.get('refresh_token', '')}")
    print(f"expires_in:    {result.get('expires_in')}")
    print(f"scope:         {result.get('scope')}")
    print(
        "\nCopy the refresh_token into your .env as X_REFRESH_TOKEN."
        "\nThe token rotates on every refresh; the runtime persists rotations"
        " to secrets/x_refresh_token (mode 0600)."
    )


if __name__ == "__main__":
    main()
