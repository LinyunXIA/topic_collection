"""数据库初始化脚本 — DESIGN §5.4

执行两件事（幂等）：
1. CREATE EXTENSION IF NOT EXISTS vector
2. alembic upgrade head（建表全走迁移）

用法: python -m scripts.init_db
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text

from app.config import load_settings
from app.db.engine import dispose_engine, get_engine


async def init_database() -> None:
    """初始化数据库：安装扩展 + 运行迁移"""
    settings = load_settings()
    engine = get_engine(settings)

    try:
        async with engine.begin() as conn:
            # 1. 安装 pgvector 扩展
            print("🔧 安装 vector 扩展...")
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            print("✅ vector 扩展就绪")

            # 2. 校验维度
            result = await conn.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
            row = result.fetchone()
            if row:
                print(f"✅ pgvector 版本: {row[0]}")
            else:
                print("❌ vector 扩展安装失败")
                sys.exit(1)

        # 3. 运行 Alembic 迁移
        print("🔧 运行 Alembic 迁移...")
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"❌ Alembic 迁移失败:\n{result.stderr}")
            sys.exit(1)
        print("✅ Alembic 迁移完成")

        # 4. 校验表已创建
        async with engine.begin() as conn:
            result = await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' ORDER BY table_name"
                )
            )
            tables = [row[0] for row in result.fetchall()]
            print(f"✅ 数据库表 ({len(tables)}): {', '.join(tables)}")

    finally:
        await dispose_engine()


def main() -> None:
    print("🚀 初始化数据库...")
    asyncio.run(init_database())
    print("\n🎉 数据库初始化完成")


if __name__ == "__main__":
    main()
