"""依赖注入 — Phase 2（DESIGN §14 2.1.2）

全部只做 Depends 工厂，不写业务。
"""

from __future__ import annotations

from typing import AsyncGenerator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, load_settings
from app.db.engine import get_session_factory
from app.llm.client import LLMClient


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """DB session 依赖（自动 commit/rollback 由 get_session 上下文管理）"""
    settings = load_settings()
    factory = get_session_factory(settings)
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_settings(request: Request) -> Settings:
    """Settings 依赖（单例，lifespan 已注入 app.state.settings）"""
    return getattr(request.app.state, "settings", load_settings())


def get_llm_client(request: Request) -> LLMClient | None:
    """LLMClient 依赖（lifespan 注入的 generate_llm）"""
    return getattr(request.app.state, "generate_llm", None)


def get_current_user() -> dict:
    """本地单用户占位，不做鉴权（DESIGN §3.1）"""
    return {"id": 1, "name": "local_user"}
