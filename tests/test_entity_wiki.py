"""#80 entity/topic wiki 接线测试

覆盖：
- enqueue_entity_wiki 把 new_ids 写入 payload_json（#46 合并逻辑真正生效）
- generate_entity_wiki / generate_topic_wiki 生成 kind='entity'/'topic' 词条
- handler 不再忽略 id 调文章词条
"""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import load_settings


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


@asynccontextmanager
async def _engine():
    """每测试独立的本地 engine，测试结束在同 loop 内 dispose（避免跨 loop 存留报错）。"""
    settings = load_settings()
    eng = create_async_engine(settings.db.dsn, pool_size=2)
    Session = async_sessionmaker(eng, expire_on_commit=False)
    try:
        # 清理脏数据
        async with eng.begin() as conn:
            for sql in [
                "DELETE FROM relations",
                "DELETE FROM article_entities",
                "DELETE FROM entities",
                "DELETE FROM article_topics",
                "DELETE FROM summaries",
                "DELETE FROM processing_jobs",
                "DELETE FROM wiki_pages",
                "DELETE FROM articles",
                "DELETE FROM topics",
                "DELETE FROM feeds",
            ]:
                await conn.execute(text(sql))
        yield Session
    finally:
        await eng.dispose()
        # 清理 module-level engine 引用，避免跨 loop 复用
        from app.db import engine as _eng_mod

        if _eng_mod._engine is not None:
            try:
                await _eng_mod._engine.dispose()
            except Exception:
                pass
            _eng_mod._engine = None
            _eng_mod._session_factory = None


class TestEntityTopicWiki:
    @pytest.mark.asyncio
    async def test_enqueue_entity_wiki_writes_payload(self):
        """enqueue_entity_wiki 把 entity_ids 写入 processing_jobs.payload_json。"""
        async with _engine() as Session:
            from app.pipeline import enqueue_entity_wiki

            ch = sha256("v1")
            async with Session() as session:
                # processing_jobs.article_id 有 FK，需先有一篇文章
                aid = (
                    await session.execute(
                        text(
                            "INSERT INTO articles (source_url, url_hash, content_hash, title, content_text, lang, status) "
                            "VALUES ('http://x/1', :uh, :ch, 't', 'c', 'zh', 'done') RETURNING id"
                        ),
                        {"uh": sha256("http://x/1"), "ch": ch},
                    )
                ).scalar()
                await enqueue_entity_wiki(session, article_id=aid, entity_ids=[10, 11], content_hash=ch)
                await session.commit()

                row = await session.execute(
                    text("SELECT payload_json FROM processing_jobs WHERE task='generate_entity_wiki'")
                )
                payload = row.scalar()
                assert payload is not None, "payload_json 不应为 NULL（#46 合并逻辑生效）"
                if isinstance(payload, str):
                    payload = json.loads(payload)
                assert payload["entity_ids"] == [10, 11]

    @pytest.mark.asyncio
    async def test_generate_entity_wiki_creates_entity_page(self):
        """generate_entity_wiki 生成 kind='entity' 词条（原 handler 误生成文章词条）。"""
        async with _engine() as Session:
            from app.services.wiki import generate_entity_wiki

            async with Session() as session:
                eid = (
                    await session.execute(
                        text(
                            "INSERT INTO entities (canonical_name_zh, entity_type, description) "
                            "VALUES ('OpenAI', 'company', 'AI 公司') RETURNING id"
                        )
                    )
                ).scalar()
                wiki_id = await generate_entity_wiki(session, eid, None)
                await session.commit()

                assert wiki_id is not None
                row = (
                    await session.execute(
                        text("SELECT kind, ref_id, title, slug FROM wiki_pages WHERE id=:id"),
                        {"id": wiki_id},
                    )
                ).mappings().first()
                assert row["kind"] == "entity"
                assert row["ref_id"] == eid
                assert row["title"] == "OpenAI"
                assert row["slug"].startswith("entity-")

    @pytest.mark.asyncio
    async def test_generate_topic_wiki_creates_topic_page(self):
        """generate_topic_wiki 生成 kind='topic' 词条。"""
        async with _engine() as Session:
            from app.services.wiki import generate_topic_wiki

            async with Session() as session:
                tid = (
                    await session.execute(
                        text(
                            "INSERT INTO topics (name, description, keywords_json, enabled) "
                            "VALUES ('AI', '人工智能', '[\"AI\",\"LLM\"]'::jsonb, true) RETURNING id"
                        )
                    )
                ).scalar()
                wiki_id = await generate_topic_wiki(session, tid, None)
                await session.commit()

                assert wiki_id is not None
                row = (
                    await session.execute(
                        text("SELECT kind, ref_id, slug FROM wiki_pages WHERE id=:id"),
                        {"id": wiki_id},
                    )
                ).mappings().first()
                assert row["kind"] == "topic"
                assert row["ref_id"] == tid
                assert row["slug"].startswith("topic-")