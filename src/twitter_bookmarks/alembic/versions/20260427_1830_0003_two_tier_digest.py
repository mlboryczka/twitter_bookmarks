"""two-tier digest scaffolding

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-27 18:30:00.000000

Adds:
  - categories.enable_model_update (bool) — flags categories that get the
    model-update tier in the weekly digest.
  - category_views — versioned baselines per category (Opus-generated
    "current view" synthesized from bookmarks older than BASELINE_AGE_DAYS).
  - digest_sections.model_update_text — the per-week reflection on how the
    week's bookmarks shift the baseline.
  - external_sources — placeholder for non-bookmark signals (RSS,
    newsletters, podcast transcripts) that future versions will mix in.

Also flips enable_model_update to true for every category whose slug isn't
'career-jobs-vc' or 'misc' (the two informational ones).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "categories",
        sa.Column(
            "enable_model_update",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    op.create_table(
        "category_views",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True
        ),
        sa.Column(
            "category_id",
            sa.BigInteger(),
            sa.ForeignKey("categories.id"),
            nullable=False,
        ),
        sa.Column("view_text", sa.Text(), nullable=False),
        sa.Column(
            "source_bookmark_count", sa.Integer(), nullable=False
        ),
        sa.Column(
            "source",
            sa.Text(),
            nullable=False,
            server_default="auto",
        ),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "model",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.CheckConstraint(
            "source IN ('auto', 'manual')",
            name="category_views_source_check",
        ),
    )
    op.create_index(
        "ix_category_views_category_generated",
        "category_views",
        ["category_id", sa.text("generated_at DESC")],
    )

    op.add_column(
        "digest_sections",
        sa.Column("model_update_text", sa.Text(), nullable=True),
    )
    op.add_column(
        "digest_sections",
        sa.Column(
            "baseline_view_id",
            sa.BigInteger(),
            sa.ForeignKey("category_views.id"),
            nullable=True,
        ),
    )

    op.create_table(
        "external_sources",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column(
            "category_id",
            sa.BigInteger(),
            sa.ForeignKey("categories.id"),
            nullable=True,
        ),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "is_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.CheckConstraint(
            "kind IN ('rss', 'substack', 'newsletter', 'podcast', 'other')",
            name="external_sources_kind_check",
        ),
    )

    # Default-on for opinion-forming categories. Career/Jobs and Misc stay
    # off because they're informational, not view-shaping.
    op.execute(
        """
        UPDATE categories
        SET enable_model_update = TRUE
        WHERE slug NOT IN ('career-jobs-vc', 'misc')
        """
    )


def downgrade() -> None:
    op.drop_table("external_sources")
    op.drop_constraint(
        "digest_sections_baseline_view_id_fkey",
        "digest_sections",
        type_="foreignkey",
    )
    op.drop_column("digest_sections", "baseline_view_id")
    op.drop_column("digest_sections", "model_update_text")
    op.drop_index(
        "ix_category_views_category_generated", table_name="category_views"
    )
    op.drop_table("category_views")
    op.drop_column("categories", "enable_model_update")
