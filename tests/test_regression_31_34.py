"""#31–#34 修复的回归测试（fix #38）

每个用例在对应修复**之前**必然失败，用于锁住已修好的行为。
需要：Docker Postgres 运行中（docker compose up -d）

- #31 语义检索按相似度而非 article_id 选取结果
- #32 空/过短正文不参与 content_hash 第二闸 + 30 天窗口
- #33 drain_queue 回灌限定到本轮，不越界重复入队
- #34 tc reindex 回填存量 articles.tsv

关于取样量：#31/#33 这类"排序 / 范围"逻辑必须用**多样本**验证——
单篇文章测不出排序，单条 job 测不出范围限定。这正是 #31 那个 P0
连续躲过两轮 review 的原因（见 #38）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.config import load_settings
from app.db.engine import get_session_factory
from app.db.fts import search_articles_fts
from app.ingest.dedup import apply_exact_dedup, content_hash, url_hash
from app.llm.client import LLMClient
from app.llm.fake import FakeLLMProvider
from app.scheduler import drain_queue
from app.services.search import _semantic_search


@pytest.fixture
def settings():
    return load_settings()


@pytest.fixture
def llm_client():
    return LLMClient(FakeLLMProvider(), max_concurrency=1)


_CLEAN_SQL = [
    "DELETE FROM processing_jobs",
    "DELETE FROM article_topics",
    "DELETE FROM summaries",
    "DELETE FROM article_embeddings",
    "DELETE FROM wiki_pages",
    "DELETE FROM fetch_events",
    "DELETE FROM articles",
    "DELETE FROM topics",
    "DELETE FROM feeds",
]


async def clean_all(settings):
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(settings.db.dsn, pool_size=2)
    async with eng.connect() as conn:
        for sql in _CLEAN_SQL:
            await conn.execute(text(sql))
        await conn.commit()
    await eng.dispose()
    from app.db import engine as _eng_mod

    if _eng_mod._engine is not None:
        await _eng_mod._engine.dispose()
        _eng_mod._engine = None
        _eng_mod._session_factory = None


async def _insert_article(session, url, title, content, lang="en", status="done"):
    result = await session.execute(
        text(
            "INSERT INTO articles (source_url, url_hash, content_hash, title, "
            " content_text, lang, status) "
            "VALUES (:url, :uh, :ch, :title, :ct, :lang, :st) RETURNING id"
        ),
        {
            "url": url,
            "uh": url_hash(url),
            "ch": content_hash(content),
            "title": title,
            "ct": content,
            "lang": lang,
            "st": status,
        },
    )
    return result.scalar()


async def _insert_embedding(session, settings, article_id, kind, vector, tag):
    vec_str = "[" + ",".join(str(v) for v in vector) + "]"
    await session.execute(
        text(
            "INSERT INTO article_embeddings "
            "(article_id, kind, model, content_hash, dim, vector) "
            "VALUES (:aid, :kind, :model, :ch, 1536, CAST(:vec AS vector))"
        ),
        {
            "aid": article_id,
            "kind": kind,
            "model": settings.llm.embed.model,
            "ch": tag,
            "vec": vec_str,
        },
    )


# ── #31 语义检索按相似度选取 ──────────────────────────────────────

class TestSemanticSelectsBySimilarity:
    @pytest.mark.asyncio
    async def test_most_similar_wins_even_with_largest_id(self, settings, llm_client):
        """最相似的文章必须被选中，哪怕它的 article_id 最大。

        构造：4 篇文章按 id 升序插入，**最后一篇**（id 最大）的 body 向量
        等于查询向量（distance=0），其余三篇用互不相关的文本（distance≈0.97）。
        取 limit=2。

        - 正确实现（ORDER BY distance）：target 排第一
        - 修复前（DISTINCT ON ... ORDER BY article_id）：返回 id 最小的两篇，
          target 连出现都不会出现

        单篇文章的用例区分不了这两种行为——这是 #31 能躲过两轮 review 的原因。
        """
        await clean_all(settings)
        factory = get_session_factory(settings)
        query = "quantum error correction breakthrough"

        # 查询向量：直接走 embed_query，与实现内部完全一致（含 instruct prefix）
        qvec = (await llm_client.embed_query(query)).embeddings[0]

        async with factory() as session:
            decoy_ids = []
            for i in range(3):
                aid = await _insert_article(
                    session,
                    f"https://t.com/decoy-{i}",
                    f"Unrelated Decoy {i}",
                    f"Completely unrelated body text number {i} about gardening.",
                )
                decoy_ids.append(aid)
                dvec = (
                    await llm_client.embed([f"gardening compost mulch topic {i}"])
                ).embeddings[0]
                await _insert_embedding(session, settings, aid, "body", dvec, f"d{i}")

            # 目标文章最后插入 → article_id 最大
            target_id = await _insert_article(
                session,
                "https://t.com/target",
                "Quantum Error Correction",
                "Surface codes and logical qubits.",
            )
            await _insert_embedding(session, settings, target_id, "body", qvec, "tgt")
            await session.commit()

        assert target_id > max(decoy_ids), "构造前提：target 必须是最大 id"

        async with factory() as session:
            results = await _semantic_search(session, query, settings, llm_client, 2)

        ids = [aid for aid, _ in results]
        assert ids, "语义检索不应返回空"
        assert ids[0] == target_id, (
            f"最相似的文章应排第一，实际返回 {ids}；"
            "若返回的是 id 最小的几篇，说明退回了按 article_id 选取 (#31)"
        )

        scores = [score for _, score in results]
        assert scores == sorted(scores, reverse=True), "结果必须按相似度降序"

    @pytest.mark.asyncio
    async def test_multi_granularity_dedup_keeps_best_distance(
        self, settings, llm_client
    ):
        """同一文章的多个粒度只返回一条，且取距离最小的那个。

        应用层去重依赖"结果已按 distance 升序、首条即最优"这个前提，
        这里用 title(远) + body(精确命中) 两个粒度把它钉住。
        """
        await clean_all(settings)
        factory = get_session_factory(settings)
        query = "superconducting qubit coherence"

        qvec = (await llm_client.embed_query(query)).embeddings[0]
        farvec = (await llm_client.embed(["totally different subject matter"])).embeddings[0]

        async with factory() as session:
            aid = await _insert_article(
                session, "https://t.com/multi", "Multi Granularity", "body text"
            )
            await _insert_embedding(session, settings, aid, "title", farvec, "far")
            await _insert_embedding(session, settings, aid, "body", qvec, "near")
            await session.commit()

        async with factory() as session:
            results = await _semantic_search(session, query, settings, llm_client, 10)

        ids = [a for a, _ in results]
        assert ids.count(aid) == 1, f"多粒度未去重，article {aid} 出现 {ids.count(aid)} 次"

        score = dict(results)[aid]
        # score = 1 - distance；body 精确命中 → distance≈0 → score≈1
        assert score > 0.9, f"应取距离最小的粒度，实际 score={score:.4f}"


# ── #32 空/过短正文不参与 content_hash 第二闸 ─────────────────────

class TestExactDedupEmptyContent:
    async def _make_feed(self, session):
        res = await session.execute(
            text(
                "INSERT INTO feeds (type, name, url, enabled) "
                "VALUES ('rss', 'T', 'https://example.com/feed', true) RETURNING id"
            )
        )
        return res.scalar()

    @pytest.mark.asyncio
    async def test_empty_content_articles_are_not_merged(self, settings):
        """两篇正文为空、标题/URL 不同的文章必须都能入库。

        修复前：content_hash("") 是固定常量，第二闸把它们全判成同一篇，
        只有第一篇能入库，其余静默丢弃（无正文 RSS 源整源只剩一篇）。
        """
        await clean_all(settings)
        factory = get_session_factory(settings)

        async with factory() as session:
            feed_id = await self._make_feed(session)
            await _insert_article(
                session, "https://t.com/empty-a", "Empty A", "", status="pending"
            )
            await session.commit()

            # 第二篇：不同 URL、同样空正文
            winner = await apply_exact_dedup(
                session,
                feed_id,
                url_hash("https://t.com/empty-b"),
                content_hash(""),
                content_text="",
            )
            await session.commit()

        assert winner is None, "空正文不应命中 content_hash 第二闸 (#32)"

    @pytest.mark.asyncio
    async def test_short_content_not_merged(self, settings):
        """归一化后长度低于阈值的正文同样跳过第二闸（模板化短摘要）。"""
        await clean_all(settings)
        factory = get_session_factory(settings)
        short = "点击查看全文"  # 远低于 32 字符阈值

        async with factory() as session:
            feed_id = await self._make_feed(session)
            await _insert_article(
                session, "https://t.com/short-a", "Short A", short, status="pending"
            )
            await session.commit()

            winner = await apply_exact_dedup(
                session,
                feed_id,
                url_hash("https://t.com/short-b"),
                content_hash(short),
                content_text=short,
            )
            await session.commit()

        assert winner is None, "过短正文不应命中第二闸 (#32)"

    @pytest.mark.asyncio
    async def test_normal_content_still_merges(self, settings):
        """守卫不能误伤正常长度的转载去重（#8 的既有行为）。"""
        await clean_all(settings)
        factory = get_session_factory(settings)
        body = "A sufficiently long article body that clearly exceeds the threshold."

        async with factory() as session:
            feed_id = await self._make_feed(session)
            aid = await _insert_article(
                session, "https://t.com/normal-a", "Normal A", body, status="pending"
            )
            await session.commit()

            winner = await apply_exact_dedup(
                session,
                feed_id,
                url_hash("https://t.com/normal-b"),
                content_hash(body),
                content_text=body,
            )
            await session.commit()

        assert winner == aid, "正常长度的同内容转载仍应被第二闸合并 (#8 不能回归)"

    @pytest.mark.asyncio
    async def test_second_gate_respects_30d_window(self, settings):
        """窗口外的同内容文章不再被合并（避免跨月模板化条目误并）。"""
        await clean_all(settings)
        factory = get_session_factory(settings)
        body = "A sufficiently long article body that clearly exceeds the threshold."

        async with factory() as session:
            feed_id = await self._make_feed(session)
            aid = await _insert_article(
                session, "https://t.com/old-a", "Old A", body, status="pending"
            )
            await session.execute(
                text(
                    "UPDATE articles SET fetched_at = now() - INTERVAL '400 days' "
                    "WHERE id=:aid"
                ),
                {"aid": aid},
            )
            await session.commit()

            winner = await apply_exact_dedup(
                session,
                feed_id,
                url_hash("https://t.com/old-b"),
                content_hash(body),
                content_text=body,
            )
            await session.commit()

        assert winner is None, "30 天窗口外不应命中第二闸 (#32)"


