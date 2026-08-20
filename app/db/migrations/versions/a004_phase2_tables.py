"""Phase 2 表：translations / entities / article_entities / relations / reports（fix #5）

背景：DESIGN §5.1/§5.1.5 承诺 Phase 1 DDL 已预创建这些表，Phase 2 切片 2.3/2.5
在此基础上增量迁移。实际 Phase 1 DDL 只建了 10 张表，缺失这 5 张——Phase 2 任务
extract_entities / generate_daily_report 等入队即 `relation does not exist`。
Phase 2 字段/索引微调按 §5.1.5 一次性到位（canonical_name_zh + GIN / source_articles_json /
status 字段），避免之后再写第二次迁移。

Revision ID: 004_phase2_tables
Revises: 003_wiki_tsv
Create Date: 2026-08-20
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers
revision = "004_phase2_tables"
down_revision = "003_wiki_tsv"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── translations ─────────────────────────────────────────────
    # 同一文章同一 (src, tgt, model) 只保留最新（DESIGN §5.1）
    op.create_table(
        "translations",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("article_id", sa.BigInteger,
                  sa.ForeignKey("articles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("src_lang", sa.String),
        sa.Column("tgt_lang", sa.String, server_default="zh"),
        sa.Column("model", sa.String),
        sa.Column("content_hash", sa.String),
        sa.Column("translated_title", sa.Text),
        sa.Column("translated_content", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("article_id", "src_lang", "tgt_lang", "model",
                             name="uq_translations_article_lang_model"),
    )

    # ── entities（Phase 2 字段已就位，避免二次迁移）─────────────────
    # canonical_name_zh + (entity_type, canonical_name_zh) UNIQUE + GIN(aliases_json)
    # ——DESIGN §5.1.5 增量步骤 1 一次性做完
    op.create_table(
        "entities",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("canonical_name_zh", sa.Text, nullable=False),
        sa.Column("aliases_json", postgresql.JSONB()),
        sa.Column("entity_type", sa.String),
        sa.Column("description", sa.Text),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("mention_count", sa.Integer, server_default=sa.text("0")),
        sa.Column("confidence", sa.Numeric),
        sa.UniqueConstraint("entity_type", "canonical_name_zh",
                             name="entities_uniq_per_type_zh"),
    )
    # GIN(aliases_json) 支持 aliases 模糊匹配（§5.1.5）
    op.execute("CREATE INDEX entities_aliases_gin_idx ON entities USING GIN (aliases_json)")

    # ── article_entities（DESIGN §6.X）────────────────────────────
    # 当前文章涉及的实体 = 抽取产物的落地表
    # CASCADE 双删：删 article / entity 自动清关联
    op.create_table(
        "article_entities",
        sa.Column("article_id", sa.BigInteger,
                  sa.ForeignKey("articles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_id", sa.BigInteger,
                  sa.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("confidence", sa.Numeric),
        sa.Column("surface", sa.Text),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("article_id", "entity_id"),
    )
    op.execute("CREATE INDEX article_entities_entity_idx ON article_entities (entity_id)")

    # ── relations（DESIGN §5.1 + §5.1.5）──────────────────────────
    # source_articles_json JSONB 维护所有来源文章，避免多行 UNIQUE 冲突丢失
    # source_article_id 改为可选（来源改用 JSONB 数组）
    op.create_table(
        "relations",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("subject_id", sa.BigInteger,
                  sa.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("predicate", sa.String),
        sa.Column("object_id", sa.BigInteger,
                  sa.ForeignKey("entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_article_id", sa.BigInteger,
                  sa.ForeignKey("articles.id", ondelete="SET NULL")),
        sa.Column("source_articles_json", postgresql.JSONB(),
                  server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("confidence", sa.Numeric),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("subject_id", "predicate", "object_id",
                             name="relations_uniq_spo"),
    )
    op.execute("CREATE INDEX relations_subject_idx ON relations (subject_id)")
    op.execute("CREATE INDEX relations_object_idx ON relations (object_id)")

    # ── reports（DESIGN §5.1 + §5.1.5）────────────────────────────
    # status / started_at / completed_at / error：失败可重试
    # (report_type, period_start, period_end) UNIQUE：同日重复生成走覆盖
    op.create_table(
        "reports",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("report_type", sa.String),
        sa.Column("period_start", sa.Date),
        sa.Column("period_end", sa.Date),
        sa.Column("content_md", sa.Text),
        sa.Column("content_html", sa.Text),
        sa.Column("stats_json", postgresql.JSONB()),
        sa.Column("status", sa.String, nullable=False, server_default=sa.text("'pending'")),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "report_type IN ('daily','weekly')", name="reports_type_check",
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','succeeded','failed')",
            name="reports_status_check",
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX reports_period_uniq "
        "ON reports (report_type, period_start, period_end)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS reports_period_uniq")
    op.drop_table("reports")
    op.execute("DROP INDEX IF EXISTS relations_object_idx")
    op.execute("DROP INDEX IF EXISTS relations_subject_idx")
    op.drop_table("relations")
    op.execute("DROP INDEX IF EXISTS article_entities_entity_idx")
    op.drop_table("article_entities")
    op.execute("DROP INDEX IF EXISTS entities_aliases_gin_idx")
    op.drop_table("entities")
    op.drop_table("translations")