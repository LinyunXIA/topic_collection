"""初始 schema — DESIGN §5.1

Revision ID: 001_initial
Create Date: 2026-08-18
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 注意：CREATE EXTENSION vector 由 scripts/init_db.py 负责，不写在迁移里

    # ── feeds ──────────────────────────────────────────────────
    op.create_table(
        "feeds",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("type", sa.String, nullable=False),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("enabled", sa.Boolean, server_default=sa.text("true")),
        sa.Column("config_json", postgresql.JSONB()),
        sa.Column("etag", sa.String),
        sa.Column("last_modified", sa.String),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True)),
        sa.Column("fetch_status", sa.String),
        sa.Column("fetch_failures", sa.Integer, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── articles ───────────────────────────────────────────────
    op.create_table(
        "articles",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("feed_id", sa.BigInteger, sa.ForeignKey("feeds.id", ondelete="SET NULL")),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("url_hash", sa.String, unique=True, nullable=False),
        sa.Column("content_hash", sa.String, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("author", sa.String),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("content_text", sa.Text),
        sa.Column("content_md", sa.Text),
        sa.Column("lang", sa.String),
        sa.Column(
            "status", sa.String, nullable=False, server_default="pending",
            comment="pending|processing|done|unparseable|error",
        ),
        sa.Column("dedupe_of", sa.BigInteger, sa.ForeignKey("articles.id", ondelete="SET NULL")),
        sa.Column("mention_count", sa.Integer, server_default=sa.text("1")),
        sa.Column("word_count", sa.Integer),
        sa.Column("tsv", postgresql.TSVECTOR()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "articles_tsv_idx", "articles", ["tsv"],
        postgresql_using="gin",
    )

    # ── article_versions ───────────────────────────────────────
    op.create_table(
        "article_versions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("article_id", sa.BigInteger, sa.ForeignKey("articles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String, nullable=False, comment="raw_html|raw_text"),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── article_embeddings ─────────────────────────────────────
    op.create_table(
        "article_embeddings",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("article_id", sa.BigInteger, sa.ForeignKey("articles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String, nullable=False, comment="title|summary|body"),
        sa.Column("model", sa.String, nullable=False),
        sa.Column("content_hash", sa.String, nullable=False),
        sa.Column("dim", sa.Integer, nullable=False),
        sa.Column("vector", postgresql.ARRAY(sa.Float)),  # placeholder; actual type = vector(1536) set raw
        sa.UniqueConstraint("article_id", "kind", "model"),
    )
    # 使用 raw SQL 创建 vector 列和 HNSW 索引（Alembic 不原生支持 pgvector 类型）
    op.execute("ALTER TABLE article_embeddings DROP COLUMN vector")
    op.execute("ALTER TABLE article_embeddings ADD COLUMN vector vector(1536)")
    op.execute(
        "CREATE INDEX emb_hnsw_idx ON article_embeddings "
        "USING hnsw (vector vector_cosine_ops) "
        "WITH (ef_construction=128)"
    )

    # ── processing_jobs ────────────────────────────────────────
    op.create_table(
        "processing_jobs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("article_id", sa.BigInteger, sa.ForeignKey("articles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task", sa.String, nullable=False),
        sa.Column(
            "status", sa.String, nullable=False, server_default="queued",
            comment="queued|running|succeeded|failed|superseded",
        ),
        sa.Column("content_hash", sa.String),
        sa.Column("attempt", sa.Integer, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer, server_default=sa.text("3")),
        sa.Column("error_class", sa.String, comment="transient|permanent"),
        sa.Column("consecutive_timeouts", sa.Integer, server_default=sa.text("0")),
        sa.Column("recover_count", sa.Integer, server_default=sa.text("0")),
        sa.Column("priority", sa.Integer, server_default=sa.text("5")),
        sa.Column("payload_json", postgresql.JSONB()),
        sa.Column("result_json", postgresql.JSONB()),
        sa.Column("error", sa.Text),
        sa.Column("lock_until", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    # 活跃态唯一索引：同 (article, task) 只允许一条 queued/running
    op.execute(
        "CREATE UNIQUE INDEX processing_jobs_active_uniq "
        "ON processing_jobs (article_id, task) "
        "WHERE status IN ('queued', 'running')"
    )
    op.execute(
        "CREATE INDEX processing_jobs_queued_idx "
        "ON processing_jobs (priority, created_at) "
        "WHERE status = 'queued'"
    )

    # ── summaries ──────────────────────────────────────────────
    op.create_table(
        "summaries",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("article_id", sa.BigInteger, sa.ForeignKey("articles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lang", sa.String, server_default="zh"),
        sa.Column("model", sa.String),
        sa.Column("content_hash", sa.String),
        sa.Column("summary_text", sa.Text),
        sa.Column("key_points_json", postgresql.JSONB()),
        sa.Column("confidence", sa.Numeric),
        sa.UniqueConstraint("article_id", "lang", "model"),
    )

    # ── topics ─────────────────────────────────────────────────
    op.create_table(
        "topics",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("keywords_json", postgresql.JSONB()),
        sa.Column("enabled", sa.Boolean, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── article_topics ─────────────────────────────────────────
    op.create_table(
        "article_topics",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("article_id", sa.BigInteger, sa.ForeignKey("articles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("topic_id", sa.BigInteger, sa.ForeignKey("topics.id", ondelete="CASCADE"), nullable=False),
        sa.Column("score", sa.Numeric),
        sa.Column("method", sa.String, comment="keyword|llm"),
        sa.UniqueConstraint("article_id", "topic_id"),
    )

    # ── wiki_pages ─────────────────────────────────────────────
    op.create_table(
        "wiki_pages",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String, comment="article|topic|entity|manual"),
        sa.Column("ref_id", sa.BigInteger, comment="多态引用"),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("slug", sa.String, unique=True, nullable=False),
        sa.Column("content_md", sa.Text),
        sa.Column("related_json", postgresql.JSONB()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── fetch_events ───────────────────────────────────────────
    op.create_table(
        "fetch_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("feed_id", sa.BigInteger, sa.ForeignKey("feeds.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String),
        sa.Column("ok", sa.Boolean),
        sa.Column("error", sa.Text),
        sa.Column("item_count", sa.Integer),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("fetch_events")
    op.drop_table("wiki_pages")
    op.drop_table("article_topics")
    op.drop_table("topics")
    op.drop_table("summaries")
    op.execute("DROP INDEX IF EXISTS processing_jobs_queued_idx")
    op.execute("DROP INDEX IF EXISTS processing_jobs_active_uniq")
    op.drop_table("processing_jobs")
    op.execute("DROP INDEX IF EXISTS emb_hnsw_idx")
    op.drop_table("article_embeddings")
    op.drop_table("article_versions")
    op.execute("DROP INDEX IF EXISTS articles_tsv_idx")
    op.drop_table("articles")
    op.drop_table("feeds")