# ── #33 drain_queue 回灌限定本轮 ──────────────────────────────────

class TestDrainQueueBackfillScope:
    @pytest.mark.asyncio
    async def test_only_backfilled_articles_get_jobs(self, settings):
        """回灌只给本轮翻转的文章入队，不碰其他 processing 文章。

        构造两篇：
        - A: status='processing' 且无任何 job（模拟成功记录被 24h 清理后的状态）
        - B: status='pending' 且无任何 job（真正需要回灌的）

        修复前 INSERT 扫全表 processing → A 被重复入队，每 24h 循环一次。
        """
        await clean_all(settings)
        factory = get_session_factory(settings)

        async with factory() as session:
            aid_a = await _insert_article(
                session, "https://t.com/dq-a", "Already Processing", "body a",
                status="processing",
            )
            aid_b = await _insert_article(
                session, "https://t.com/dq-b", "Needs Backfill", "body b",
                status="pending",
            )
            await session.commit()

        await drain_queue(settings)

        async with factory() as session:
            result = await session.execute(
                text(
                    "SELECT article_id, task FROM processing_jobs ORDER BY article_id, task"
                )
            )
            rows = [(r[0], r[1]) for r in result.fetchall()]

        a_jobs = [t for aid, t in rows if aid == aid_a]
        b_jobs = sorted(t for aid, t in rows if aid == aid_b)

        assert a_jobs == [], (
            f"已在 processing 的文章不应被回灌入队，实际入队 {a_jobs} (#33)"
        )
        assert b_jobs == ["embed_core", "summarize"], (
            f"pending 文章应被回灌 embed_core+summarize，实际 {b_jobs}"
        )

    @pytest.mark.asyncio
    async def test_backfilled_article_flipped_to_processing(self, settings):
        """回灌的文章状态应翻成 processing。"""
        await clean_all(settings)
        factory = get_session_factory(settings)

        async with factory() as session:
            aid = await _insert_article(
                session, "https://t.com/dq-c", "Pending", "body c", status="pending"
            )
            await session.commit()

        await drain_queue(settings)

        async with factory() as session:
            result = await session.execute(
                text("SELECT status FROM articles WHERE id=:aid"), {"aid": aid}
            )
            assert result.scalar() == "processing"


