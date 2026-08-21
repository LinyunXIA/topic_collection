"""实体链路测试 — #81 content_hash 断言 + #80 enqueue_entity_wiki 接线

覆盖：
- complete_extract 对非法 content_hash（如正文片段）抛 PermanentError（fix #81）
- 合法 sha256 content_hash 正常通过
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import text

from app.config import load_settings
from app.db.engine import get_session_factory
from app.llm.client import PermanentError
from app.services.entities import complete_extract


async def reset_engine(settings):
    from app.db import engine as _eng_mod

    if _eng_mod._engine is not None:
        await _eng_mod._engine.dispose()
        _eng_mod._engine = None
        _eng_mod._session_factory = None


async def clean(settings):
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(settings.db.dsn, pool_size=2)
    async with eng.connect() as conn:
        for sql in [
            "DELETE FROM relations",
            "DELETE FROM article_entities",
            "DELETE FROM entities",
            "DELETE FROM article_topics",
            "DELETE FROM summaries",
            "DELETE FROM processing_jobs",
            "DELETE FROM wiki_pages",
        ]:
            await conn.execute(text(sql))
        await conn.commit()
    await eng.dispose()
    await reset_engine(settings)


class TestCompleteExtractContentHash:
    """#81：content_hash 参数必须合法（sha256 64 位十六进制）。"""

    async def _session(self):
        settings = load_settings()
        await clean(settings)
        factory = get_session_factory(settings)
        return settings, factory

    @pytest.mark.asyncio
    async def test_invalid_content_hash_raises(self):
        """传正文片段（长文本）当 content_hash → PermanentError（回归 #81）。"""
        settings, factory = await self._session()
        async with factory() as session:
            with pytest.raises(PermanentError, match="content_hash 非法"):
                await complete_extract(
                    session,
                    article_id=1,
                    content_hash="这段是正文内容的前 100 个字符，而不是 sha256 哈希 12345678901234567890",
                    parsed={"entities": [], "relations": []},
                    content_text="这段是正文内容",
                    settings=settings,
                )

    @pytest.mark.asyncio
    async def test_valid_content_hash_passes(self):
        """合法 sha256 不抛错（空 entities 走 upsert → done 检查，不写脏数据）。"""
        valid = hashlib.sha256(b"content-v1").hexdigest()
        settings, factory = await self._session()
        async with factory() as session:
            # 不应抛异常
            await complete_extract(
                session,
                article_id=1,
                content_hash=valid,
                parsed={"entities": [], "relations": []},
                content_text="hello",
                settings=settings,
            )
            await session.rollback()  # 不持久化脏数据