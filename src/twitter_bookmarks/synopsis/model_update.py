"""Per-category 'how this week's bookmarks update my thinking' synthesis.

For each category that has a current baseline view AND new bookmarks in
the digest period, Opus is given:
  - the baseline view ('this is your current take')
  - the per-tweet synopses of this week's bookmarks
  - the underlying tweet/article text
…and asked to write 100-200 words on how the week's bookmarks shift,
support, contradict, or extend the baseline. Specific. Names names.

The output goes into `digest_sections.model_update_text`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from anthropic import AsyncAnthropic
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from twitter_bookmarks.config import get_settings
from twitter_bookmarks.db.models import Category, CategoryView
from twitter_bookmarks.db.session import session_scope
from twitter_bookmarks.x_api.client import log_api_call

logger = logging.getLogger(__name__)

OPUS_INPUT_PER_MTOK = 15.00
OPUS_OUTPUT_PER_MTOK = 75.00

UPDATE_TOOL = {
    "name": "record_model_update",
    "description": (
        "Record a 100-200 word reflection on how this week's bookmarks "
        "in this category shift, support, contradict, or extend the "
        "user's existing view (provided as the baseline). Be concrete: "
        "name the specific bookmarks (by @username) that drive each "
        "point. If the week's bookmarks don't actually change the view "
        "much, say so plainly — don't manufacture insight."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "movement": {
                "type": "string",
                "enum": ["shifts", "supports", "contradicts", "extends", "minimal"],
                "description": (
                    "Which mode best characterizes how this week's "
                    "bookmarks relate to the baseline."
                ),
            },
            "update_text": {
                "type": "string",
                "description": (
                    "100-200 words. Markdown allowed. Reference specific "
                    "@usernames for concrete points."
                ),
            },
        },
        "required": ["movement", "update_text"],
    },
}


class _UpdatePayload(BaseModel):
    movement: str
    update_text: str


def _system_prompt(category: Category) -> str:
    return (
        "You are writing one week's reflection on how the user's "
        "bookmarks update their evolving view in a single category. "
        "You'll be given the user's CURRENT VIEW (a baseline) and this "
        "week's NEW BOOKMARKS with structured synopses.\n\n"
        f"CATEGORY: {category.name} ({category.slug})\n\n"
        "Write 100-200 words covering:\n"
        "- Which baseline positions does this week's reading reinforce, "
        "  challenge, or refine?\n"
        "- Are there new threads, debates, or framings this week "
        "  introduces that aren't in the baseline?\n"
        "- Reference @usernames for concrete points.\n\n"
        "If the week's bookmarks don't materially change the view, say "
        "that plainly (e.g., 'this week mostly extends prior threads on "
        "X without introducing new framings'). Do not manufacture "
        "insight. Do not hedge.\n\n"
        "Always respond by calling the record_model_update tool."
    )


def _format_recent(bookmarks: list[dict[str, Any]]) -> str:
    parts = []
    for b in bookmarks:
        parts.append(
            f"---\n@{b['username']} ({b['tweet_id']})\n"
            f"synopsis: {b['gist']}\n"
            f"text: {b['text_excerpt']}"
        )
    return "\n\n".join(parts)


def _estimate_cost(usage: Any) -> float:
    if not usage:
        return 0.0
    in_tokens = getattr(usage, "input_tokens", 0)
    out_tokens = getattr(usage, "output_tokens", 0)
    return (
        in_tokens / 1_000_000 * OPUS_INPUT_PER_MTOK
        + out_tokens / 1_000_000 * OPUS_OUTPUT_PER_MTOK
    )


async def generate_model_update(
    session: AsyncSession,
    category: Category,
    baseline: CategoryView,
    week_bookmarks: list[dict[str, Any]],
) -> str | None:
    """Generate the model-update text for one category. Returns None on skip."""
    if not week_bookmarks:
        return None

    settings = get_settings()
    user_msg = (
        f"CURRENT VIEW (baseline, generated {baseline.generated_at:%Y-%m-%d}):\n"
        f"{baseline.view_text}\n\n"
        f"NEW BOOKMARKS THIS WEEK ({len(week_bookmarks)}):\n"
        f"{_format_recent(week_bookmarks)}"
    )

    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    response = await client.messages.create(
        model=settings.OPUS_MODEL,
        max_tokens=1024,
        system=_system_prompt(category),
        tools=[UPDATE_TOOL],
        tool_choice={"type": "tool", "name": "record_model_update"},
        messages=[{"role": "user", "content": user_msg}],
    )

    cost = _estimate_cost(response.usage)
    async with session_scope() as cost_session:
        await log_api_call(
            cost_session,
            service="anthropic",
            endpoint=f"messages:{settings.OPUS_MODEL}:model_update",
            status_code=200,
            cost_usd=cost,
            tweets_returned=len(week_bookmarks),
            notes=f"category={category.slug}",
        )

    tool_input: dict[str, Any] | None = None
    for block in response.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and block.name == "record_model_update"
        ):
            tool_input = block.input  # type: ignore[assignment]
            break
    if tool_input is None:
        logger.warning(
            "Opus did not call record_model_update for %s. Got: %s",
            category.slug,
            json.dumps([b.model_dump() for b in response.content])[:300],
        )
        return None

    payload = _UpdatePayload.model_validate(tool_input)
    return payload.update_text.strip()
