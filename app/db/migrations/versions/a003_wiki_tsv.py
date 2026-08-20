"""wiki_pages 加 tsv 列 + GIN 索引 + jieba backfill（fix #6）

背景：DESIGN §5.1.5/§7.1/PRD §15 验收 5 要求 Wiki 全文搜索。
原 schema 漏了 tsv 列，search_wiki / _wiki_search 退化 ILIKE 子串匹配，
中文多词召回为 0。

本迁移与 articles.tsv 保持同一模式：
- Alembic 手工加 tsvector + GIN（pgvector 走 raw SQL 同款套路）
- Python jieba backfill：jieba 是 sync，env.py 切到 psycopg2 同步驱动，
  upgrade() 里直接调用，无 trigger 依赖（与 articles 一致）
- 新/更新行由 app/services/wiki.py:generate_article_wiki 调
  app/db/fts.py:update_wiki_tsv 写入 jieba'd tsv

Revision ID: 003_wiki_tsv
Revises: 002_topics_name_unique
Create Date: 2026-08-20
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = "003_wiki_tsv"
down_revision = "002_topics_name_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1. 加 tsvector 列 + GIN 索引（Alembic 不原生支持 tsvector 走 raw SQL）
    op.execute("ALTER TABLE wiki_pages ADD COLUMN tsv tsvector")
    op.execute("CREATE INDEX wiki_tsv_idx ON wiki_pages USING GIN (tsv)")

    # 2. Python jieba backfill 已存在行（不依赖 trigger，应用层 update_wiki_tsv
    #    与 articles.tsv 同一模式——jieba 预切词 + to_tsvector('simple', ...)）。
    #    注意：Alembic env.py 用 psycopg2 同步驱动；jieba 是 sync 函数，直接调用即可。
    import jieba

    rows = bind.execute(
        sa.text("SELECT id, title, content_md FROM wiki_pages")
    ).fetchall()
    for row in rows:
        title = row[1] or ""
        content_md = row[2] or ""
        text_content = f"{title} {content_md}"
        joined = " ".join(
            t.strip() for t in jieba.cut_for_search(text_content) if t.strip()
        )
        bind.execute(
            sa.text(
                "UPDATE wiki_pages SET tsv = to_tsvector('simple', :joined) "
                "WHERE id = :id"
            ),
            {"joined": joined, "id": row[0]},
        )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS wiki_tsv_idx")
    op.execute("ALTER TABLE wiki_pages DROP COLUMN IF EXISTS tsv")