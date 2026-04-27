"""Resend email delivery for digests.

The Resend Python SDK is synchronous; we run it in a thread executor
because the rest of the app is async. We log every send attempt to
api_calls so it shows up alongside X and Anthropic spend.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import resend

from twitter_bookmarks.config import get_settings
from twitter_bookmarks.db.models import Digest
from twitter_bookmarks.db.session import session_scope
from twitter_bookmarks.x_api.client import log_api_call

logger = logging.getLogger(__name__)


def _send_sync(payload: dict[str, Any]) -> dict[str, Any]:
    return resend.Emails.send(payload)  # type: ignore[no-any-return]


async def send_digest(digest: Digest) -> Digest:
    """Send a composed digest via Resend and update its email status row."""
    settings = get_settings()
    if not settings.RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY not configured")
    if not settings.DIGEST_FROM_EMAIL or not settings.DIGEST_TO_EMAIL:
        raise RuntimeError(
            "DIGEST_FROM_EMAIL and DIGEST_TO_EMAIL must both be set"
        )

    resend.api_key = settings.RESEND_API_KEY
    payload = {
        "from": settings.DIGEST_FROM_EMAIL,
        "to": settings.DIGEST_TO_EMAIL,
        "subject": digest.subject,
        "html": digest.html_body,
        "text": digest.markdown_body,
    }

    try:
        result = await asyncio.to_thread(_send_sync, payload)
        message_id = result.get("id") if isinstance(result, dict) else None
        digest.sent_at = datetime.now(timezone.utc)
        digest.email_status = "sent"
        digest.email_message_id = message_id
        async with session_scope() as session:
            await log_api_call(
                session,
                service="resend",
                endpoint="emails.send",
                status_code=200,
                cost_usd=0.0,
                tweets_returned=None,
                notes=f"digest_id={digest.id} message_id={message_id}",
            )
        logger.info("Digest %d sent (message_id=%s)", digest.id, message_id)
    except Exception as exc:
        digest.email_status = "failed"
        digest.sent_at = None
        async with session_scope() as session:
            await log_api_call(
                session,
                service="resend",
                endpoint="emails.send",
                status_code=None,
                cost_usd=0.0,
                tweets_returned=None,
                notes=f"digest_id={digest.id} error={exc}",
            )
        logger.exception("Digest %d send failed", digest.id)
        raise

    return digest
