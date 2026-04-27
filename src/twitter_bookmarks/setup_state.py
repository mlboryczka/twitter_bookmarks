"""Helpers for reading/writing the app's setup_phase flag in worker_state."""

from __future__ import annotations

from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from twitter_bookmarks.db.models import WorkerState

SetupPhase = Literal[
    "not_started",
    "fetching_bookmarks",
    "awaiting_taxonomy_review",
    "classifying_backfill",
    "awaiting_classification_review",
    "complete",
]

VALID_PHASES: tuple[SetupPhase, ...] = (
    "not_started",
    "fetching_bookmarks",
    "awaiting_taxonomy_review",
    "classifying_backfill",
    "awaiting_classification_review",
    "complete",
)

_KEY = "setup_phase"


async def get_setup_phase(session: AsyncSession) -> SetupPhase:
    """Return the current setup phase, defaulting to 'not_started'."""
    result = await session.execute(select(WorkerState).where(WorkerState.key == _KEY))
    row = result.scalar_one_or_none()
    if row is None:
        return "not_started"
    value = row.value
    if isinstance(value, dict):
        phase = value.get("phase", "not_started")
    else:
        phase = value
    if phase not in VALID_PHASES:
        return "not_started"
    return phase  # type: ignore[return-value]


async def set_setup_phase(session: AsyncSession, phase: SetupPhase) -> None:
    """Persist the setup phase."""
    if phase not in VALID_PHASES:
        raise ValueError(f"Invalid setup phase: {phase}")
    stmt = pg_insert(WorkerState).values(key=_KEY, value={"phase": phase})
    stmt = stmt.on_conflict_do_update(
        index_elements=[WorkerState.key],
        set_={"value": {"phase": phase}, "updated_at": func.now()},
    )
    await session.execute(stmt)