# ── #34 tc reindex 回填 tsv ───────────────────────────────────────

class TestReindexBackfill:
    @pytest.mark.asyncio
    async def test_reindex_rebuilds_null_tsv(self, settings):
        """tsv IS NULL 的存量文章跑完 reindex 后应能被关键词检索命中。"""
        from app.services.cli import _reindex

        await clean_all(settings)
        factory = get_session_factory(settings)

        async with factory() as session:
            aid = await _insert_article(
                session,
                "https://t.com/reindex-1",
                "Photosynthesis Research",
                "An article about chloroplast and photosynthesis efficiency.",
            )
            await session.commit()

        # 前置：tsv 是 NULL，检索不到
        async with factory() as session:
            result = await session.execute(
                text("SELECT tsv FROM articles WHERE id=:aid"), {"aid": aid}
            )
            assert result.scalar() is None, "构造前提：tsv 应为 NULL"
            assert aid not in await search_articles_fts(session, "chloroplast")

        await _reindex(all_articles=False, batch_size=500)

        async with factory() as session:
            hits = await search_articles_fts(session, "chloroplast")

        assert aid in hits, "reindex 后原文词应可检索 (#34)"

    @pytest.mark.asyncio
    async def test_reindex_all_repairs_summary_only_tsv(self, settings):
        """--all 应修复「tsv 非 NULL 但只含摘要段」的存量。

        这类数据来自 PR #1 之前的整列覆盖写 bug：原文段被摘要抹掉。
        它们的 tsv 不是 NULL，所以默认谓词选不中（见 #39）。
        """
        from app.services.cli import _reindex

        await clean_all(settings)
        factory = get_session_factory(settings)

        async with factory() as session:
            aid = await _insert_article(
                session,
                "https://t.com/reindex-2",
                "Mitochondria Study",
                "An article about mitochondria and cellular respiration.",
            )
            # 模拟旧 bug 留下的状态：tsv 非 NULL，但只含中文摘要段、丢了原文段
            await session.execute(
                text(
                    "UPDATE articles SET tsv = to_tsvector('simple', :only_summary) "
                    "WHERE id=:aid"
                ),
                {"only_summary": "本文 讨论 线粒体 与 细胞 呼吸 能量 代谢", "aid": aid},
            )
            await session.commit()

        async with factory() as session:
            assert aid not in await search_articles_fts(session, "mitochondria"), (
                "构造前提：原文词此时应搜不到"
            )

        await _reindex(all_articles=True, batch_size=500)

        async with factory() as session:
            hits = await search_articles_fts(session, "mitochondria")

        assert aid in hits, "reindex --all 后原文词应可检索 (#34/#39)"
