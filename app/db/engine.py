"""数据库引擎 — 异步 SQLAlchemy 2.0 + asyncpg（DESIGN §5.4）"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine(settings: Settings) -> AsyncEngine:
    """获取或创建全局 async engine（单例）。"""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.db.dsn,
            pool_size=settings.db.pool_size,
            echo=False,
        )
    return _engine


def get_session_factory(settings: Settings) -> async_sessionmaker[AsyncSession]:
    """获取或创建 session factory（单例）。"""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(settings),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


@asynccontextmanager
async def get_session(settings: Settings) -> AsyncGenerator[AsyncSession, None]:
    """获取一个 async session，自动提交/回滚。"""
    factory = get_session_factory(settings)
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check_extensions(settings: Settings) -> None:
    """校验 pgvector 扩展已安装 + 向量维度一致（DESIGN §4.2/§5.2）。"""
    engine = get_engine(settings)
    async with engine.connect() as conn:
        # 检查 vector 扩展
        result = await conn.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        )
        if not result.scalar():
            raise RuntimeError(
                "pgvector 扩展未安装，请运行: CREATE EXTENSION IF NOT EXISTS vector"
            )
        logger.info("✅ pgvector 扩展已就绪")

        # 检查向量维度（通过 article_embeddings 表的列类型）
        result = await conn.execute(
            text(
                "SELECT atttypmod FROM pg_attribute "
                "WHERE attrelid = 'article_embeddings'::regclass "
                "AND attname = 'vector'"
            )
        )
        row = result.fetchone()
        if row and row[0] > 0:
            # atttypmod 对 vector 类型 = 维度 + 8
            db_dim = row[0] - 8
            if db_dim != settings.db.vector_dim:
                raise RuntimeError(
                    f"向量维度不匹配：DB={db_dim}, config={settings.db.vector_dim}"
                )
            logger.info(f"✅ 向量维度校验通过: {db_dim}")


async def dispose_engine() -> None:
    """关闭全局 engine。"""
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
        _engine = None
        _session_factory = None
