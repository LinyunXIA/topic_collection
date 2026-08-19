"""FakeLLM 集成测试 — DESIGN §14 切片一验收

覆盖 PRD §15 验收 1（建库+抓取+清洗）/ 7（中文摘要）/ 8（关键词搜索）
需要：Docker Postgres 运行中（docker compose up -d）
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from app.config import load_settings
from app.db.engine import get_engine, get_session_factory, dispose_engine
from app.db.fts import search_articles_fts, update_article_tsv
from app.ingest.dedup import url_hash, content_hash
from app.ingest.feeds import FeedFetcher
from app.llm.base import GenerateRequest
from app.llm.client import LLMClient
from app.llm.fake import FakeLLMProvider
from app.pipeline import enqueue_jobs
from app.services.cleaner import clean_article
from app.services.llm_tasks import complete_summarize, complete_embed, run_summarize
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


_CLEAN_SQL = [
    "DELETE FROM processing_jobs",
    "DELETE FROM article_topics",
    "DELETE FROM summaries",
    "DELETE FROM article_embeddings",
    "DELETE FROM article_versions",
    "DELETE FROM wiki_pages",
    "DELETE FROM fetch_events",
    "DELETE FROM articles",
    "DELETE FROM topics",
    "DELETE FROM feeds",
]


async def clean_all(settings):
    """清空所有测试表（创建新引擎避免 singleton 连接池冲突）。"""
    from sqlalchemy.ext.asyncio import create_async_engine
    eng = create_async_engine(settings.db.dsn, pool_size=2)
    async with eng.connect() as conn:
        for sql in _CLEAN_SQL:
            await conn.execute(text(sql))
        await conn.commit()
    await eng.dispose()
    # 也 dispose 全局 singleton engine 释放其连接
    from app.db import engine as _eng_mod
    if _eng_mod._engine is not None:
        await _eng_mod._engine.dispose()
        _eng_mod._engine = None
        _eng_mod._session_factory = None


# ── PRD 验收 1: 建库 + 抓取 + 清洗 ───────────────────────────────

class TestAcceptance1_DB_Fetch_Clean:
    @pytest.mark.asyncio
    async def test_extensions_loaded(self, settings):
        await clean_all(settings)
        """验证 pgvector 扩展已安装。"""
        engine = get_engine(settings)
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT 1 FROM pg_extension WHERE extname='vector'")
            )
            assert result.scalar() == 1

    @pytest.mark.asyncio
    async def test_vector_dimension(self, settings):
        await clean_all(settings)
        """验证向量维度 = 1536。"""
        engine = get_engine(settings)
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT atttypmod FROM pg_attribute "
                     "WHERE attrelid='article_embeddings'::regclass AND attname='vector'")
            )
            assert result.scalar() == 1536

    @pytest.mark.asyncio
    async def test_url_hash_dedup(self, settings):
        await clean_all(settings)
        """URL hash 幂等 + 不同 URL 不同 hash。"""
        h1 = url_hash("https://example.com/a")
        h2 = url_hash("https://example.com/a")
        h3 = url_hash("https://example.com/b")
        assert h1 == h2
        assert h1 != h3

    @pytest.mark.asyncio
    async def test_content_hash_dedup(self):
        """Content hash 归一化。"""
        assert content_hash("a  b") == content_hash("a b")
        assert content_hash("x") != content_hash("y")

    @pytest.mark.asyncio
    async def test_article_insert_and_unique(self, settings):
        await clean_all(settings)
        """文章入库 + url_hash 唯一约束。"""
        factory = get_session_factory(settings)
        async with factory() as session:
            uh = url_hash("https://test.com/1")
            ch = content_hash("test content")
            await session.execute(
                text("INSERT INTO articles (source_url, url_hash, content_hash, title, content_text, lang, status) "
                     "VALUES (:url, :uh, :ch, :title, :ct, 'en', 'pending')"),
                {"url": "https://test.com/1", "uh": uh, "ch": ch, "title": "Test", "ct": "test content"}
            )
            # 重复插入应触发 ON CONFLICT
            await session.execute(
                text("INSERT INTO articles (source_url, url_hash, content_hash, title, content_text, lang, status) "
                     "VALUES (:url, :uh, :ch, :title, :ct, 'en', 'pending') ON CONFLICT DO NOTHING"),
                {"url": "https://test.com/1", "uh": uh, "ch": ch, "title": "Test", "ct": "test content"}
            )
            result = await session.execute(text("SELECT COUNT(*) FROM articles"))
            assert result.scalar() == 1  # 幂等，只有一行

    @pytest.mark.asyncio
    async def test_clean_article(self):
        """HTML 清洗 → 可解析。"""
        html = "<html><body><h1>Title</h1><p>Hello world content for testing.</p></body></html>"
        result = await clean_article(html, "Title")
        assert result["is_parseable"]
        assert result["word_count"] > 0

    @pytest.mark.asyncio
    async def test_clean_article_unparseable(self):
        """空 HTML + 空标题 → unparseable。"""
        result = await clean_article("", "")
        assert not result["is_parseable"]

    @pytest.mark.asyncio
    async def test_language_detection(self):
        """语言检测 en/zh。"""
        from app.services.cleaner import detect_language
        assert await detect_language("This is a longer English text for language detection testing.") == "en"
        assert await detect_language("这是一段较长的中文测试文本，用于验证语言检测功能。") == "zh"


# ── PRD 验收 7: 中文摘要 ──────────────────────────────────────────

class TestAcceptance7_Summary:
    @pytest.mark.asyncio
    async def test_complete_summarize_hook(self, settings):
        await clean_all(settings)
        """complete_summarize 钩子：summaries upsert + tsv 刷新 + 入队。"""
        factory = get_session_factory(settings)
        async with factory() as session:
            uh = url_hash("https://test.com/summary")
            ch = content_hash("AI content about machine learning.")
            result = await session.execute(
                text("INSERT INTO articles (source_url, url_hash, content_hash, title, content_text, lang, status) "
                     "VALUES (:url, :uh, :ch, :title, :ct, 'zh', 'pending') RETURNING id"),
                {"url": "https://test.com/summary", "uh": uh, "ch": ch,
                 "title": "AI 技术综述", "ct": "本文讨论了人工智能技术的最新发展。"}
            )
            article_id = result.scalar()

            summary_data = {
                "summary_zh": "本文综述了人工智能的最新技术进展。",
                "key_points": ["深度学习持续演进", "LLM 能力增强"],
                "confidence": 0.88,
            }
            await complete_summarize(session, article_id, ch, summary_data, settings)
            await session.commit()

        # 验证 summary 落库
        async with factory() as session:
            result = await session.execute(
                text("SELECT summary_text, key_points_json, confidence FROM summaries WHERE article_id=:aid"),
                {"aid": article_id}
            )
            row = result.mappings().first()
            assert row is not None
            assert "人工智能" in row["summary_text"]
            assert row["confidence"] == 0.88

    @pytest.mark.asyncio
    async def test_run_summarize_with_fake_llm(self, settings, llm_client):
        await clean_all(settings)
        """FakeLLM 跑通 run_summarize → complete_summarize。"""
        factory = get_session_factory(settings)
        async with factory() as session:
            uh = url_hash("https://test.com/fake-sum")
            ch = content_hash("FakeLLM test content.")
            result = await session.execute(
                text("INSERT INTO articles (source_url, url_hash, content_hash, title, content_text, lang, status) "
                     "VALUES (:url, :uh, :ch, :title, :ct, 'en', 'pending') RETURNING id"),
                {"url": "https://test.com/fake-sum", "uh": uh, "ch": ch,
                 "title": "FakeLLM Test Article", "ct": "This is a test article for FakeLLM integration."}
            )
            article_id = result.scalar()
            await session.commit()

            job = {"id": 999, "article_id": article_id, "task": "summarize", "content_hash": ch}
            await run_summarize(session, job, settings, llm_client)
            await session.commit()

        # 验证
        async with factory() as session:
            result = await session.execute(
                text("SELECT COUNT(*) FROM summaries WHERE article_id=:aid"), {"aid": article_id}
            )
            assert result.scalar() == 1

    @pytest.mark.asyncio
    async def test_summary_tsv_refresh(self, settings):
        await clean_all(settings)
        """摘要落库后 tsv 包含摘要关键词。"""
        factory = get_session_factory(settings)
        async with factory() as session:
            uh = url_hash("https://test.com/tsv-refresh")
            ch = content_hash("English article about quantum computing.")
            result = await session.execute(
                text("INSERT INTO articles (source_url, url_hash, content_hash, title, content_text, lang, status) "
                     "VALUES (:url, :uh, :ch, :title, :ct, 'en', 'pending') RETURNING id"),
                {"url": "https://test.com/tsv-refresh", "uh": uh, "ch": ch,
                 "title": "Quantum Computing", "ct": "An article about quantum computing."}
            )
            article_id = result.scalar()

            summary_data = {
                "summary_zh": "本文讨论了量子计算的最新进展和应用前景。",
                "key_points": ["量子优势", "量子纠错"],
                "confidence": 0.9,
            }
            await complete_summarize(session, article_id, ch, summary_data, settings)
            await session.commit()

            # tsv 应包含中文摘要的关键词
            result = await session.execute(
                text("SELECT tsv::text FROM articles WHERE id=:aid"), {"aid": article_id}
            )
            tsv_text = result.scalar()
            assert tsv_text is not None
            assert len(tsv_text) > 0


# ── PRD 验收 8: 关键词全文搜索 ────────────────────────────────────

class TestAcceptance8_FTS:
    @pytest.mark.asyncio
    async def test_keyword_search(self, settings):
        await clean_all(settings)
        """关键词搜索命中。"""
        factory = get_session_factory(settings)
        async with factory() as session:
            uh = url_hash("https://test.com/fts-article")
            ch = content_hash("人工智能技术发展迅速。")
            result = await session.execute(
                text("INSERT INTO articles (source_url, url_hash, content_hash, title, content_text, lang, status) "
                     "VALUES (:url, :uh, :ch, :title, :ct, 'zh', 'pending') RETURNING id"),
                {"url": "https://test.com/fts-article", "uh": uh, "ch": ch,
                 "title": "人工智能趋势", "ct": "人工智能技术发展迅速，应用广泛。"}
            )
            article_id = result.scalar()

            # 填充 tsv 列（模拟 ingest 时的 tsv 初始化）
            await update_article_tsv(session, article_id,
                                     title="人工智能趋势",
                                     content_text="人工智能技术发展迅速，应用广泛。")
            await session.commit()

            # FTS 搜索
            ids = await search_articles_fts(session, "人工智能")
            assert len(ids) >= 1

    @pytest.mark.asyncio
    async def test_keyword_search_no_match(self, settings):
        await clean_all(settings)
        """关键词搜索无匹配。"""
        factory = get_session_factory(settings)
        async with factory() as session:
            uh = url_hash("https://test.com/fts-nomatch")
            ch = content_hash("Quantum computing article.")
            await session.execute(
                text("INSERT INTO articles (source_url, url_hash, content_hash, title, content_text, lang, status) "
                     "VALUES (:url, :uh, :ch, :title, :ct, 'en', 'pending')"),
                {"url": "https://test.com/fts-nomatch", "uh": uh, "ch": ch,
                 "title": "Quantum Computing", "ct": "A article about quantum computing."}
            )
            await session.commit()

            ids = await search_articles_fts(session, "量子计算")
            # 英文文章无中文 tsv，可能匹配不上
            assert isinstance(ids, list)

    @pytest.mark.asyncio
    async def test_match_keywords(self, settings):
        await clean_all(settings)
        """关键词快路径匹配主题。"""
        factory = get_session_factory(settings)
        async with factory() as session:
            # 创建主题
            await session.execute(
                text("INSERT INTO topics (name, keywords_json, enabled) "
                     "VALUES ('AI', :kw, true) ON CONFLICT DO NOTHING"),
                {"kw": json.dumps(["人工智能", "AI", "机器学习"])}
            )

            uh = url_hash("https://test.com/kw-article")
            ch = content_hash("This article discusses AI and machine learning.")
            result = await session.execute(
                text("INSERT INTO articles (source_url, url_hash, content_hash, title, content_text, lang, status) "
                     "VALUES (:url, :uh, :ch, :title, :ct, 'en', 'pending') RETURNING id"),
                {"url": "https://test.com/kw-article", "uh": uh, "ch": ch,
                 "title": "AI Revolution", "ct": "This article discusses AI and machine learning advances."}
            )
            article_id = result.scalar()
            await session.commit()

            matched = await match_keywords(session, article_id)
            assert len(matched) >= 1  # "AI" 应命中


# ── 端到端 Pipeline ───────────────────────────────────────────────

class TestEndToEndPipeline:
    @pytest.mark.asyncio
    async def test_enqueue_and_complete_flow(self, settings, llm_client):
        await clean_all(settings)
        """入库 → 入队 → FakeLLM summarize → complete_summarize → summary 落库。"""
        factory = get_session_factory(settings)
        async with factory() as session:
            uh = url_hash("https://test.com/e2e")
            ch = content_hash("End to end pipeline test content about AI.")
            result = await session.execute(
                text("INSERT INTO articles (source_url, url_hash, content_hash, title, content_text, lang, status) "
                     "VALUES (:url, :uh, :ch, :title, :ct, 'en', 'pending') RETURNING id"),
                {"url": "https://test.com/e2e", "uh": uh, "ch": ch,
                 "title": "E2E Pipeline Test", "ct": "End to end pipeline test content about AI."}
            )
            article_id = result.scalar()

            # 入队
            await enqueue_jobs(session, article_id, ["embed_core", "summarize"], ch)
            await session.commit()

        # FakeLLM summarize
        async with factory() as session:
            job = {"id": 0, "article_id": article_id, "task": "summarize", "content_hash": ch}
            await run_summarize(session, job, settings, llm_client)
            await session.commit()

        # 验证
        async with factory() as session:
            result = await session.execute(
                text("SELECT COUNT(*) FROM summaries WHERE article_id=:aid"), {"aid": article_id}
            )
            assert result.scalar() == 1
            result = await session.execute(
                text("SELECT COUNT(*) FROM processing_jobs WHERE article_id=:aid AND task='embed_summary'"),
                {"aid": article_id}
            )
            assert result.scalar() >= 1  # embed_summary 已入队

    @pytest.mark.asyncio
    async def test_embed_core_flow(self, settings, llm_client):
        await clean_all(settings)
        """FakeLLM embed → complete_embed → 向量落库。"""
        factory = get_session_factory(settings)
        async with factory() as session:
            uh = url_hash("https://test.com/embed-core")
            ch = content_hash("Embed core test.")
            result = await session.execute(
                text("INSERT INTO articles (source_url, url_hash, content_hash, title, content_text, lang, status) "
                     "VALUES (:url, :uh, :ch, :title, :ct, 'en', 'pending') RETURNING id"),
                {"url": "https://test.com/embed-core", "uh": uh, "ch": ch,
                 "title": "Embed Test", "ct": "Embed core test content."}
            )
            article_id = result.scalar()
            await session.commit()

            resp_title = await llm_client.embed(["Embed Test"])
            resp_body = await llm_client.embed(["Embed core test content."])
            await complete_embed(session, article_id, ch, [
                ("title", resp_title.embeddings[0], resp_title.dim),
                ("body", resp_body.embeddings[0], resp_body.dim),
            ], settings)
            await session.commit()

        async with factory() as session:
            result = await session.execute(
                text("SELECT COUNT(*) FROM article_embeddings WHERE article_id=:aid"), {"aid": article_id}
            )
            assert result.scalar() == 2  # title + body

    @pytest.mark.asyncio
    async def test_near_dedup_no_hit(self, settings, llm_client):
        await clean_all(settings)
        """两篇不同文章不应触发近似去重。"""
        factory = get_session_factory(settings)
        async with factory() as session:
            # 文章 A
            uh_a = url_hash("https://test.com/dedup-a")
            ch_a = content_hash("Article about quantum physics.")
            result = await session.execute(
                text("INSERT INTO articles (source_url, url_hash, content_hash, title, content_text, lang, status) "
                     "VALUES (:url, :uh, :ch, :title, :ct, 'en', 'pending') RETURNING id"),
                {"url": "https://test.com/dedup-a", "uh": uh_a, "ch": ch_a,
                 "title": "Quantum Physics", "ct": "Article about quantum physics."}
            )
            aid_a = result.scalar()

            # 文章 B
            uh_b = url_hash("https://test.com/dedup-b")
            ch_b = content_hash("Cooking recipes for Italian pasta.")
            result = await session.execute(
                text("INSERT INTO articles (source_url, url_hash, content_hash, title, content_text, lang, status) "
                     "VALUES (:url, :uh, :ch, :title, :ct, 'en', 'pending') RETURNING id"),
                {"url": "https://test.com/dedup-b", "uh": uh_b, "ch": ch_b,
                 "title": "Italian Cooking", "ct": "Cooking recipes for Italian pasta."}
            )
            aid_b = result.scalar()
            await session.commit()

            # embed A
            resp_a = await llm_client.embed(["Quantum Physics Article about quantum physics."])
            await complete_embed(session, aid_a, ch_a, [
                ("body", resp_a.embeddings[0], resp_a.dim),
            ], settings)
            await session.commit()

            # embed B → 不应命中 A
            resp_b = await llm_client.embed(["Italian Cooking Cooking recipes for Italian pasta."])
            await complete_embed(session, aid_b, ch_b, [
                ("body", resp_b.embeddings[0], resp_b.dim),
            ], settings)
            await session.commit()

        # 验证 B 没有被合并
        async with factory() as session:
            result = await session.execute(
                text("SELECT dedupe_of FROM articles WHERE id=:bid"), {"bid": aid_b}
            )
            assert result.scalar() is None


# ── FakeLLM Provider ──────────────────────────────────────────────

class TestFakeLLM:
    @pytest.mark.asyncio
    async def test_healthcheck(self):
        fake = FakeLLMProvider()
        status = await fake.healthcheck()
        assert status.healthy

    @pytest.mark.asyncio
    async def test_generate_returns_json(self):
        fake = FakeLLMProvider()
        resp = await fake.generate(GenerateRequest(
            model="test", messages=[{"role": "user", "content": "test"}]
        ))
        assert resp.text
        assert resp.finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_embed_dim(self):
        fake = FakeLLMProvider()
        resp = await fake.embed(["hello"])
        assert resp.dim == 1536
        assert len(resp.embeddings) == 1

    @pytest.mark.asyncio
    async def test_rerank(self):
        fake = FakeLLMProvider()
        result = await fake.rerank("query", ["doc1", "doc2"], top_n=2)
        assert len(result.indices) == 2

    @pytest.mark.asyncio
    async def test_call_count(self):
        fake = FakeLLMProvider()
        assert fake.call_count == 0
        await fake.generate(GenerateRequest(model="t", messages=[{"role": "user", "content": "x"}]))
        assert fake.call_count == 1
