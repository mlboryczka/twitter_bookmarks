"""Markdown and HTML rendering for digests.

The HTML renderer produces email-safe table-based markup with inline CSS
suitable for Resend → Gmail/Apple Mail. The markdown renderer produces a
plain-text-ish version we keep for the archive's source-of-truth.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from twitter_bookmarks.db.models import (
    Author,
    Category,
    Digest,
    DigestSection,
    Tweet,
)


async def _hydrate_section_bookmarks(
    session: AsyncSession,
    section: DigestSection,
) -> list[dict[str, Any]]:
    if not section.bookmark_tweet_ids:
        return []
    rows = (
        await session.execute(
            select(Tweet.tweet_id, Tweet.text, Author.username)
            .join(Author, Author.author_id == Tweet.author_id)
            .where(Tweet.tweet_id.in_(section.bookmark_tweet_ids))
        )
    ).all()
    text_by_id = {tid: text for tid, text, _ in rows}
    user_by_id = {tid: username for tid, _, username in rows}
    out = []
    for tid in section.bookmark_tweet_ids:
        text = text_by_id.get(tid, "")
        out.append(
            {
                "tweet_id": tid,
                "text": text,
                "username": user_by_id.get(tid, "unknown"),
                "url": f"https://x.com/i/web/status/{tid}",
            }
        )
    return out


async def render_markdown(
    session: AsyncSession,
    digest: Digest,
    sections: list[DigestSection],
) -> str:
    """Plain markdown — used for archive and for Resend's text fallback."""
    parts = [
        f"# {digest.subject}",
        "",
        f"_{digest.bookmark_count} bookmarks · {digest.period_start} → {digest.period_end}_",
        "",
    ]
    for section in sections:
        category = await session.get(Category, section.category_id)
        cat_name = category.name if category else "(unknown)"
        bookmarks = await _hydrate_section_bookmarks(session, section)
        parts.append(f"## {cat_name}")
        parts.append("")
        parts.append(section.synthesis.strip())
        parts.append("")
        for b in bookmarks:
            text = (b["text"] or "").strip().replace("\n", " ")
            if len(text) > 280:
                text = text[:280] + "…"
            parts.append(f"- [@{b['username']}]({b['url']}): {text}")
        parts.append("")
    return "\n".join(parts)


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


async def render_html(
    session: AsyncSession,
    digest: Digest,
    sections: list[DigestSection],
) -> str:
    """Email-safe HTML with inline styles. Uses simple table layout."""
    import markdown as md

    body_parts = [
        '<!DOCTYPE html>',
        '<html><head><meta charset="utf-8">',
        f'<title>{_escape_html(digest.subject)}</title>',
        '</head>',
        '<body style="font-family: -apple-system, BlinkMacSystemFont, '
        'Helvetica, Arial, sans-serif; max-width: 640px; margin: 0 auto; '
        'padding: 24px; color: #1d1d1f; background: #ffffff;">',
        '<table width="100%" cellpadding="0" cellspacing="0" border="0">',
        '<tr><td>',
        f'<h1 style="font-size: 22px; margin: 0 0 4px;">'
        f'{_escape_html(digest.subject)}</h1>',
        f'<p style="color: #6e6e73; margin: 0 0 24px; font-size: 14px;">'
        f'{digest.bookmark_count} bookmarks · '
        f'{digest.period_start} → {digest.period_end}</p>',
    ]

    for section in sections:
        category = await session.get(Category, section.category_id)
        cat_name = category.name if category else "(unknown)"
        bookmarks = await _hydrate_section_bookmarks(session, section)

        body_parts.append(
            f'<h2 style="font-size: 18px; margin: 32px 0 8px; '
            f'border-bottom: 1px solid #e0e0e0; padding-bottom: 4px;">'
            f'{_escape_html(cat_name)}</h2>'
        )
        # Render synthesis markdown to HTML.
        synthesis_html = md.markdown(section.synthesis, extensions=["extra"])
        body_parts.append(
            f'<div style="font-size: 15px; line-height: 1.5;">{synthesis_html}</div>'
        )
        # Bookmark list.
        body_parts.append('<ul style="padding-left: 18px; margin: 12px 0;">')
        for b in bookmarks:
            text = (b["text"] or "").strip().replace("\n", " ")
            if len(text) > 280:
                text = text[:280] + "…"
            body_parts.append(
                f'<li style="margin-bottom: 8px; font-size: 14px;">'
                f'<a href="{b["url"]}" style="color: #007aff; text-decoration: none;">'
                f'@{_escape_html(b["username"])}</a>: '
                f'{_escape_html(text)}</li>'
            )
        body_parts.append('</ul>')

    body_parts.extend(
        [
            '<hr style="border: 0; border-top: 1px solid #e0e0e0; margin: 32px 0;">',
            '<p style="font-size: 12px; color: #6e6e73;">'
            "Sent by your private twitter_bookmarks instance.</p>",
            '</td></tr></table>',
            '</body></html>',
        ]
    )
    return "\n".join(body_parts)
