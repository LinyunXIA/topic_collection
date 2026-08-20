"""processing_jobs.task CHECK 扩展至 9 值（fix #7）

背景：DESIGN §5.1.5 要求 task IN ('summarize','translate','extract_entities','topics','wiki',
'generate_entity_wiki','generate_topic_wiki','embed_core','embed_summary')，
a001 仅含 String 无 CHECK，a004 未补，导致 Phase 2 入队 extract_entities 等
不会被 DB 校验但也不符合设计。

Revision ID: 005_processing_jobs_task_check
Revises: 004_phase2_tables
Create Date: 2026-08-20
"""

from alembic import op

# revision identifiers
revision = "005_processing_jobs_task_check"
down_revision = "004_phase2_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 兼容幂等：先删旧约束（若存在）再建新约束。旧库无此约束则 DO 块不报错
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'processing_jobs_task_check'
            ) THEN
                ALTER TABLE processing_jobs DROP CONSTRAINT processing_jobs_task_check;
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        ALTER TABLE processing_jobs ADD CONSTRAINT processing_jobs_task_check
        CHECK (task IN (
            'summarize','translate','extract_entities','topics','wiki',
            'generate_entity_wiki','generate_topic_wiki',
            'embed_core','embed_summary'
        ))
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'processing_jobs_task_check'
            ) THEN
                ALTER TABLE processing_jobs DROP CONSTRAINT processing_jobs_task_check;
            END IF;
        END $$;
        """
    )
