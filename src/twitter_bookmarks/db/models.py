"""SQLAlchemy 2.0 declarative models. See README/spec for the data model."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class Author(Base):
    __tablename__ = "authors"

    author_id: Mapped[str] = mapped_column(Text, primary_key=True)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    public_metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Tweet(Base):
    __tablename__ = "tweets"

    tweet_id: Mapped[str] = mapped_column(Text, primary_key=True)
    author_id: Mapped[str] = mapped_column(
        Text, ForeignKey("authors.author_id"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lang: Mapped[str | None] = mapped_column(Text)
    public_metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    entities: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    conversation_id: Mapped[str | None] = mapped_column(Text)
    in_reply_to_user_id: Mapped[str | None] = mapped_column(Text)
    referenced_tweets: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    author: Mapped[Author] = relationship(lazy="joined")


class Bookmark(Base):
    __tablename__ = "bookmarks"

    tweet_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tweets.tweet_id"), primary_key=True
    )
    bookmarked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    thread_root_id: Mapped[str | None] = mapped_column(Text)
    has_full_thread: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    tweet: Mapped[Tweet] = relationship(lazy="joined")

    __table_args__ = (
        Index("ix_bookmarks_bookmarked_at", "bookmarked_at", postgresql_using="btree"),
    )


class BookmarkThread(Base):
    __tablename__ = "bookmark_threads"

    bookmark_tweet_id: Mapped[str] = mapped_column(
        Text, ForeignKey("bookmarks.tweet_id"), primary_key=True
    )
    thread_tweet_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    full_thread_text: Mapped[str] = mapped_column(Text, nullable=False)
    reconstructed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_categories_slug", "slug"),)


class BookmarkClassification(Base):
    __tablename__ = "bookmark_classifications"

    tweet_id: Mapped[str] = mapped_column(
        Text, ForeignKey("bookmarks.tweet_id"), primary_key=True
    )
    category_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("categories.id"), nullable=False
    )
    sub_tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)
    gist: Mapped[str] = mapped_column(Text, nullable=False)
    classifier_reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    classifier_version: Mapped[str] = mapped_column(Text, nullable=False)
    is_user_corrected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    previous_category_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("categories.id")
    )
    classified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    category: Mapped[Category] = relationship(
        foreign_keys=[category_id], lazy="joined"
    )
    bookmark: Mapped[Bookmark] = relationship(lazy="joined")

    __table_args__ = (
        Index(
            "ix_bookmark_classifications_category_classified_at",
            "category_id",
            "classified_at",
        ),
        Index("ix_bookmark_classifications_is_user_corrected", "is_user_corrected"),
    )


class ClassifierFeedback(Base):
    __tablename__ = "classifier_feedback"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tweet_id: Mapped[str] = mapped_column(
        Text, ForeignKey("bookmarks.tweet_id"), nullable=False
    )
    predicted_category_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("categories.id")
    )
    correct_category_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("categories.id"), nullable=False
    )
    thread_text_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    corrected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    predicted_category: Mapped[Category | None] = relationship(
        foreign_keys=[predicted_category_id], lazy="joined"
    )
    correct_category: Mapped[Category] = relationship(
        foreign_keys=[correct_category_id], lazy="joined"
    )

    __table_args__ = (
        Index("ix_classifier_feedback_corrected_at", "corrected_at"),
    )


class TaxonomyProposal(Base):
    __tablename__ = "taxonomy_proposals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    proposal_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'finalized', 'superseded')",
            name="taxonomy_proposals_status_check",
        ),
    )


class Digest(Base):
    __tablename__ = "digests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    composed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_status: Mapped[str] = mapped_column(Text, nullable=False)
    email_message_id: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    html_body: Mapped[str] = mapped_column(Text, nullable=False)
    markdown_body: Mapped[str] = mapped_column(Text, nullable=False)
    bookmark_count: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "email_status IN ('pending', 'sent', 'failed')",
            name="digests_email_status_check",
        ),
        Index("ix_digests_period_start", "period_start"),
    )


class DigestSection(Base):
    __tablename__ = "digest_sections"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    digest_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("digests.id"), nullable=False
    )
    category_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("categories.id"), nullable=False
    )
    synthesis: Mapped[str] = mapped_column(Text, nullable=False)
    bookmark_tweet_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    composed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    category: Mapped[Category] = relationship(lazy="joined")


class WorkerState(Base):
    __tablename__ = "worker_state"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ApiCall(Base):
    __tablename__ = "api_calls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    called_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    service: Mapped[str] = mapped_column(Text, nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6), default=0, nullable=False)
    tweets_returned: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_api_calls_called_at", "called_at"),)
