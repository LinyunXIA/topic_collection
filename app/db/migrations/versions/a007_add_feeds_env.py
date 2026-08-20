"""feeds 表加 env 列（dev/prod 隔离，方案 C）

- 加 env TEXT NOT NULL DEFAULT 'dev' CHECK (env IN ('dev','prod'))
- 加唯一约束 (url, env) 供 ON CONFLICT (url, env) 使用
- 存量行 env 默认为 dev

Revision ID: 007_add_feeds_env
Revises: 006_pg_trgm
Create Date: 2026-08-20
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "007_add_feeds_env"
down_revision = "006_pg_trgm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 加 env 列
    op.execute("ALTER TABLE feeds ADD COLUMN IF NOT EXISTS env TEXT NOT NULL DEFAULT 'dev'")
    # 加 CHECK 约束（若已存在则跳过）
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'feeds_env_check'
            ) THEN
                ALTER TABLE feeds ADD CONSTRAINT feeds_env_check CHECK (env IN ('dev','prod'));
            END IF;
        END $$;
        """
    )
    # 回填存量（已 DEFAULT dev，但显式更新一次以确保）
    op.execute("UPDATE feeds SET env='dev' WHERE env IS NULL OR env=''")
    # 加唯一约束 (url, env)
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'feeds_url_env_uniq'
            ) THEN
                ALTER TABLE feeds ADD CONSTRAINT feeds_url_env_uniq UNIQUE (url, env);
            END IF;
        END $$;
        """
    )
    # 索引（可选，用于按 env 筛选）
    op.execute("CREATE INDEX IF NOT EXISTS feeds_env_idx ON feeds (env)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS feeds_env_idx")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'feeds_url_env_uniq'
            ) THEN
                ALTER TABLE feeds DROP CONSTRAINT feeds_url_env_uniq;
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'feeds_env_check'
            ) THEN
                ALTER TABLE feeds DROP CONSTRAINT feeds_env_check;
            END IF;
        END $$;
        """
    )
    op.execute("ALTER TABLE feeds DROP COLUMN IF EXISTS env")
