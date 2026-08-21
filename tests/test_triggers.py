"""#79 触发点接线测试

覆盖：
- complete_summarize 级联入队 extract_entities（DESIGN §14 2.3.3）
- setup_scheduler 注册 daily_report / weekly_report（DESIGN §10.1）
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import load_settings


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


@asynccontextmanager
async def _engine():
    settings = load_settings()
    eng = create_async_engine(settings.db.dsn, pool_size=2)
    Session = async_sessionmaker(eng, expire_on_commit=False)
    try:
        async with eng.begin() as conn:
            for sql in [
                "DELETE FROM article_entities",
                "DELETE FROM entities",
                "DELETE FROM article_topics",
                "DELETE FROM summaries",
                "DELETE FROM processing_jobs",
                "DELETE FROM wiki_pages",
                "DELETE FROM articles",
                "DELETE FROM topics",
                "DELETE FROM reports",
                "DELETE FROM feeds",
            ]:
                await conn.execute(text(sql))
        yield Session
    finally:
        await eng.dispose()
        from app.db import engine as _eng_mod

        if _eng_mod._engine is not None:
            try:
                await _eng_mod._engine.dispose()
            except Exception:
                pass
            _eng_mod._engine = None
            _eng_mod._session_factory = None


class TestCompleteSummarizeCascade:
    """#79：complete_summarize 后 processing_jobs 出现 extract_entities。"""

    @pytest.mark.asyncio
    async def test_extract_entities_enqueued(self):
        async with _engine() as Session:
            from app.services.llm_tasks import complete_summarize

            ch = sha256("v1")
            async with Session() as session:
                aid = (
                    await session.execute(
                        text(
                            "INSERT INTO articles (source_url, url_hash, content_hash, title, content_text, lang, status) "
                            "VALUES ('http://x/1', :uh, :ch, 't', 'c', 'zh', 'pending') RETURNING id"
                        ),
                        {"uh": sha256("http://x/1"), "ch": ch},
                    )
                ).scalar()

                await complete_summarize(
                    session, aid, ch,
                    {"summary_zh": "摘要", "key_points": ["要点"], "confidence": 0.9},
                    load_settings(),
                )
                await session.commit()

                tasks = (
                    await session.execute(
                        text("SELECT task FROM processing_jobs WHERE article_id=:aid"),
                        {"aid": aid},
                    )
                ).scalars().all()
                assert "extract_entities" in tasks, f"级联应入队 extract_entities，实际: {tasks}"


class TestSchedulerRegistersReports:
    """#79：setup_scheduler 注册 daily_report / weekly_report。"""

    def test_report_jobs_registered(self):
        from app.scheduler import setup_scheduler

        settings = load_settings()
        scheduler = setup_scheduler(settings, llm_client=None)
        ids = {j.id for j in scheduler.get_jobs()}
        # 未 start 的 scheduler 直接丢弃即可（GC 无待跑 task）
        assert "daily_report" in ids, f"缺 daily_report，实际: {ids}"
        assert "weekly_report" in ids, f"缺 weekly_report，实际: {ids}"