"""Phase 1 装配缺口回归测试

每个用例对应一处「实现正确但没接上线」的缺陷——在修复前必然失败。
需要：Docker Postgres 运行中（docker compose up -d）

对应审查发现：
1. articles.tsv 从未在入库时建立，且 summarize 后被摘要整列覆盖
2. 近似去重代码从未被执行（complete_embed 调用方不传 job）
3. 近似去重审计写 fetch_events(feed_id=0) 触发外键违例
4. reclassify_recent 的 INTERVAL 天数写在字符串字面量里，SQL 报错
5. reclassify_recent 无条件全量删除 keyword 行，窗口外文章永久丢分类
6. wiki slug 冲突导致同名文章互相覆盖
7. search() 用 wiki_pages.id 去比 article id（id 空间不同）
8. handle_transient_failure 非超时分支不清零 consecutive_timeouts
9. 瞬时退避没有阶梯，TRANSIENT_BACKOFFS 是死常量
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.config import load_settings
from app.db.engine import get_session_factory
from app.db.fts import search_articles_fts, update_article_tsv
from app.ingest.dedup import content_hash, url_hash
from app.llm.client import LLMClient
from app.llm.fake import FakeLLMProvider, _vector_for
from app.pipeline import (
    TRANSIENT_BACKOFFS,
    _transient_backoff,
    enqueue_jobs,
    handle_transient_failure,
)
from app.services.llm_tasks import complete_summarize, run_embed_core
from app.services.search import search
from app.services.topics import create_topic, match_keywords, reclassify_recent
from app.services.wiki import generate_article_wiki


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
    "DELETE FROM article_versions",
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


async def _insert_article(session, url, title, content, lang="en"):
    result = await session.execute(
        text(
            "INSERT INTO articles (source_url, url_hash, content_hash, title, "
            " content_text, lang, status) "
            "VALUES (:url, :uh, :ch, :title, :ct, :lang, 'pending') RETURNING id"
        ),
        {
            "url": url,
            "uh": url_hash(url),
            "ch": content_hash(content),
            "title": title,
            "ct": content,
            "lang": lang,
        },
    )
    return result.scalar()


async def _job_id_for(session, article_id, task):
    result = await session.execute(
        text(
            "SELECT id FROM processing_jobs "
            "WHERE article_id=:aid AND task=:task ORDER BY id DESC LIMIT 1"
        ),
        {"aid": article_id, "task": task},
    )
    row = result.first()
    return row[0] if row else None


# ── 1. tsv 两阶段：摘要不能抹掉原文段 ──────────────────────────────

class TestTsvTwoPhase:
    @pytest.mark.asyncio
    async def test_summarize_keeps_original_text_in_tsv(self, settings):
        """阶段二（摘要）落库后，阶段一（原文）建立的索引必须还在。

        修复前：complete_summarize 传空的 title/content_text，而
        update_article_tsv 是整列覆盖写 → 原文词全部丢失，
        英文原文再也搜不到（PRD 验收 8）。
        """
        await clean_all(settings)
        factory = get_session_factory(settings)
        content = "An article about quantum computing and superconductors."
        async with factory() as session:
            aid = await _insert_article(
                session, "https://t.com/tsv-1", "Quantum Computing", content
            )
            ch = content_hash(content)

            # 阶段一
            await update_article_tsv(
                session, aid, title="Quantum Computing", content_text=content
            )
            await session.commit()

            hits = await search_articles_fts(session, "superconductors")
            assert aid in hits, "阶段一：原文词应可检索"

            # 阶段二
            await complete_summarize(
                session,
                aid,
                ch,
                {
                    "summary_zh": "本文讨论量子计算与超导体的最新进展。",
                    "key_points": ["量子优势", "超导材料"],
                    "confidence": 0.9,
                },
                settings,
            )
            await session.commit()

        async with factory() as session:
            zh_hits = await search_articles_fts(session, "量子计算")
            en_hits = await search_articles_fts(session, "superconductors")

        assert aid in zh_hits, "阶段二：中文摘要词应可检索"
        assert aid in en_hits, "阶段二不得抹掉阶段一：英文原文词仍应可检索"


# ── 2/3. 近似去重真的被执行 ────────────────────────────────────────

class TestNearDedupWired:
    @pytest.mark.asyncio
    async def test_identical_content_merges_via_run_embed_core(
        self, settings, llm_client
    ):
        """两篇内容相同的文章，走 run_embed_core 后第二篇应被合并。

        修复前三重失效：
        - run_embed_core 调 complete_embed 不传 job，去重分支恒不触发
        - 去重查询的向量参数缺 CAST
        - 审计写 fetch_events(feed_id=0) 会外键违例并回滚整个事务
        """
        await clean_all(settings)
        factory = get_session_factory(settings)
        body = "Exactly the same body text used by two different sources."
        ch = content_hash(body)

        async with factory() as session:
            aid_a = await _insert_article(session, "https://a.com/x", "Source A", body)
            await enqueue_jobs(session, aid_a, ["embed_core"], ch)
            await session.commit()
            job_a = await _job_id_for(session, aid_a, "embed_core")
            await run_embed_core(
                session,
                {
                    "id": job_a,
                    "article_id": aid_a,
                    "task": "embed_core",
                    "content_hash": ch,
                },
                settings,
                llm_client,
            )
            await session.commit()

        async with factory() as session:
            aid_b = await _insert_article(session, "https://b.com/y", "Source B", body)
            await enqueue_jobs(session, aid_b, ["embed_core"], ch)
            await session.commit()
            job_b = await _job_id_for(session, aid_b, "embed_core")
            await run_embed_core(
                session,
                {
                    "id": job_b,
                    "article_id": aid_b,
                    "task": "embed_core",
                    "content_hash": ch,
                },
                settings,
                llm_client,
            )
            await session.commit()

        async with factory() as session:
            result = await session.execute(
                text("SELECT dedupe_of, status FROM articles WHERE id=:aid"),
                {"aid": aid_b},
            )
            row = result.mappings().first()

        assert row["dedupe_of"] == aid_a, "内容相同的第二篇应合并到第一篇"
        assert row["status"] == "done"

    @pytest.mark.asyncio
    async def test_different_content_does_not_merge(self, settings, llm_client):
        """语义不同的两篇不应被合并（FakeLLM 现在按文本派生向量）。"""
        await clean_all(settings)
        factory = get_session_factory(settings)

        ids = []
        for url, title, body in [
            (
                "https://a.com/q",
                "Quantum",
                "Quantum mechanics is fundamental to physics.",
            ),
            (
                "https://b.com/p",
                "Pasta",
                "Italian pasta recipes for a weeknight dinner.",
            ),
        ]:
            ch = content_hash(body)
            async with factory() as session:
                aid = await _insert_article(session, url, title, body)
                await enqueue_jobs(session, aid, ["embed_core"], ch)
                await session.commit()
                jid = await _job_id_for(session, aid, "embed_core")
                await run_embed_core(
                    session,
                    {
                        "id": jid,
                        "article_id": aid,
                        "task": "embed_core",
                        "content_hash": ch,
                    },
                    settings,
                    llm_client,
                )
                await session.commit()
            ids.append(aid)

        async with factory() as session:
            result = await session.execute(
                text("SELECT dedupe_of FROM articles WHERE id=:aid"), {"aid": ids[1]}
            )
        assert result.scalar() is None

    def test_fake_llm_vectors_are_text_derived(self):
        """FakeLLM 必须按文本派生向量，否则去重的正/反用例都测不出东西。"""
        same = _vector_for("hello world")
        assert same == _vector_for("hello world")
        other = _vector_for("completely unrelated text")
        dot = sum(a * b for a, b in zip(same, other))
        assert 1.0 - dot > 0.5, "不同文本应近似正交"


# ── 4/5. reclassify_recent ────────────────────────────────────────

class TestReclassifyRecent:
    @pytest.mark.asyncio
    async def test_runs_and_preserves_out_of_window_rows(self, settings):
        """SQL 必须能跑通，且窗口外文章的 keyword 归类不能被删。

        修复前：INTERVAL 的天数写在字符串字面量里不会被绑定，直接 SQL 报错；
        且第一步无条件全量 DELETE，窗口外文章永久丢分类。
        """
        await clean_all(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            tid = await create_topic(session, "量子", ["quantum"], "量子相关")

            recent = await _insert_article(
                session,
                "https://t.com/rc-new",
                "Quantum Now",
                "Quantum computing progress this month.",
            )
            old = await _insert_article(
                session,
                "https://t.com/rc-old",
                "Quantum Then",
                "Quantum annealing results from last year.",
            )
            await session.commit()

            await match_keywords(session, recent)
            await match_keywords(session, old)
            # 把 old 推到窗口外
            await session.execute(
                text(
                    "UPDATE articles SET fetched_at = now() - INTERVAL '400 days' "
                    "WHERE id=:aid"
                ),
                {"aid": old},
            )
            await session.commit()

            requeued = await reclassify_recent(session, tid, settings)
            await session.commit()

        assert isinstance(requeued, int)

        async with factory() as session:
            result = await session.execute(
                text(
                    "SELECT article_id FROM article_topics "
                    "WHERE topic_id=:tid AND method='keyword'"
                ),
                {"tid": tid},
            )
            kept = {r[0] for r in result.fetchall()}

        assert old in kept, "窗口外文章的 keyword 归类不应被删除"
        assert recent in kept, "窗口内文章应被重新匹配上"


# ── 6. wiki slug ──────────────────────────────────────────────────

class TestWikiSlug:
    @pytest.mark.asyncio
    async def test_same_title_articles_get_separate_pages(self, settings):
        """同标题的两篇文章必须各有一条词条，且 ref_id 指向各自的文章。

        修复前：slug 只由标题派生且 UNIQUE，第二篇 upsert 覆盖第一篇正文，
        而 DO UPDATE 不更新 ref_id，内容与引用错位。
        通稿一稿多投时这是常态，不是边缘情况。
        """
        await clean_all(settings)
        factory = get_session_factory(settings)
        title = "阿里千问 3.6 登顶全球模型调用榜首"

        async with factory() as session:
            aid_a = await _insert_article(
                session, "https://qbitai.com/1", title, "量子位的版本正文。", lang="zh"
            )
            aid_b = await _insert_article(
                session, "https://leiphone.com/1", title, "雷锋网的版本正文。", lang="zh"
            )
            await session.commit()

            await generate_article_wiki(session, aid_a, settings)
            await generate_article_wiki(session, aid_b, settings)
            await session.commit()

        async with factory() as session:
            result = await session.execute(
                text(
                    "SELECT ref_id, slug FROM wiki_pages "
                    "WHERE kind='article' ORDER BY ref_id"
                )
            )
            rows = result.mappings().all()

        refs = [r["ref_id"] for r in rows]
        slugs = {r["slug"] for r in rows}
        assert len(rows) == 2, "同标题的两篇文章应各有一条词条"
        assert set(refs) == {aid_a, aid_b}
        assert len(slugs) == 2, "slug 必须唯一到文章级"


# ── 7. search 的 wiki 去重 ────────────────────────────────────────

class TestSearchWikiDedup:
    @pytest.mark.asyncio
    async def test_wiki_of_matched_article_not_listed_twice(self, settings):
        """wiki 词条与已命中的文章去重必须按 ref_id，不能拿 wiki_pages.id 比。"""
        await clean_all(settings)
        factory = get_session_factory(settings)

        async with factory() as session:
            aid = await _insert_article(
                session,
                "https://t.com/dedup-wiki",
                "Quantum Computing",
                "An article about quantum computing.",
            )
            await update_article_tsv(
                session,
                aid,
                title="Quantum Computing",
                content_text="An article about quantum computing.",
            )
            await session.execute(
                text(
                    "INSERT INTO wiki_pages (kind, ref_id, title, slug, content_md) "
                    "VALUES ('article', :ref, 'Quantum Computing', :slug, "
                    "        'quantum computing entry')"
                ),
                {"ref": aid, "slug": f"quantum-computing-{aid}"},
            )
            await session.commit()

            resp = await search(
                session, "quantum", settings, llm_client=None, mode="keyword"
            )

        article_hits = [r for r in resp.results if r.source == "article" and r.id == aid]
        wiki_dupes = [
            r
            for r in resp.results
            if r.source == "wiki" and r.title == "Quantum Computing"
        ]
        assert len(article_hits) == 1
        assert wiki_dupes == [], "同一篇文章的 wiki 词条不应与文章重复列出"


# ── 8/9. 瞬时失败处理 ─────────────────────────────────────────────

class TestTransientFailure:
    def test_backoff_ladder_caps_at_last_step(self):
        """阶梯退避必须真的用上 TRANSIENT_BACKOFFS 并封顶。"""
        assert _transient_backoff(0) == TRANSIENT_BACKOFFS[0]
        assert _transient_backoff(1) == TRANSIENT_BACKOFFS[1]
        assert _transient_backoff(2) == TRANSIENT_BACKOFFS[-1]
        assert _transient_backoff(99) == TRANSIENT_BACKOFFS[-1]
        assert _transient_backoff(-5) == TRANSIENT_BACKOFFS[0]

    @pytest.mark.asyncio
    async def test_non_timeout_resets_consecutive_timeouts(self, settings):
        """非超时的瞬时错误必须把 consecutive_timeouts 清零。

        修复前 SQL 根本没碰这个字段，连续超时会跨越中间的非超时失败继续累加，
        正常任务被误判成病态文章进死信。
        """
        await clean_all(settings)
        factory = get_session_factory(settings)
        body = "Content for transient failure test."
        ch = content_hash(body)

        async with factory() as session:
            aid = await _insert_article(session, "https://t.com/tf", "TF", body)
            await enqueue_jobs(session, aid, ["summarize"], ch)
            await session.commit()
            jid = await _job_id_for(session, aid, "summarize")

            async def _set_running():
                await session.execute(
                    text("UPDATE processing_jobs SET status='running' WHERE id=:jid"),
                    {"jid": jid},
                )

            for _ in range(2):
                await _set_running()
                await handle_transient_failure(
                    session, jid, "timeout", is_timeout=True, health_ok=False
                )
            await session.commit()

            result = await session.execute(
                text("SELECT consecutive_timeouts FROM processing_jobs WHERE id=:jid"),
                {"jid": jid},
            )
            assert result.scalar() == 2

            await _set_running()
            await handle_transient_failure(
                session, jid, "connection reset", is_timeout=False, health_ok=False
            )
            await session.commit()

            result = await session.execute(
                text(
                    "SELECT consecutive_timeouts, status FROM processing_jobs "
                    "WHERE id=:jid"
                ),
                {"jid": jid},
            )
            row = result.mappings().first()

        assert row["consecutive_timeouts"] == 0, "非超时失败后必须清零"
        assert row["status"] == "queued"
