"""切片二测试 — 混合检索（DESIGN §7 / PRD §15 验收 9）

覆盖：RRF 融合 / 语义搜索 / 关键词搜索 / 降级 / CLI --mode
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.config import load_settings
from app.db.engine import get_engine, get_session_factory
from app.db.fts import update_article_tsv
from app.ingest.dedup import url_hash, content_hash
from app.llm.client import LLMClient
from app.llm.fake import FakeLLMProvider
from app.services.search import (
    search,
    _rrf_merge,
    _keyword_search,
    _semantic_search,
    RRF_K,
)


async def clean_all(settings):
    from sqlalchemy.ext.asyncio import create_async_engine
    eng = create_async_engine(settings.db.dsn, pool_size=2)
    async with eng.connect() as conn:
        for sql in [
            "DELETE FROM processing_jobs", "DELETE FROM article_topics",
            "DELETE FROM summaries", "DELETE FROM article_embeddings",
            "DELETE FROM articles", "DELETE FROM topics",
        ]:
            await conn.execute(text(sql))
        await conn.commit()
    await eng.dispose()
    from app.db import engine as _eng_mod
    if _eng_mod._engine is not None:
        await _eng_mod._engine.dispose()
        _eng_mod._engine = None
        _eng_mod._session_factory = None


@pytest.fixture
def settings():
    return load_settings()


@pytest.fixture
def fake_llm():
    return FakeLLMProvider()


@pytest.fixture
def llm_client(fake_llm):
    return LLMClient(fake_llm, max_concurrency=1)


async def _insert_article(session, url, title, content_text, lang="en"):
    """辅助：插入文章 + 填充 tsv。"""
    uh = url_hash(url)
    ch = content_hash(content_text)
    result = await session.execute(
        text(
            "INSERT INTO articles (source_url, url_hash, content_hash, title, content_text, lang, status) "
            "VALUES (:url, :uh, :ch, :title, :ct, :lang, 'done') RETURNING id"
        ),
        {"url": url, "uh": uh, "ch": ch, "title": title, "ct": content_text, "lang": lang},
    )
    article_id = result.scalar()
    await update_article_tsv(session, article_id, title=title, content_text=content_text)
    return article_id


# ── RRF 融合单元测试 ──────────────────────────────────────────────

class TestRRFMerge:
    def test_rrf_merge_basic(self):
        """两条通道重叠 → RRF 分数叠加。"""
        kw = [(1, 0.9), (2, 0.8), (3, 0.7)]
        sem = [(2, 0.95), (3, 0.85), (4, 0.75)]
        merged = _rrf_merge(kw, sem, limit=10)
        ids = [aid for aid, _ in merged]
        # id=2 和 id=3 同时出现在两通道，应排名靠前
        assert ids.index(2) < ids.index(1)  # 2 有双通道加分
        assert ids.index(3) < ids.index(1)  # 3 也有双通道加分

    def test_rrf_merge_empty(self):
        merged = _rrf_merge([], [], limit=10)
        assert merged == []

    def test_rrf_merge_single_channel(self):
        """只有一条通道也能正常排序。"""
        kw = [(10, 0.9), (20, 0.8)]
        merged = _rrf_merge(kw, [], limit=10)
        assert len(merged) == 2
        assert merged[0][0] == 10  # 分数高的排前面

    def test_rrf_merge_limit(self):
        """limit 截断。"""
        kw = [(i, 0.5) for i in range(100)]
        merged = _rrf_merge(kw, [], limit=5)
        assert len(merged) == 5


# ── 关键词搜索集成测试 ────────────────────────────────────────────

class TestKeywordSearch:
    @pytest.mark.asyncio
    async def test_keyword_hit(self, settings):
        await clean_all(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            aid = await _insert_article(
                session,
                "https://test.com/kw1",
                "量子计算技术进展",
                "量子计算是下一代计算技术的核心方向。",
                lang="zh",
            )
            await session.commit()

        async with factory() as session:
            results = await _keyword_search(session, "量子计算", 10)
            ids = [r[0] for r in results]
            assert aid in ids

    @pytest.mark.asyncio
    async def test_keyword_no_match(self, settings):
        await clean_all(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            await _insert_article(
                session,
                "https://test.com/kw2",
                "Cooking Recipes",
                "Pasta and Italian food.",
                lang="en",
            )
            await session.commit()

        async with factory() as session:
            results = await _keyword_search(session, "量子计算", 10)
            assert len(results) == 0


# ── 语义搜索集成测试 ──────────────────────────────────────────────

class TestSemanticSearch:
    @pytest.mark.asyncio
    async def test_semantic_search_with_embeddings(self, settings, llm_client):
        await clean_all(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            aid = await _insert_article(
                session,
                "https://test.com/sem1",
                "Artificial Intelligence Advances",
                "Machine learning and neural networks are transforming technology.",
                lang="en",
            )
            # 创建 embedding
            resp = await llm_client.embed(["Artificial Intelligence Advances Machine learning"])
            vec = resp.embeddings[0]
            vec_str = "[" + ",".join(str(v) for v in vec) + "]"
            await session.execute(
                text(
                    "INSERT INTO article_embeddings (article_id, kind, model, content_hash, dim, vector) "
                    "VALUES (:aid, 'title', :model, 'test', 1536, CAST(:vec AS vector))"
                ),
                {"aid": aid, "model": settings.llm.embed.model, "vec": vec_str},
            )
            await session.commit()

        async with factory() as session:
            results = await _semantic_search(session, "AI technology", settings, llm_client, 10)
            ids = [r[0] for r in results]
            assert aid in ids

    @pytest.mark.asyncio
    async def test_semantic_empty_when_no_embeddings(self, settings, llm_client):
        await clean_all(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            await _insert_article(
                session,
                "https://test.com/sem2",
                "No Embeddings",
                "This article has no vector embeddings.",
            )
            await session.commit()

        async with factory() as session:
            results = await _semantic_search(session, "test", settings, llm_client, 10)
            assert len(results) == 0

    @pytest.mark.asyncio
    async def test_hnsw_index_used(self, settings, llm_client):
        """验证多粒度 embedding 去重取最优距离（fix #10 → #31 改为应用层去重）。

        实现已从 DISTINCT ON 换成 ORDER BY distance + 应用层按 article_id 去重
        （#31：DISTINCT ON 强制按分组键排序，导致按 article_id 而非相似度选取）。
        本用例只覆盖"同一文章多粒度只返回一条"；排序正确性见
        tests/test_regression_31_34.py::TestSemanticSelectsBySimilarity。
        """
        await clean_all(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            aid = await _insert_article(
                session,
                "https://test.com/hnsw1",
                "HNSW Index Test",
                "Testing HNSW index usage for semantic search.",
                lang="en",
            )
            resp = await llm_client.embed(["HNSW index test"])
            vec = resp.embeddings[0]
            vec_str = "[" + ",".join(str(v) for v in vec) + "]"
            # 同一文章插入 title + body 两个粒度的 embedding
            for kind in ("title", "body"):
                await session.execute(
                    text(
                        "INSERT INTO article_embeddings (article_id, kind, model, content_hash, dim, vector) "
                        "VALUES (:aid, :kind, :model, :ch, 1536, CAST(:vec AS vector))"
                    ),
                    {"aid": aid, "kind": kind, "model": settings.llm.embed.model, "ch": f"hnsw_{kind}", "vec": vec_str},
                )
            await session.commit()

        async with factory() as session:
            results = await _semantic_search(session, "HNSW index test", settings, llm_client, 10)
            # 应只返回一条（article_id 去重），而非两条
            ids = [r[0] for r in results]
            assert ids.count(aid) == 1, f"多粒度未去重：article {aid} 出现 {ids.count(aid)} 次"
            # 距离应在合理范围内（cosine distance ∈ [0, 2]）
            dist = 1.0 - results[0][1]  # results 存的是 score=1-distance
            assert 0.0 <= dist <= 2.0


# ── 混合搜索集成测试 ──────────────────────────────────────────────

class TestHybridSearch:
    @pytest.mark.asyncio
    async def test_hybrid_mode(self, settings, llm_client):
        await clean_all(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            aid = await _insert_article(
                session,
                "https://test.com/hyb1",
                "深度学习技术",
                "深度学习是人工智能的核心技术，广泛应用于自然语言处理。",
                lang="zh",
            )
            # 创建 embedding
            resp = await llm_client.embed(["深度学习技术 深度学习是人工智能的核心技术"])
            vec = resp.embeddings[0]
            vec_str = "[" + ",".join(str(v) for v in vec) + "]"
            await session.execute(
                text(
                    "INSERT INTO article_embeddings (article_id, kind, model, content_hash, dim, vector) "
                    "VALUES (:aid, 'title', :model, 'test', 1536, CAST(:vec AS vector))"
                ),
                {"aid": aid, "model": settings.llm.embed.model, "vec": vec_str},
            )
            await session.commit()

        async with factory() as session:
            resp = await search(session, "深度学习", settings, llm_client, mode="hybrid", limit=10)
            assert resp.mode == "hybrid"
            ids = [r.id for r in resp.results]
            assert aid in ids

    @pytest.mark.asyncio
    async def test_keyword_only_mode(self, settings):
        await clean_all(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            aid = await _insert_article(
                session,
                "https://test.com/kw-only",
                "Python 编程教程",
                "Python 是一种广泛使用的编程语言。",
                lang="zh",
            )
            await session.commit()

        async with factory() as session:
            resp = await search(session, "Python", settings, None, mode="keyword", limit=10)
            assert resp.mode == "keyword"
            ids = [r.id for r in resp.results]
            assert aid in ids

    @pytest.mark.asyncio
    async def test_empty_query(self, settings):
        await clean_all(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            resp = await search(session, "", settings, None, mode="hybrid")
            assert resp.results == []
            assert resp.total == 0

    @pytest.mark.asyncio
    async def test_fallback_to_keyword_when_no_llm(self, settings):
        """无 LLM 时 hybrid 降级为 keyword。"""
        await clean_all(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            await _insert_article(
                session,
                "https://test.com/fallback",
                "Fallback Test",
                "Testing keyword fallback when LLM is unavailable.",
            )
            await session.commit()

        async with factory() as session:
            resp = await search(session, "Fallback", settings, None, mode="hybrid")
            # 无 LLM → 降级 keyword
            assert resp.mode == "keyword"
            assert len(resp.results) >= 1
