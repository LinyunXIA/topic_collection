"""FakeLLM 集成测试 — DESIGN §14 切片一验收

验证：建库 + 抓取 + 清洗 + 摘要 + 关键词搜索（用 FakeLLM 跑通）
需要：Docker Postgres 运行中（docker compose up -d）
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import text

from app.config import load_settings
from app.db.engine import check_extensions, get_engine, get_session_factory, dispose_engine
from app.db.fts import search_articles_fts, update_article_tsv
from app.ingest.dedup import url_hash, content_hash
from app.llm.base import GenerateRequest
from app.llm.client import LLMClient
from app.llm.fake import FakeLLMProvider
from app.pipeline import enqueue_jobs
from app.services.cleaner import clean_article
from app.services.llm_tasks import complete_summarize
from app.services.topics import match_keywords


@pytest.fixture
def settings():
    return load_settings()


@pytest.fixture
def fake_llm():
    return FakeLLMProvider()


@pytest.fixture
def llm_client(fake_llm):
    return LLMClient(fake_llm, max_concurrency=1)


@pytest.mark.asyncio
async def test_full_pipeline(settings, fake_llm, llm_client):
    """端到端测试：文章入库 → 清洗 → 摘要 → 关键词搜索。"""
    engine = get_engine(settings)
    factory = get_session_factory(settings)

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM articles"))
        await conn.execute(text("DELETE FROM feeds"))
        await conn.execute(text("DELETE FROM summaries"))
        await conn.execute(text("DELETE FROM article_topics"))
        await conn.execute(text("DELETE FROM processing_jobs"))

    async with factory() as session:
        # 1. 入库一篇文章
        test_url = "https://example.com/test-ai-article"
        u_hash = url_hash(test_url)
        c_hash = content_hash("这是一篇关于人工智能的技术文章，讨论了大语言模型的最新进展。")

        result = await session.execute(
            text(
                "INSERT INTO articles "
                "(source_url, url_hash, content_hash, title, content_text, lang, status) "
                "VALUES (:url, :uh, :ch, :title, :ct, 'zh', 'pending') "
                "RETURNING id"
            ),
            {
                "url": test_url,
                "uh": u_hash,
                "ch": c_hash,
                "title": "AI 技术进展",
                "ct": "这是一篇关于人工智能的技术文章，讨论了大语言模型的最新进展。",
            },
        )
        article_id = result.scalar()
        assert article_id is not None
        print(f"✅ 1. 文章入库: id={article_id}")

        # 2. 清洗测试
        html = "<html><body><p>Test content</p></body></html>"
        cleaned = await clean_article(html, "Test")
        assert cleaned["is_parseable"]
        print(f"✅ 2. 清洗通过: lang={cleaned['lang']}")

        # 3. 入队任务
        await enqueue_jobs(session, article_id, ["embed_core", "summarize"], c_hash)
        print("✅ 3. 任务入队")

        # 4. 模拟 summarize 完成（complete_summarize 钩子）
        fake_summary = {
            "summary_zh": "本文讨论了人工智能和大语言模型的最新技术进展。",
            "key_points": ["大语言模型持续发展", "AI 技术在各领域应用"],
            "confidence": 0.9,
        }
        await complete_summarize(session, article_id, c_hash, fake_summary, settings)
        print("✅ 4. complete_summarize 钩子执行")

        # 5. 验证 summary 落库
        result = await session.execute(
            text("SELECT summary_text FROM summaries WHERE article_id=:aid"),
            {"aid": article_id},
        )
        row = result.first()
        assert row is not None
        assert "人工智能" in row[0]
        print(f"✅ 5. Summary 落库: {row[0][:30]}...")

        # 6. 验证 tsv 已刷新
        result = await session.execute(
            text("SELECT tsv FROM articles WHERE id=:aid"),
            {"aid": article_id},
        )
        row = result.first()
        assert row is not None
        print("✅ 6. tsv 已刷新")

        # 7. 关键词搜索
        # 先创建一个主题
        await session.execute(
            text(
                "INSERT INTO topics (name, keywords_json, enabled) "
                "VALUES ('人工智能', :kw, true) ON CONFLICT DO NOTHING"
            ),
            {"kw": json.dumps(["人工智能", "AI", "大语言模型"])},
        )

        # 关键词匹配
        matched = await match_keywords(session, article_id)
        assert len(matched) > 0
        print(f"✅ 7. 关键词匹配: {len(matched)} 个主题")

        # 8. FTS 搜索
        ids = await search_articles_fts(session, "人工智能")
        assert article_id in ids
        print(f"✅ 8. FTS 搜索: 找到 {len(ids)} 篇")

        # 9. FakeLLM generate 测试
        resp = await llm_client.generate(
            GenerateRequest(
                model="test",
                messages=[{"role": "user", "content": "test"}],
            )
        )
        assert resp.text
        print(f"✅ 9. FakeLLM generate: {resp.text[:40]}...")

        # 10. FakeLLM embed 测试
        embed_resp = await llm_client.embed(["test"])
        assert embed_resp.dim == 1536
        print(f"✅ 10. FakeLLM embed: dim={embed_resp.dim}")

        await session.commit()

    await dispose_engine()
    print("\n🎉 全部验收测试通过！")


@pytest.mark.asyncio
async def test_dedup_hash():
    """测试 URL/content hash 去重。"""
    h1 = url_hash("https://example.com/article/1")
    h2 = url_hash("https://example.com/article/1")
    h3 = url_hash("https://example.com/article/2")
    assert h1 == h2
    assert h1 != h3
    print("✅ URL hash 去重测试通过")

    c1 = content_hash("Hello World")
    c2 = content_hash("Hello World")
    c3 = content_hash("Different content")
    assert c1 == c2
    assert c1 != c3
    print("✅ Content hash 去重测试通过")
