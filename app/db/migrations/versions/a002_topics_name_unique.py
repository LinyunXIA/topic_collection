"""topics.name 加 UNIQUE 约束（fix #11）

背景：原 schema 漏了 UniqueConstraint，导致 tc topic add 反复执行可建出
多个同名 topics 行，article_topics 各自累积，tc topic list 出现重复项。
此迁移先按 name 去重（保留 id 最小的行，CASCADE 清掉其 article_topics），
再加 UNIQUE 约束。

Revision ID: 002_topics_name_unique
Revises: 001_initial
Create Date: 2026-08-20
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = "002_topics_name_unique"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1. 去重：按 name 分组，保留 id 最小的行，其余删除（CASCADE 清 article_topics）。
    #    迁移前 dev DB 可能已有重名行（issue #11 描述的 bug 现场）。
    bind.execute(
        sa.text(
            """
            DELETE FROM topics
            WHERE id NOT IN (
                SELECT MIN(id) FROM topics GROUP BY name
            )
            """
        )
    )

    # 2. 加 UNIQUE 约束
    op.create_unique_constraint("uq_topics_name", "topics", ["name"])


def downgrade() -> None:
    op.drop_constraint("uq_topics_name", "topics", type_="unique")