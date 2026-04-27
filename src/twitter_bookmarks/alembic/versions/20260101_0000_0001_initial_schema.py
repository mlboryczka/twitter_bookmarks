"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-01-01 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "authors",
        sa.Column("author_id", sa.Text(), primary_key=True),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "public_metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "tweets",
        sa.Column("tweet_id", sa.Text(), primary_key=True),
        sa.Column(
            "author_id",
            sa.Text(),
            sa.ForeignKey("authors.author_id"),
            nullable=False,
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lang", sa.Text()),
        sa.Column(
            "public_metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("entities", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("conversation_id", sa.Text()),
        sa.Column("in_reply_to_user_id", sa.Text()),
        sa.Column("referenced_tweets", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("raw_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "bookmarks",
        sa.Column(
            "tweet_id",
            sa.Text(),
            sa.ForeignKey("tweets.tweet_id"),
            primary_key=True,
        ),
        sa.Column("bookmarked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("thread_root_id", sa.Text()),
        sa.Column(
            "has_full_thread",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_bookmarks_bookmarked_at",
        "bookmarks",
        [sa.text("bookmarked_at DESC")],
    )

    op.create_table(
        "bookmark_threads",
        sa.Column(
            "bookmark_tweet_id",
            sa.Text(),
            sa.ForeignKey("bookmarks.tweet_id"),
            primary_key=True,
        ),
        sa.Column(
            "thread_tweet_ids",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
        ),
        sa.Column("full_thread_text", sa.Text(), nullable=False),
        sa.Column(
            "reconstructed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "categories",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True
        ),
        sa.Column("slug", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_categories_slug", "categories", ["slug"])

    op.create_table(
        "bookmark_classifications",
        sa.Column(
            "tweet_id",
            sa.Text(),
            sa.ForeignKey("bookmarks.tweet_id"),
            primary_key=True,
        ),
        sa.Column(
            "category_id",
            sa.BigInteger(),
            sa.ForeignKey("categories.id"),
            nullable=False,
        ),
        sa.Column(
            "sub_tags",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("gist", sa.Text(), nullable=False),
        sa.Column("classifier_reasoning", sa.Text(), nullable=False),
        sa.Column("classifier_version", sa.Text(), nullable=False),
        sa.Column(
            "is_user_corrected",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "previous_category_id",
            sa.BigInteger(),
            sa.ForeignKey("categories.id"),
        ),
        sa.Column(
            "classified_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_bookmark_classifications_category_classified_at",
        "bookmark_classifications",
        ["category_id", sa.text("classified_at DESC")],
    )
    op.create_index(
        "ix_bookmark_classifications_is_user_corrected",
        "bookmark_classifications",
        ["is_user_corrected"],
    )

    op.create_table(
        "classifier_feedback",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True
        ),
        sa.Column(
            "tweet_id",
            sa.Text(),
            sa.ForeignKey("bookmarks.tweet_id"),
            nullable=False,
        ),
        sa.Column(
            "predicted_category_id",
            sa.BigInteger(),
            sa.ForeignKey("categories.id"),
        ),
        sa.Column(
            "correct_category_id",
            sa.BigInteger(),
            sa.ForeignKey("categories.id"),
            nullable=False,
        ),
        sa.Column("thread_text_snapshot", sa.Text(), nullable=False),
        sa.Column(
            "corrected_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_classifier_feedback_corrected_at",
        "classifier_feedback",
        [sa.text("corrected_at DESC")],
    )

    op.create_table(
        "taxonomy_proposals",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True
        ),
        sa.Column(
            "proposal_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finalized_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('draft', 'finalized', 'superseded')",
            name="taxonomy_proposals_status_check",
        ),
    )

    op.create_table(
        "digests",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True
        ),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("composed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("email_status", sa.Text(), nullable=False),
        sa.Column("email_message_id", sa.Text()),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("html_body", sa.Text(), nullable=False),
        sa.Column("markdown_body", sa.Text(), nullable=False),
        sa.Column("bookmark_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "email_status IN ('pending', 'sent', 'failed')",
            name="digests_email_status_check",
        ),
    )
    op.create_index(
        "ix_digests_period_start",
        "digests",
        [sa.text("period_start DESC")],
    )

    op.create_table(
        "digest_sections",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True
        ),
        sa.Column(
            "digest_id",
            sa.BigInteger(),
            sa.ForeignKey("digests.id"),
            nullable=False,
        ),
        sa.Column(
            "category_id",
            sa.BigInteger(),
            sa.ForeignKey("categories.id"),
            nullable=False,
        ),
        sa.Column("synthesis", sa.Text(), nullable=False),
        sa.Column(
            "bookmark_tweet_ids",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "composed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "worker_state",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "api_calls",
        sa.Column(
            "id", sa.BigInteger(), primary_key=True, autoincrement=True
        ),
        sa.Column(
            "called_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("service", sa.Text(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("status_code", sa.Integer()),
        sa.Column(
            "cost_usd",
            sa.Numeric(10, 6),
            nullable=False,
            server_default="0",
        ),
        sa.Column("tweets_returned", sa.Integer()),
        sa.Column("notes", sa.Text()),
    )
    op.create_index(
        "ix_api_calls_called_at",
        "api_calls",
        [sa.text("called_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_api_calls_called_at", table_name="api_calls")
    op.drop_table("api_calls")
    op.drop_table("worker_state")
    op.drop_table("digest_sections")
    op.drop_index("ix_digests_period_start", table_name="digests")
    op.drop_table("digests")
    op.drop_table("taxonomy_proposals")
    op.drop_index(
        "ix_classifier_feedback_corrected_at", table_name="classifier_feedback"
    )
    op.drop_table("classifier_feedback")
    op.drop_index(
        "ix_bookmark_classifications_is_user_corrected",
        table_name="bookmark_classifications",
    )
    op.drop_index(
        "ix_bookmark_classifications_category_classified_at",
        table_name="bookmark_classifications",
    )
    op.drop_table("bookmark_classifications")
    op.drop_index("ix_categories_slug", table_name="categories")
    op.drop_table("categories")
    op.drop_table("bookmark_threads")
    op.drop_index("ix_bookmarks_bookmarked_at", table_name="bookmarks")
    op.drop_table("bookmarks")
    op.drop_table("tweets")
    op.drop_table("authors")
