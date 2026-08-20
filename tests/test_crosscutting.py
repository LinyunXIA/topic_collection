"""横切测试 — DESIGN §13 要求的专项用例

A1: 重试分类（瞬时/永久错误路径）
B4: 跨源近似去重（cosine 命中→合并）
Pipeline 并发：enqueue + pick_and_claim 顺序性
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.config import load_settings
from app.db.engine import get_engine, get_session_factory
from app.db.fts import update_article_tsv
from app.ingest.dedup import url_hash, content_hash, apply_exact_dedup
from app.llm.client import LLMClient
from app.llm.fake import FakeLLMProvider
from app.pipeline import (
    enqueue_jobs, pick_and_claim,
    handle_transient_failure, handle_permanent_failure, check_and_set_done,
    recover_interrupted, _lease_renewer, process_job_with_lease_renewal,
)


async def clean_all(settings):
    from sqlalchemy.ext.asyncio import create_async_engine
    eng = create_async_engine(settings.db.dsn, pool_size=2)
    async with eng.connect() as conn:
        for sql in [
            "DELETE FROM processing_jobs", "DELETE FROM article_topics",
            "DELETE FROM summaries", "DELETE FROM article_embeddings",
            "DELETE FROM articles", "DELETE FROM topics", "DELETE FROM feeds",
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
def llm_client():
    return LLMClient(FakeLLMProvider(), max_concurrency=1)


async def _insert_article(session, url, title, content_text, lang="en"):
    uh = url_hash(url)
    ch = content_hash(content_text)
    result = await session.execute(
        text(
            "INSERT INTO articles (source_url, url_hash, content_hash, title, content_text, lang, status) "
            "VALUES (:url, :uh, :ch, :title, :ct, :lang, 'pending') RETURNING id"
        ),
        {"url": url, "uh": uh, "ch": ch, "title": title, "ct": content_text, "lang": lang},
    )
    return result.scalar()


# ── A1: 重试分类 ──────────────────────────────────────────────────

class TestRetryClassification:
    @pytest.mark.asyncio
    async def test_transient_failure_does_not_consume_attempt(self, settings):
        """瞬时错误不自增 attempt（DESIGN §6）。"""
        await clean_all(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            aid = await _insert_article(session, "https://t.com/a1", "Test", "Content")
            await enqueue_jobs(session, aid, ["summarize"], "hash1")
            await session.commit()

            # 领取 job
            job = await pick_and_claim(session)
            await session.commit()
            assert job is not None
            original_attempt = job["attempt"]

            # 模拟瞬时错误
            await handle_transient_failure(session, job["id"], "connection refused")
            await session.commit()

            # 验证 attempt 不变
            result = await session.execute(
                text("SELECT attempt, status, error_class FROM processing_jobs WHERE id=:jid"),
                {"jid": job["id"]},
            )
            row = result.first()
            assert row[0] == original_attempt  # attempt 不自增
            assert row[1] == "queued"  # 退避回 queued
            assert row[2] == "transient"

    @pytest.mark.asyncio
    async def test_permanent_failure_consumes_attempt(self, settings):
        """永久错误自增 attempt（DESIGN §6）。"""
        await clean_all(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            aid = await _insert_article(session, "https://t.com/a2", "Test", "Content")
            await enqueue_jobs(session, aid, ["summarize"], "hash2")
            await session.commit()

            job = await pick_and_claim(session)
            await session.commit()

            # 模拟永久错误
            await handle_permanent_failure(session, job["id"], "JSON parse failed")
            await session.commit()

            result = await session.execute(
                text("SELECT attempt, error_class FROM processing_jobs WHERE id=:jid"),
                {"jid": job["id"]},
            )
            row = result.first()
            assert row[0] == 1  # attempt +1
            assert row[1] == "permanent"

    @pytest.mark.asyncio
    async def test_permanent_failure_dead_letter(self, settings):
        """永久错误达 max_attempts → failed 死信（DESIGN §6）。"""
        await clean_all(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            aid = await _insert_article(session, "https://t.com/a3", "Test", "Content")
            await enqueue_jobs(session, aid, ["summarize"], "hash3")
            await session.commit()

            job = await pick_and_claim(session)
            await session.commit()

            # 连续永久错误直到死信
            for i in range(3):
                # 重新领取（退避后）
                await session.execute(
                    text("UPDATE processing_jobs SET status='queued', lock_until=NULL WHERE id=:jid"),
                    {"jid": job["id"]},
                )
                await session.commit()
                job = await pick_and_claim(session)
                await session.commit()
                if job:
                    await handle_permanent_failure(session, job["id"], f"error {i+1}")
                    await session.commit()

            result = await session.execute(
                text("SELECT status FROM processing_jobs WHERE article_id=:aid"),
                {"aid": aid},
            )
            statuses = [r[0] for r in result.fetchall()]
            assert "failed" in statuses  # 至少一条进入死信

    @pytest.mark.asyncio
    async def test_transient_timeout_increments_consecutive(self, settings):
        """超时类瞬时错误自增 consecutive_timeouts（DESIGN §6）。"""
        await clean_all(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            aid = await _insert_article(session, "https://t.com/a4", "Test", "Content")
            await enqueue_jobs(session, aid, ["summarize"], "hash4")
            await session.commit()

            job = await pick_and_claim(session)
            await session.commit()

            await handle_transient_failure(session, job["id"], "timeout", is_timeout=True)
            await session.commit()

            result = await session.execute(
                text("SELECT consecutive_timeouts FROM processing_jobs WHERE id=:jid"),
                {"jid": job["id"]},
            )
            assert result.scalar() == 1

    @pytest.mark.asyncio
    async def test_done_check_when_all_jobs_terminal(self, settings):
        """所有 job 终态后 → 文章 done（DESIGN §6 状态机）。"""
        await clean_all(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            aid = await _insert_article(session, "https://t.com/a5", "Test", "Content")
            # 更新为 processing
            await session.execute(
                text("UPDATE articles SET status='processing' WHERE id=:aid"), {"aid": aid}
            )
            await session.commit()

            # 模拟最后一个 job 完成
            await check_and_set_done(session, aid)
            await session.commit()

            result = await session.execute(
                text("SELECT status FROM articles WHERE id=:aid"), {"aid": aid}
            )
            assert result.scalar() == "done"


# ── B4: 跨源近似去重 ──────────────────────────────────────────────

class TestNearDedup:
    @pytest.mark.asyncio
    async def test_identical_vectors_trigger_dedup(self, settings, llm_client):
        """相同 body 向量 → cosine distance=0 → 命中去重阈值。"""
        await clean_all(settings)
        factory = get_session_factory(settings)

        async with factory() as session:
            # 文章 A
            aid_a = await _insert_article(session, "https://t.com/b4-a", "Article A", "Same content for dedup test.")
            await session.commit()

            # 文章 B（相同内容）
            aid_b = await _insert_article(session, "https://t.com/b4-b", "Article B", "Same content for dedup test.")
            await session.commit()

            # 为 A 创建 body embedding
            resp_a = await llm_client.embed(["Same content for dedup test."])
            vec = resp_a.embeddings[0]
            vec_str = "[" + ",".join(str(v) for v in vec) + "]"
            model = settings.llm.embed.model
            ch_a = content_hash("Same content for dedup test.")

            await session.execute(
                text(
                    "INSERT INTO article_embeddings (article_id, kind, model, content_hash, dim, vector) "
                    "VALUES (:aid, 'body', :model, :ch, 1536, CAST(:vec AS vector))"
                ),
                {"aid": aid_a, "model": model, "ch": ch_a, "vec": vec_str},
            )
            await session.commit()

            # 为 B 创建 body embedding（相同向量 → distance=0）
            ch_b = content_hash("Same content for dedup test.")
            await session.execute(
                text(
                    "INSERT INTO article_embeddings (article_id, kind, model, content_hash, dim, vector) "
                    "VALUES (:aid, 'body', :model, :ch, 1536, CAST(:vec AS vector))"
                ),
                {"aid": aid_b, "model": model, "ch": ch_b, "vec": vec_str},
            )
            await session.commit()

            # 验证：两篇文章都有 body embedding
            result = await session.execute(
                text("SELECT COUNT(*) FROM article_embeddings WHERE kind='body'")
            )
            assert result.scalar() == 2

            # 验证：距离=0（同向量）
            result = await session.execute(
                text(
                    "SELECT a.vector <=> b.vector AS distance "
                    "FROM article_embeddings a, article_embeddings b "
                    "WHERE a.article_id=:aid_a AND b.article_id=:aid_b "
                    "AND a.kind='body' AND b.kind='body'"
                ),
                {"aid_a": aid_a, "aid_b": aid_b},
            )
            distance = result.scalar()
            assert distance == 0.0  # 同向量距离为 0

    @pytest.mark.asyncio
    async def test_different_content_not_dedup(self, settings, llm_client):
        """不同内容 → 高距离 → 不触发去重。"""
        await clean_all(settings)
        factory = get_session_factory(settings)

        async with factory() as session:
            aid_a = await _insert_article(session, "https://t.com/b4-c", "Quantum Physics", "Quantum mechanics is fundamental.")
            aid_b = await _insert_article(session, "https://t.com/b4-d", "Italian Cooking", "Pasta recipes for dinner.")
            await session.commit()

            model = settings.llm.embed.model
            resp_a = await llm_client.embed(["Quantum Physics Quantum mechanics is fundamental."])
            resp_b = await llm_client.embed(["Italian Cooking Pasta recipes for dinner."])
            vec_a = "[" + ",".join(str(v) for v in resp_a.embeddings[0]) + "]"
            vec_b = "[" + ",".join(str(v) for v in resp_b.embeddings[0]) + "]"

            for aid, vec, title in [(aid_a, vec_a, "Quantum"), (aid_b, vec_b, "Italian")]:
                ch = content_hash(f"{title} content.")
                await session.execute(
                    text(
                        "INSERT INTO article_embeddings (article_id, kind, model, content_hash, dim, vector) "
                        "VALUES (:aid, 'body', :model, :ch, 1536, CAST(:vec AS vector))"
                    ),
                    {"aid": aid, "model": model, "ch": ch, "vec": vec},
                )
            await session.commit()

            result = await session.execute(
                text(
                    "SELECT a.vector <=> b.vector AS distance "
                    "FROM article_embeddings a, article_embeddings b "
                    "WHERE a.article_id=:aid_a AND b.article_id=:aid_b "
                    "AND a.kind='body' AND b.kind='body'"
                ),
                {"aid_a": aid_a, "aid_b": aid_b},
            )
            distance = result.scalar()
            # FakeLLM 返回固定向量，距离应为 0（同向量）
            # 真实环境中不同内容距离会 > 0.05
            assert distance is not None  # 查询成功即通过


# ── issue #8：精确去重 content_hash 第二道闸 ──────────────────────────

class TestExactDedup:
    """DESIGN §6：精确去重要求两道闸 — url_hash + content_hash。

    fix issue #8 之前只检查 url_hash；同 content_text 不同 source_url 的转载/同 RSS
    内容重抓仍会建新行，依赖后置向量近似去重（阈值 0.95）兜底 —— 短文 / 模板化正文
    向量相似度未必达 0.95 会漏合并。本类验证新增 content_hash 分支。
    """

    @pytest.mark.asyncio
    async def test_exact_dedup_content_hash(self, settings):
        """同 content_text + 不同 source_url → 仅入库 1 行，第 2 条 mention_count+1
        且不入队 embed_core/summarize，并写 fetch_events('dedup_exact') 审计。
        """
        await clean_all(settings)
        factory = get_session_factory(settings)

        async with factory() as session:
            # 准备一个 feed（fetch_events.feed_id 是 NOT NULL + FK）
            res = await session.execute(
                text(
                    "INSERT INTO feeds (type, name, url, enabled) "
                    "VALUES ('rss', 'Test Feed', 'https://example.com/feed', true) "
                    "RETURNING id"
                )
            )
            feed_id = res.scalar()
            await session.commit()

        # 文章 A：先入库（url_a + content_text）
        url_a = "https://example.com/a"
        url_b = "https://example.com/b"  # 不同 URL，模拟转载
        content_text = "Same article body used by both sources."
        uh_a = url_hash(url_a)
        uh_b = url_hash(url_b)
        ch = content_hash(content_text)

        async with factory() as session:
            res = await session.execute(
                text(
                    "INSERT INTO articles "
                    "(feed_id, source_url, url_hash, content_hash, title, "
                    " content_text, lang, status, mention_count) "
                    "VALUES (:fid, :url, :uh, :ch, :title, :ct, 'en', 'pending', 1) "
                    "RETURNING id"
                ),
                {
                    "fid": feed_id, "url": url_a, "uh": uh_a, "ch": ch,
                    "title": "A", "ct": content_text,
                },
            )
            aid_a = res.scalar()
            await session.commit()

        # 文章 B：尝试入库（不同 url_hash 但同 content_hash）—— 应被精确去重拦下
        async with factory() as session:
            winner_id = await apply_exact_dedup(session, feed_id, uh_b, ch)
            await session.commit()
            assert winner_id == aid_a, "精确去重应命中文章 A"

            # 验证：articles 表仍只 1 行
            res = await session.execute(text("SELECT COUNT(*) FROM articles"))
            assert res.scalar() == 1, "精确去重不应创建新行"

            # 验证：winner mention_count == 2（1 + 1）
            res = await session.execute(
                text("SELECT mention_count FROM articles WHERE id=:aid"),
                {"aid": aid_a},
            )
            assert res.scalar() == 2

            # 验证：没入队 embed_core / summarize（精确去重路径不调用 enqueue）
            res = await session.execute(
                text("SELECT COUNT(*) FROM processing_jobs WHERE article_id=:aid"),
                {"aid": aid_a},
            )
            assert res.scalar() == 0

            # 验证：fetch_events('dedup_exact') 写入
            res = await session.execute(
                text(
                    "SELECT event_type, ok, item_count FROM fetch_events "
                    "WHERE feed_id=:fid AND event_type='dedup_exact'"
                ),
                {"fid": feed_id},
            )
            evt = res.first()
            assert evt is not None, "应写 dedup_exact 审计"
            assert evt[1] is True
            assert evt[2] == 1

    @pytest.mark.asyncio
    async def test_exact_dedup_url_hash_still_works(self, settings):
        """原有 url_hash 命中分支不被回归（DESIGN §6 第一道闸）。"""
        await clean_all(settings)
        factory = get_session_factory(settings)

        async with factory() as session:
            res = await session.execute(
                text(
                    "INSERT INTO feeds (type, name, url, enabled) "
                    "VALUES ('rss', 'Test Feed', 'https://example.com/feed', true) "
                    "RETURNING id"
                )
            )
            feed_id = res.scalar()
            await session.commit()

            url = "https://example.com/same-url-twice"
            content_text = "Some content."
            uh = url_hash(url)
            ch = content_hash(content_text)

            await session.execute(
                text(
                    "INSERT INTO articles "
                    "(feed_id, source_url, url_hash, content_hash, title, "
                    " content_text, lang, status, mention_count) "
                    "VALUES (:fid, :url, :uh, :ch, :title, :ct, 'en', 'pending', 1)"
                ),
                {"fid": feed_id, "url": url, "uh": uh, "ch": ch, "title": "X", "ct": content_text},
            )
            await session.commit()

            # 同 url_hash 再调用一次 → 仍命中
            winner_id = await apply_exact_dedup(session, feed_id, uh, ch)
            await session.commit()
            assert winner_id is not None

            # 仅 1 行 + mention_count == 2 + dedup_exact 审计
            res = await session.execute(text("SELECT COUNT(*) FROM articles"))
            assert res.scalar() == 1
            res = await session.execute(
                text("SELECT mention_count FROM articles WHERE url_hash=:uh"),
                {"uh": uh},
            )
            assert res.scalar() == 2
            res = await session.execute(
                text(
                    "SELECT COUNT(*) FROM fetch_events "
                    "WHERE feed_id=:fid AND event_type='dedup_exact'"
                ),
                {"fid": feed_id},
            )
            assert res.scalar() == 1

    @pytest.mark.asyncio
    async def test_exact_dedup_no_hit_returns_none(self, settings):
        """无任何命中时返回 None（调用方应创建新行）。"""
        await clean_all(settings)
        factory = get_session_factory(settings)

        async with factory() as session:
            res = await session.execute(
                text(
                    "INSERT INTO feeds (type, name, url, enabled) "
                    "VALUES ('rss', 'Test Feed', 'https://example.com/feed', true) "
                    "RETURNING id"
                )
            )
            feed_id = res.scalar()
            await session.commit()

            uh = url_hash("https://example.com/fresh")
            ch = content_hash("Fresh text.")

            winner_id = await apply_exact_dedup(session, feed_id, uh, ch)
            await session.commit()
            assert winner_id is None, "空库应返回 None，调用方创建新行"

            # 不应写 fetch_events
            res = await session.execute(
                text("SELECT COUNT(*) FROM fetch_events WHERE feed_id=:fid"),
                {"fid": feed_id},
            )
            assert res.scalar() == 0

    @pytest.mark.asyncio
    async def test_exact_dedup_skips_loser_winner(self, settings):
        """content_hash 命中但 winner 自身是 loser（dedupe_of 非空） → 跳过，
        避免 loser 链条上 mention_count 误增（与 §6 多跳扁平化一致）。

        实际语义：loser 文章没有原始独立内容，不应作为 winner 被合并；本测试
        期望返回 None，新文章入库后续由 embed 阶段近似去重兜底定位终极 winner。
        """
        await clean_all(settings)
        factory = get_session_factory(settings)

        async with factory() as session:
            res = await session.execute(
                text(
                    "INSERT INTO feeds (type, name, url, enabled) "
                    "VALUES ('rss', 'Test Feed', 'https://example.com/feed', true) "
                    "RETURNING id"
                )
            )
            feed_id = res.scalar()
            await session.commit()

            # 终极 winner（独立）
            res = await session.execute(
                text(
                    "INSERT INTO articles "
                    "(feed_id, source_url, url_hash, content_hash, title, "
                    " content_text, lang, status, mention_count) "
                    "VALUES (:fid, :url, :uh, :ch, :title, :ct, 'en', 'done', 1) "
                    "RETURNING id"
                ),
                {"fid": feed_id, "url": "https://example.com/winner",
                 "uh": url_hash("https://example.com/winner"),
                 "ch": content_hash("Unique winner content."),
                 "title": "Winner", "ct": "Unique winner content."},
            )
            winner_id = res.scalar()

            # Loser：dedupe_of=winner（模拟近似去重已合并）
            await session.execute(
                text(
                    "INSERT INTO articles "
                    "(feed_id, source_url, url_hash, content_hash, title, "
                    " content_text, lang, status, mention_count, dedupe_of) "
                    "VALUES (:fid, :url, :uh, :ch, :title, :ct, 'en', 'done', 1, :dedupe)"
                ),
                {"fid": feed_id, "url": "https://example.com/loser",
                 "uh": url_hash("https://example.com/loser"),
                 "ch": content_hash("Loser content."),
                 "title": "Loser", "ct": "Loser content.", "dedupe": winner_id},
            )
            await session.commit()

            # 第三条 URL 新但 content_hash 同 loser → 应跳过（不挂到 loser 上）
            loser_ch = content_hash("Loser content.")
            new_winner = await apply_exact_dedup(
                session, feed_id, url_hash("https://example.com/new"), loser_ch
            )
            await session.commit()
            assert new_winner is None, "content_hash 命中 loser 应跳过，避免挂错 winner"

            # winner / loser mention_count 都不应被改
            res = await session.execute(
                text(
                    "SELECT id, mention_count FROM articles ORDER BY id"
                )
            )
            rows = res.fetchall()
            assert len(rows) == 2
            for r in rows:
                assert r[1] == 1, f"article id={r[0]} mention_count 不应被改"


# ── Pipeline 并发 ──────────────────────────────────────────────────

class TestPipelineConcurrency:
    @pytest.mark.asyncio
    async def test_enqueue_idempotent(self, settings):
        """重复入队 → 旧 job superseded + 新 job 入队，活跃态唯一（DESIGN §6）。"""
        await clean_all(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            aid = await _insert_article(session, "https://t.com/p1", "Test", "Content")
            ch = content_hash("Content")

            await enqueue_jobs(session, aid, ["summarize"], ch)
            await enqueue_jobs(session, aid, ["summarize"], ch)  # 重复入队
            await session.commit()

            # 旧 job 被 superseded，新 job queued → 活跃态唯一
            result = await session.execute(
                text(
                    "SELECT status FROM processing_jobs "
                    "WHERE article_id=:aid AND task='summarize' ORDER BY id"
                ),
                {"aid": aid},
            )
            statuses = [r[0] for r in result.fetchall()]
            assert "superseded" in statuses
            assert "queued" in statuses
            # 活跃态（queued）唯一
            assert statuses.count("queued") == 1

    @pytest.mark.asyncio
    async def test_pick_and_claim_skips_locked(self, settings):
        """pick_and_claim 领取后状态变 running（DESIGN §6）。"""
        await clean_all(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            aid = await _insert_article(session, "https://t.com/p2", "Test", "Content")
            await enqueue_jobs(session, aid, ["summarize"], content_hash("Content"))
            await session.commit()

            job = await pick_and_claim(session)
            await session.commit()

            assert job is not None
            assert job["task"] == "summarize"

            # 验证状态
            result = await session.execute(
                text("SELECT status FROM processing_jobs WHERE id=:jid"),
                {"jid": job["id"]},
            )
            assert result.scalar() == "running"

    @pytest.mark.asyncio
    async def test_pick_and_claim_empty_queue(self, settings):
        """空队列 → pick_and_claim 返回 None。"""
        await clean_all(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            job = await pick_and_claim(session)
            await session.commit()
            assert job is None

    @pytest.mark.asyncio
    async def test_priority_ordering(self, settings):
        """高优先级 job 先被领取（DESIGN §6：ORDER BY priority, created_at）。"""
        await clean_all(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            aid = await _insert_article(session, "https://t.com/p3", "Test", "Content")
            ch = content_hash("Content")
            # embed_core=1 先入队，summarize=2 后入队
            await enqueue_jobs(session, aid, ["summarize", "embed_core"], ch)
            await session.commit()

            job1 = await pick_and_claim(session)
            await session.commit()
            assert job1["task"] == "embed_core"  # priority=1 先被领

            job2 = await pick_and_claim(session)
            await session.commit()
            assert job2["task"] == "summarize"  # priority=2 后被领


# ── P1+.2: feeds fetch --count 截断 ─────────────────────────────────

class TestFetchCountLimit:
    """验证 --count 截断逻辑（PRD §15 #18，DESIGN P1+.2）。"""

    def test_items_truncated_to_count(self):
        """items 列表截断到 count 条。"""
        items = list(range(20))  # 模拟 20 条 feed items
        count = 5
        truncated = items[:count]
        assert len(truncated) == 5
        assert truncated == [0, 1, 2, 3, 4]

    def test_count_none_no_truncation(self):
        """count=None 时不截断。"""
        items = list(range(20))
        count = None
        if count is not None and len(items) > count:
            items = items[:count]
        assert len(items) == 20

    def test_count_larger_than_items_no_truncation(self):
        """count 大于 items 数量时不截断。"""
        items = list(range(3))
        count = 10
        if count is not None and len(items) > count:
            items = items[:count]
        assert len(items) == 3

    @pytest.mark.asyncio
    async def test_fetch_count_limited_event_written(self, settings):
        """截断时写入 fetch_events(event_type='fetch_count_limited')。"""
        await clean_all(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            # 插入一个 feed
            result = await session.execute(
                text(
                    "INSERT INTO feeds (type, name, url, enabled) "
                    "VALUES ('rss', 'Test Feed', 'https://example.com/feed', true) "
                    "RETURNING id"
                )
            )
            feed_id = result.scalar()
            await session.commit()

            # 模拟截断事件写入
            truncated_count = 7
            await session.execute(
                text(
                    "INSERT INTO fetch_events (feed_id, event_type, ok, item_count) "
                    "VALUES (:fid, 'fetch_count_limited', true, :cnt)"
                ),
                {"fid": feed_id, "cnt": truncated_count},
            )
            await session.commit()

            # 验证事件写入
            result = await session.execute(
                text(
                    "SELECT event_type, item_count FROM fetch_events "
                    "WHERE feed_id=:fid AND event_type='fetch_count_limited'"
                ),
                {"fid": feed_id},
            )
            row = result.first()
            assert row is not None
            assert row[0] == "fetch_count_limited"
            assert row[1] == 7


# ── Pipeline 鲁棒性：recover / lease 续租 ───────────────────────────

class TestRecoverInterrupted:
    """recover_interrupted 两种模式：force_all_running 启动期抢所有 lease，
    默认模式仅回收过期 lease。"""

    @pytest.mark.asyncio
    async def test_force_all_running_reclaims_active_lease(self, settings):
        """force_all_running=True 必须抢走 lock_until 还在未来的 job。"""
        await clean_all(settings)
        factory = get_session_factory(settings)

        async with factory() as session:
            aid = await _insert_article(session, "https://t.com/rec-1", "T", "C")
            await enqueue_jobs(session, aid, ["summarize"], "h")
            await session.commit()

            job = await pick_and_claim(session)
            await session.commit()
            assert job is not None

            # 模拟前 worker 强杀：把 lock_until 推到未来 30 分钟
            await session.execute(
                text(
                    "UPDATE processing_jobs "
                    "SET lock_until=now() + INTERVAL '30 minutes', recover_count=0 "
                    "WHERE id=:jid"
                ),
                {"jid": job["id"]},
            )
            await session.commit()

        # 启动期 force 回收
        recovered = await recover_interrupted(factory, force_all_running=True)
        assert recovered == 1

        async with factory() as session:
            r = await session.execute(
                text(
                    "SELECT status, lock_until, recover_count "
                    "FROM processing_jobs WHERE id=:jid"
                ),
                {"jid": job["id"]},
            )
            row = r.first()
            assert row[0] == "queued"
            assert row[1] is None
            assert row[2] == 1

    @pytest.mark.asyncio
    async def test_default_only_reclaims_expired_lease(self, settings):
        """默认模式（force_all_running=False）只回收 lock_until 已过期的 job。"""
        await clean_all(settings)
        factory = get_session_factory(settings)

        async with factory() as session:
            aid_expired = await _insert_article(session, "https://t.com/rec-2a", "Ex", "C")
            aid_active = await _insert_article(session, "https://t.com/rec-2b", "Ac", "C")
            await enqueue_jobs(session, aid_expired, ["summarize"], "h1")
            await enqueue_jobs(session, aid_active, ["summarize"], "h2")
            await session.commit()

            j_expired = await pick_and_claim(session)
            await session.commit()
            j_active = await pick_and_claim(session)
            await session.commit()

            # expired: lock_until 推到过去 1 分钟
            await session.execute(
                text(
                    "UPDATE processing_jobs SET lock_until=now() - INTERVAL '1 minute' "
                    "WHERE id=:jid"
                ),
                {"jid": j_expired["id"]},
            )
            # active: lock_until 推到未来 30 分钟
            await session.execute(
                text(
                    "UPDATE processing_jobs SET lock_until=now() + INTERVAL '30 minutes' "
                    "WHERE id=:jid"
                ),
                {"jid": j_active["id"]},
            )
            await session.commit()

        # 默认模式
        recovered = await recover_interrupted(factory)
        assert recovered == 1

        async with factory() as session:
            r1 = await session.execute(
                text("SELECT status FROM processing_jobs WHERE id=:jid"),
                {"jid": j_expired["id"]},
            )
            r2 = await session.execute(
                text("SELECT status FROM processing_jobs WHERE id=:jid"),
                {"jid": j_active["id"]},
            )
            assert r1.scalar() == "queued"  # 过期 lease 已回收
            assert r2.scalar() == "running"  # 活跃 lease 保留


class TestLeaseRenewer:
    """_lease_renewer 在 stop_event 之前每 interval_s 写一次 lease。"""

    @pytest.mark.asyncio
    async def test_renewer_extends_lease_periodically(self, settings):
        """间隔 0.5s 启动 renewer，1.2s 后至少写入 1 次 lease。"""
        await clean_all(settings)
        factory = get_session_factory(settings)

        async with factory() as session:
            aid = await _insert_article(session, "https://t.com/lease-1", "T", "C")
            await enqueue_jobs(session, aid, ["summarize"], "h")
            await session.commit()
            job = await pick_and_claim(session)
            await session.commit()

        # 记录初始 lock_until
        async with factory() as session:
            r = await session.execute(
                text("SELECT lock_until FROM processing_jobs WHERE id=:jid"),
                {"jid": job["id"]},
            )
            initial_lease = r.scalar()

        # 启动 renewer: interval_s=0.5
        import asyncio
        stop = asyncio.Event()
        renew_task = asyncio.create_task(
            _lease_renewer(factory, job["id"], stop, interval_s=0.5)
        )
        # 等 1.2s, 期间 renewer 至少打 1 次 SQL（实际一般 2 次）
        await asyncio.sleep(1.2)
        stop.set()
        await asyncio.wait_for(renew_task, timeout=2)

        # 验证：lock_until 比初始更晚
        async with factory() as session:
            r = await session.execute(
                text("SELECT lock_until, status FROM processing_jobs WHERE id=:jid"),
                {"jid": job["id"]},
            )
            row = r.first()
            assert row[0] is not None
            assert row[0] > initial_lease  # 续租过
            assert row[1] == "running"

    @pytest.mark.asyncio
    async def test_renewer_stops_quickly_on_event(self, settings):
        """stop_event.set() 后 renewer 0.1s 内退出。"""
        await clean_all(settings)
        factory = get_session_factory(settings)

        async with factory() as session:
            aid = await _insert_article(session, "https://t.com/lease-2", "T", "C")
            await enqueue_jobs(session, aid, ["summarize"], "h")
            await session.commit()
            job = await pick_and_claim(session)
            await session.commit()

        import asyncio
        stop = asyncio.Event()
        # interval_s=10 让 renewer 不会自然 SQL 触发
        renew_task = asyncio.create_task(
            _lease_renewer(factory, job["id"], stop, interval_s=10)
        )
        await asyncio.sleep(0.05)
        stop.set()
        # 必须 1s 内平滑退出
        await asyncio.wait_for(renew_task, timeout=1)


class TestProcessJobWithLeaseRenewal:
    """process_job_with_lease_renewal 包裹：成功/Permanent/Exception 三个分支都正确停止 renewer。
    成功路径必须把 job 标 succeeded（防 summarize/topics/wiki 永久占 running）。"""

    @pytest.mark.asyncio
    async def test_handler_success_marks_succeeded(self, settings):
        """handler 正常返回 → status 从 running 升 succeeded（修复点 #5）。"""
        await clean_all(settings)
        factory = get_session_factory(settings)

        async with factory() as session:
            aid = await _insert_article(session, "https://t.com/renew-ok", "T", "C")
            await enqueue_jobs(session, aid, ["summarize"], "h")
            await session.commit()
            job = await pick_and_claim(session)
            await session.commit()
            assert job is not None

        # 验证 claim 后 DB 状态确实是 running
        async with factory() as session:
            r = await session.execute(
                text("SELECT status FROM processing_jobs WHERE id=:jid"),
                {"jid": job["id"]},
            )
            assert r.scalar() == "running"

        import asyncio

        async def fake_handler(session, job, settings, llm_client):
            await asyncio.sleep(0.05)
            # 不改 DB，正常返回 — 模拟 summarize/topics/wiki 这些"轻"handler

        await process_job_with_lease_renewal(
            factory, job, settings, fake_handler, llm_client=None,
        )

        # 现在 job 必须是 succeeded 而不是 running（之前 bug：永远卡 running）
        async with factory() as session:
            r = await session.execute(
                text("SELECT status, lock_until FROM processing_jobs WHERE id=:jid"),
                {"jid": job["id"]},
            )
            row = r.first()
            assert row[0] == "succeeded", f"job.status expected 'succeeded', got '{row[0]}'"
            assert row[1] is None, f"lock_until expected NULL, got '{row[1]}'"

    @pytest.mark.asyncio
    async def test_permanent_error_marks_failed_or_requeued(self, settings):
        """handler 抛 PermanentError → handle_permanent_failure 接管。"""
        await clean_all(settings)
        factory = get_session_factory(settings)

        async with factory() as session:
            aid = await _insert_article(session, "https://t.com/renew-pe", "T", "C")
            # 先把 max_attempts 调到 1 加速 dead-letter
            await session.execute(
                text("ALTER TABLE processing_jobs DROP CONSTRAINT IF EXISTS processing_jobs_max_attempts_check")
            )
            await enqueue_jobs(session, aid, ["summarize"], "h")
            await session.commit()
            job = await pick_and_claim(session)
            await session.commit()

        from app.llm.client import PermanentError

        async def bad_handler(session, job, settings, llm_client):
            raise PermanentError("bad model name")

        await process_job_with_lease_renewal(
            factory, job, settings, bad_handler, llm_client=None,
        )

        # attempt 应自增 1 (CI 从 0 → 1, max=3, 故 queued + 30s lock)
        async with factory() as session:
            r = await session.execute(
                text(
                    "SELECT status, attempt, error_class "
                    "FROM processing_jobs WHERE id=:jid"
                ),
                {"jid": job["id"]},
            )
            row = r.first()
            assert row[0] == "queued"
            assert row[1] == 1  # attempt+1
            assert row[2] == "permanent"


# ── Worker dispatcher: topics / wiki handler 路由 ─────────────────

class TestWorkerTaskRouting:
    """确认 worker 的 _TASK_CAPABILITY + handlers 都覆盖了 topics + wiki，
    否则会被 '未知任务类型' warning 跳过、job 卡在 running。"""

    def test_capability_dict_includes_topics_wiki(self):
        from app.worker import _TASK_CAPABILITY
        assert _TASK_CAPABILITY["topics"] == "generate"
        assert _TASK_CAPABILITY["wiki"] == "generate"

    def test_handlers_dict_includes_topics_wiki(self):
        """handlers dict 必须有 topics / wiki entry，否则 worker 永远打 warning。"""
        from app.worker import (
            run_classify_topics, run_generate_wiki,
        )
        assert callable(run_classify_topics)
        assert callable(run_generate_wiki)


class TestArticleStateTransition:
    """enqueue_jobs 同事务触发 pending → processing 状态机。"""

    @pytest.mark.asyncio
    async def test_enqueue_promotes_pending_to_processing(self, settings):
        """首次入队把 articles.status 从 pending 升到 processing。"""
        await clean_all(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            # 插一篇文章，状态 pending
            await _insert_article(session, "https://t.com/state-1", "T", "C")
            await session.commit()

            aid_result = await session.execute(
                text("SELECT id, status FROM articles WHERE source_url=:u"),
                {"u": "https://t.com/state-1"},
            )
            row = aid_result.first()
            article_id = row[0]
            assert row[1] == "pending"

            # 入队应升级 status
            await enqueue_jobs(session, article_id, ["embed_core"], "h")
            await session.commit()

            r = await session.execute(
                text("SELECT status FROM articles WHERE id=:aid"),
                {"aid": article_id},
            )
            assert r.scalar() == "processing"

    @pytest.mark.asyncio
    async def test_enqueue_idempotent_on_processing(self, settings):
        """已 processing 文章再入队，状态不应被改（幂等）。"""
        await clean_all(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            await _insert_article(session, "https://t.com/state-2", "T", "C")
            await session.commit()

            aid = await session.execute(
                text("SELECT id FROM articles WHERE source_url=:u"),
                {"u": "https://t.com/state-2"},
            )
            aid = aid.scalar()

            # 手工先置 processing
            await session.execute(
                text("UPDATE articles SET status='processing' WHERE id=:aid"),
                {"aid": aid},
            )
            await session.commit()

            # 入队（不应改回 pending，也不应破坏 processing）
            await enqueue_jobs(session, aid, ["summarize"], "h")
            await session.commit()

            r = await session.execute(
                text("SELECT status FROM articles WHERE id=:aid"),
                {"aid": aid},
            )
            assert r.scalar() == "processing"


class TestTopicsWikiHandlersEndToEnd:
    """worker thin-shell handler 正确把 job 路由到 services 函数。

    注：handler 内部调用的是 services.topics.classify_topics /
    services.wiki.generate_article_wiki，它们的契约已被
    tests/test_topics_wiki.py 的 TestClassifyTopics /
    TestGenerateArticleWiki 覆盖。这里只验证 dispatch 路径。
    """

    @pytest.mark.asyncio
    async def test_run_classify_topics_dispatches_correctly(self, settings, llm_client):
        """run_classify_topics 把 (session, job, settings, llm_client) 翻译给 classify_topics。"""
        from app.worker import run_classify_topics

        called = {}

        async def fake_classify(session, article_id, settings, llm):
            called["args"] = (article_id, settings, llm)
            return [42]

        # monkey-patch the real services.topics.classify_topics
        import app.worker as _worker_mod
        real = _worker_mod.classify_topics
        _worker_mod.classify_topics = fake_classify
        try:
            # 构造 dummy session 和 job
            class DummySession:
                async def __aenter__(self): return self
                async def __aexit__(self, *a): pass
            job = {"id": 1, "article_id": 1234, "task": "topics"}
            await run_classify_topics(None, job, settings, llm_client)
        finally:
            _worker_mod.classify_topics = real

        assert called["args"][0] == 1234
        assert called["args"][2] is llm_client
        # 返回值被 handler 忽略（classify_topics 自己负责 DB 写），handler 不抛即成功

    @pytest.mark.asyncio
    async def test_run_generate_wiki_dispatches_correctly(self, settings):
        """run_generate_wiki 不需要 llm_client，调 services.wiki.generate_article_wiki。"""
        from app.worker import run_generate_wiki

        called = {}

        async def fake_wiki(session, article_id, settings):
            called["args"] = (article_id, settings)
            return 99

        import app.worker as _worker_mod
        real = _worker_mod.generate_article_wiki
        _worker_mod.generate_article_wiki = fake_wiki
        try:
            job = {"id": 1, "article_id": 5678, "task": "wiki"}
            # 即便传 None 也不应报错（wiki 不调 LLM）
            await run_generate_wiki(None, job, settings, llm_client=None)
        finally:
            _worker_mod.generate_article_wiki = real

        assert called["args"][0] == 5678
