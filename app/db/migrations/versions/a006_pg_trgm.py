"""pg_trgm 扩展用于 entities 模糊合并（fix #24）

背景：DESIGN §6.Y merge_aliases 依赖 similarity() / % 操作符，
v0.13 头部宣称已含 pg_trgm，但 a004 未建，导致 Phase 2 首调 PG::UndefinedObject。

Revision ID: 006_pg_trgm
Revises: 005_processing_jobs_task_check
Create Date: 2026-08-20
"""

from alembic import op

# revision identifiers
revision = "006_pg_trgm"
down_revision = "005_processing_jobs_task_check"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    # canonical_name_zh 模糊搜索用 gin_trgm_ops
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS entities_canonical_trgm_idx
        ON entities USING GIN (canonical_name_zh gin_trgm_ops)
        """
    )
    # aliases_json::text 亦可走 trgm，P2 细化时可改为表达式索引
    # 保留 IF NOT EXISTS 幂等
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS entities_aliases_trgm_idx
        ON entities USING GIN ((aliases_json::text) gin_trgm_ops)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS entities_aliases_trgm_idx")
    op.execute("DROP INDEX IF EXISTS entities_canonical_trgm_idx")
    # 不 DROP EXTENSION pg_trgm，避免影响其他对象
