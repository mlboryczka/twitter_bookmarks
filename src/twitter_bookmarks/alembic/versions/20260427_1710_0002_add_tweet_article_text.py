"""add tweets.article_text

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-27 17:10:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tweets", sa.Column("article_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tweets", "article_text")
