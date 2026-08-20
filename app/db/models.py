"""SQLAlchemy ORM 模型 — DESIGN §5.1 DDL 参考快照

schema 唯一真源 = Alembic 迁移；此处为应用层查询接口。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


class Base(DeclarativeBase):
    pass


# ── feeds ──────────────────────────────────────────────────────────
class Feed(Base):
    __tablename__ = "feeds"
    __table_args__ = (UniqueConstraint("url", "env", name="feeds_url_env_uniq"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String, nullable=False)  # rss | api | scrape
    name: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    env: Mapped[str] = mapped_column(String, server_default="dev", default="dev")  # dev|prod，方案 C 双文件隔离
    config_json: Mapped[dict | None] = mapped_column(JSONB)
    etag: Mapped[str | None] = mapped_column(String)
    last_modified: Mapped[str | None] = mapped_column(String)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetch_status: Mapped[str | None] = mapped_column(String)
    fetch_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    articles: Mapped[list[Article]] = relationship(back_populates="feed")


# ── articles ───────────────────────────────────────────────────────
class Article(Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    feed_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("feeds.id", ondelete="SET NULL")
    )
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    url_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str | None] = mapped_column(String)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    content_text: Mapped[str | None] = mapped_column(Text)
    content_md: Mapped[str | None] = mapped_column(Text)
    lang: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending"
    )  # pending|processing|done|unparseable|error
    dedupe_of: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("articles.id", ondelete="SET NULL")
    )
    mention_count: Mapped[int] = mapped_column(Integer, default=1)
    word_count: Mapped[int | None] = mapped_column(Integer)
    tsv: Mapped[dict | None] = mapped_column(TSVECTOR)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    feed: Mapped[Feed | None] = relationship(back_populates="articles")
    versions: Mapped[list[ArticleVersion]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )
    embeddings: Mapped[list[ArticleEmbedding]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )
    jobs: Mapped[list[ProcessingJob]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )
    summary: Mapped[Summary | None] = relationship(
        back_populates="article", uselist=False, cascade="all, delete-orphan"
    )
    article_topics: Mapped[list[ArticleTopic]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )
    entities: Mapped[list["ArticleEntity"]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )


# ── article_versions ───────────────────────────────────────────────
class ArticleVersion(Base):
    __tablename__ = "article_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    article_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)  # raw_html | raw_text
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    article: Mapped[Article] = relationship(back_populates="versions")


# ── article_embeddings ─────────────────────────────────────────────
class ArticleEmbedding(Base):
    __tablename__ = "article_embeddings"
    __table_args__ = (
        UniqueConstraint("article_id", "kind", "model"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    article_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)  # title|summary|body
    model: Mapped[str] = mapped_column(String, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    # vector 列真实类型 = vector(1536)，由 a001_initial_schema 迁移手工 ALTER COLUMN
    # 设上去；SQLAlchemy 在此用 String 占位是因为项目当前没装 pgvector 包。
    # alembic autogenerate 会误报 vector 类型变更 → 这是已知约束，迁移手工管。
    # 装上 `pgvector>=0.3` 后可换 mapped_column(Vector(1536)) 自动对齐 schema。
    vector = mapped_column(String)  # noqa: placeholder; see comment above

    article: Mapped[Article] = relationship(back_populates="embeddings")


# ── processing_jobs ────────────────────────────────────────────────
class ProcessingJob(Base):
    __tablename__ = "processing_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    article_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False
    )
    task: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="queued"
    )  # queued|running|succeeded|failed|superseded
    content_hash: Mapped[str | None] = mapped_column(String)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    error_class: Mapped[str | None] = mapped_column(String)
    consecutive_timeouts: Mapped[int] = mapped_column(Integer, default=0)
    recover_count: Mapped[int] = mapped_column(Integer, default=0)
    priority: Mapped[int] = mapped_column(Integer, default=5)
    payload_json: Mapped[dict | None] = mapped_column(JSONB)
    result_json: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    lock_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    article: Mapped[Article] = relationship(back_populates="jobs")


# ── summaries ──────────────────────────────────────────────────────
class Summary(Base):
    __tablename__ = "summaries"
    __table_args__ = (UniqueConstraint("article_id", "lang", "model"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    article_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False
    )
    lang: Mapped[str] = mapped_column(String, default="zh")
    model: Mapped[str | None] = mapped_column(String)
    content_hash: Mapped[str | None] = mapped_column(String)
    summary_text: Mapped[str | None] = mapped_column(Text)
    key_points_json: Mapped[dict | None] = mapped_column(JSONB)
    confidence: Mapped[float | None] = mapped_column(Numeric)

    article: Mapped[Article] = relationship(back_populates="summary")


# ── topics ─────────────────────────────────────────────────────────
class Topic(Base):
    __tablename__ = "topics"
    __table_args__ = (UniqueConstraint("name", name="uq_topics_name"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    keywords_json: Mapped[dict | None] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    article_topics: Mapped[list[ArticleTopic]] = relationship(
        back_populates="topic", cascade="all, delete-orphan"
    )


# ── article_topics ─────────────────────────────────────────────────
class ArticleTopic(Base):
    __tablename__ = "article_topics"
    __table_args__ = (UniqueConstraint("article_id", "topic_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    article_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False
    )
    topic_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("topics.id", ondelete="CASCADE"), nullable=False
    )
    score: Mapped[float | None] = mapped_column(Numeric)
    method: Mapped[str | None] = mapped_column(String)  # keyword|llm

    article: Mapped[Article] = relationship(back_populates="article_topics")
    topic: Mapped[Topic] = relationship(back_populates="article_topics")


# ── wiki_pages ─────────────────────────────────────────────────────
class WikiPage(Base):
    __tablename__ = "wiki_pages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str | None] = mapped_column(String)  # article|topic|entity|manual
    ref_id: Mapped[int | None] = mapped_column(BigInteger)  # 多态引用
    title: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    content_md: Mapped[str | None] = mapped_column(Text)
    related_json: Mapped[dict | None] = mapped_column(JSONB)
    # tsv 列真实类型 = tsvector，由 a003_wiki_tsv 迁移手工加 + GIN 索引，
    # 应用层通过 app.db.fts.update_wiki_tsv 写入 jieba'd tsv（与 articles.tsv 同模式）。
    # alembic autogenerate 会误报 tsv 类型 → 已知约束，迁移手工管。
    tsv: Mapped[Any | None] = mapped_column(TSVECTOR)  # noqa: placeholder; see comment
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ── fetch_events ───────────────────────────────────────────────────
class FetchEvent(Base):
    __tablename__ = "fetch_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    feed_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("feeds.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str | None] = mapped_column(String)
    ok: Mapped[bool | None] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text)
    item_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ── translations（DESIGN §5.1，Phase 2 切片 2.7） ──────────────────
class Translation(Base):
    __tablename__ = "translations"
    __table_args__ = (
        UniqueConstraint("article_id", "src_lang", "tgt_lang", "model",
                         name="uq_translations_article_lang_model"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    article_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False
    )
    src_lang: Mapped[str | None] = mapped_column(String)
    tgt_lang: Mapped[str | None] = mapped_column(String, default="zh")
    model: Mapped[str | None] = mapped_column(String)
    content_hash: Mapped[str | None] = mapped_column(String)
    translated_title: Mapped[str | None] = mapped_column(Text)
    translated_content: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ── entities（DESIGN §5.1/§5.1.5，Phase 2 切片 2.3） ───────────────
class Entity(Base):
    __tablename__ = "entities"
    __table_args__ = (
        UniqueConstraint("entity_type", "canonical_name_zh",
                         name="entities_uniq_per_type_zh"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    canonical_name_zh: Mapped[str] = mapped_column(Text, nullable=False)
    aliases_json: Mapped[dict | None] = mapped_column(JSONB)
    entity_type: Mapped[str | None] = mapped_column(String)  # person|org|product|model|...
    description: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mention_count: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[float | None] = mapped_column(Numeric)

    articles: Mapped[list["ArticleEntity"]] = relationship(
        back_populates="entity", cascade="all, delete-orphan"
    )
    relations_as_subject: Mapped[list["Relation"]] = relationship(
        back_populates="subject",
        foreign_keys="Relation.subject_id",
        cascade="all, delete-orphan",
    )
    relations_as_object: Mapped[list["Relation"]] = relationship(
        back_populates="object",
        foreign_keys="Relation.object_id",
        cascade="all, delete-orphan",
    )


# ── article_entities（DESIGN §6.X，Phase 2 切片 2.3） ──────────────
class ArticleEntity(Base):
    __tablename__ = "article_entities"

    article_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("articles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    entity_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("entities.id", ondelete="CASCADE"),
        primary_key=True,
    )
    confidence: Mapped[float | None] = mapped_column(Numeric)
    surface: Mapped[str | None] = mapped_column(Text)  # 该 entity 在本文出现的原文子串
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    article: Mapped[Article] = relationship(back_populates="entities")
    entity: Mapped[Entity] = relationship(back_populates="articles")


# ── relations（DESIGN §5.1/§5.1.5，Phase 2 切片 2.3/2.4） ───────────
class Relation(Base):
    __tablename__ = "relations"
    __table_args__ = (
        UniqueConstraint("subject_id", "predicate", "object_id",
                         name="relations_uniq_spo"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subject_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    predicate: Mapped[str | None] = mapped_column(String)
    object_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    # source_article_id 改为可选（兼容 Phase 2 §5.1.5：来源文章改用 JSONB 数组）
    source_article_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("articles.id", ondelete="SET NULL")
    )
    source_articles_json: Mapped[dict] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False,
    )
    confidence: Mapped[float | None] = mapped_column(Numeric)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    subject: Mapped[Entity] = relationship(
        back_populates="relations_as_subject", foreign_keys=[subject_id],
    )
    object: Mapped[Entity] = relationship(
        back_populates="relations_as_object", foreign_keys=[object_id],
    )


# ── reports（DESIGN §5.1/§5.1.5，Phase 2 切片 2.5） ────────────────
class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    report_type: Mapped[str | None] = mapped_column(String)  # daily|weekly
    period_start: Mapped[Any | None] = mapped_column(Date)  # noqa: placeholder
    period_end: Mapped[Any | None] = mapped_column(Date)  # noqa: placeholder
    content_md: Mapped[str | None] = mapped_column(Text)
    content_html: Mapped[str | None] = mapped_column(Text)
    stats_json: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending",
    )  # pending|running|succeeded|failed
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
