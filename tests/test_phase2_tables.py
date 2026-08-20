"""Phase 2 表存在 + 关键约束测试（fix #5）

DESIGN §5.1/§5.1.5/§6.X 承诺 Phase 2 表 translations/entities/article_entities/
relations/reports 应在 Phase 1 DDL 预创建但实际缺失。本测试断言：
- 5 表均在 DB
- 关键 UNIQUE / GIN / CHECK 约束生效
- article_entities 双 FK CASCADE 工作
- relations 双向索引、reports 状态机可用
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text

from app.config import load_settings
from app.db.engine import get_session_factory


TABLES = ("translations", "entities", "article_entities", "relations", "reports")


async def clean_phase2(settings):
    """按 FK 反向顺序清 Phase 2 表 + 关联表 + 重置 module-level engine（防跨 loop 复用）。"""
    from sqlalchemy.ext.asyncio import create_async_engine
    eng = create_async_engine(settings.db.dsn, pool_size=2)
    async with eng.connect() as conn:
        for sql in [
            "DELETE FROM relations",
            "DELETE FROM article_entities",
            "DELETE FROM entities",
            "DELETE FROM translations",
            "DELETE FROM reports",
            "DELETE FROM processing_jobs",
            "DELETE FROM article_topics",
            "DELETE FROM summaries",
            "DELETE FROM article_embeddings",
            "DELETE FROM wiki_pages",
            "DELETE FROM articles",
            "DELETE FROM topics",
            "DELETE FROM feeds",
        ]:
            await conn.execute(text(sql))
        await conn.commit()
    await eng.dispose()
    from app.db import engine as _eng_mod
    if _eng_mod._engine is not None:
        await _eng_mod._engine.dispose()
        _eng_mod._engine = None
        _eng_mod._session_factory = None


async def reset_engine(settings):
    """仅重置 module-level engine（不动数据）——纯查询测试用。"""
    from app.db import engine as _eng_mod
    if _eng_mod._engine is not None:
        await _eng_mod._engine.dispose()
        _eng_mod._engine = None
        _eng_mod._session_factory = None


class TestPhase2TablesExist:
    """5 张 Phase 2 表在 DB 中真实存在（issue #5 验收）。"""

    @pytest.mark.asyncio
    async def test_all_phase2_tables_present(self):
        settings = load_settings()
        await reset_engine(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            result = await session.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name = ANY(:names)"
                ),
                {"names": list(TABLES)},
            )
            present = {row[0] for row in result.fetchall()}

        missing = set(TABLES) - present
        assert not missing, f"缺失 Phase 2 表: {missing}"

    @pytest.mark.asyncio
    async def test_phase2_column_counts_match_design(self):
        """每张表列数对齐 DESIGN §5.1 + §5.1.5。"""
        expected = {
            "translations": 9,
            "entities": 9,
            "article_entities": 5,
            "relations": 9,
            "reports": 12,
        }
        settings = load_settings()
        await reset_engine(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            for tbl, n_expected in expected.items():
                result = await session.execute(
                    text(
                        "SELECT COUNT(*) FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name=:t"
                    ),
                    {"t": tbl},
                )
                n_actual = result.scalar()
                assert n_actual == n_expected, (
                    f"{tbl} 列数期望 {n_expected}，实际 {n_actual}"
                )


class TestEntitiesConstraints:
    """entities 表 Phase 2 字段已就位（fix #5 一次性到位，避免二次迁移）。"""

    @pytest.mark.asyncio
    async def test_unique_per_type_zh(self):
        settings = load_settings()
        await clean_phase2(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            await session.execute(
                text(
                    "INSERT INTO entities (canonical_name_zh, entity_type) "
                    "VALUES ('OpenAI', 'org')"
                )
            )
            await session.commit()
            with pytest.raises(Exception):  # IntegrityError
                await session.execute(
                    text(
                        "INSERT INTO entities (canonical_name_zh, entity_type) "
                        "VALUES ('OpenAI', 'org')"
                    )
                )
                await session.commit()

    @pytest.mark.asyncio
    async def test_aliases_gin_index_exists(self):
        """GIN(aliases_json) 索引存在——§5.1.5 必备。"""
        settings = load_settings()
        await reset_engine(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            result = await session.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname='public' AND tablename='entities' "
                    "AND indexname='entities_aliases_gin_idx'"
                )
            )
            row = result.first()
            assert row is not None, "entities_aliases_gin_idx 不存在"
            assert "using gin" in row[0].lower()


class TestRelationsConstraints:
    """relations 表 UNIQUE(subject, predicate, object) + 双向索引 + JSONB 来源列表。"""

    @pytest.mark.asyncio
    async def test_unique_spo(self):
        settings = load_settings()
        await clean_phase2(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            # 先创建两个 entity
            await session.execute(text(
                "INSERT INTO entities (canonical_name_zh, entity_type) "
                "VALUES ('A', 'org'), ('B', 'org')"
            ))
            await session.commit()
            aid = (await session.execute(
                text("SELECT id FROM entities WHERE canonical_name_zh='A'")
            )).scalar()
            bid = (await session.execute(
                text("SELECT id FROM entities WHERE canonical_name_zh='B'")
            )).scalar()

            # 同 (s, p, o) 二次插入 → 应失败
            await session.execute(text(
                "INSERT INTO relations (subject_id, predicate, object_id) "
                "VALUES (:a, 'invested_in', :b)"
            ), {"a": aid, "b": bid})
            await session.commit()
            with pytest.raises(Exception):
                await session.execute(text(
                    "INSERT INTO relations (subject_id, predicate, object_id) "
                    "VALUES (:a, 'invested_in', :b)"
                ), {"a": aid, "b": bid})
                await session.commit()

    @pytest.mark.asyncio
    async def test_bidirectional_indexes(self):
        """relations_subject_idx / relations_object_idx 双向索引存在。"""
        settings = load_settings()
        await reset_engine(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            for idx in ("relations_subject_idx", "relations_object_idx"):
                row = (await session.execute(
                    text(
                        "SELECT 1 FROM pg_indexes "
                        "WHERE schemaname='public' AND indexname=:i"
                    ),
                    {"i": idx},
                )).first()
                assert row is not None, f"{idx} 不存在"


class TestArticleEntitiesCascade:
    """article_entities 双 FK CASCADE（删 article / entity 自动清关联）。"""

    @pytest.mark.asyncio
    async def test_delete_article_cascades(self):
        settings = load_settings()
        await clean_phase2(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            await session.execute(text(
                "INSERT INTO articles "
                "(source_url, url_hash, content_hash, title, content_text, status) "
                "VALUES ('https://x.com/a1', 'uh1', 'ch1', 'T', 'C', 'done')"
            ))
            await session.execute(text(
                "INSERT INTO entities (canonical_name_zh, entity_type) "
                "VALUES ('E1', 'org')"
            ))
            await session.commit()
            aid = (await session.execute(
                text("SELECT id FROM articles WHERE url_hash='uh1'")
            )).scalar()
            eid = (await session.execute(
                text("SELECT id FROM entities WHERE canonical_name_zh='E1'")
            )).scalar()
            await session.execute(text(
                "INSERT INTO article_entities (article_id, entity_id) VALUES (:a, :e)"
            ), {"a": aid, "e": eid})
            await session.commit()

            # 删 article → article_entities 自动清
            await session.execute(text("DELETE FROM articles WHERE id=:a"), {"a": aid})
            await session.commit()
            n = (await session.execute(text(
                "SELECT COUNT(*) FROM article_entities WHERE article_id=:a"
            ), {"a": aid})).scalar()
            assert n == 0, "删 article 后 article_entities 应被 CASCADE 清空"


class TestReportsConstraints:
    """reports 表 status 状态机 + period 唯一。"""

    @pytest.mark.asyncio
    async def test_status_default_pending(self):
        """status 默认 'pending'（DESIGN §5.1.5 修正）。"""
        settings = load_settings()
        await clean_phase2(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            await session.execute(text(
                "INSERT INTO reports (report_type, period_start, period_end) "
                "VALUES ('daily', :ps, :pe)"
            ), {"ps": date(2026, 8, 20), "pe": date(2026, 8, 20)})
            await session.commit()
            row = (await session.execute(text(
                "SELECT status FROM reports WHERE report_type='daily'"
            ))).mappings().first()
            assert row["status"] == "pending"

    @pytest.mark.asyncio
    async def test_period_unique(self):
        """(report_type, period_start, period_end) 唯一——同日重复生成走覆盖。"""
        settings = load_settings()
        await clean_phase2(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            ps = date(2026, 8, 20)
            pe = date(2026, 8, 20)
            await session.execute(text(
                "INSERT INTO reports (report_type, period_start, period_end) "
                "VALUES ('daily', :ps, :pe)"
            ), {"ps": ps, "pe": pe})
            await session.commit()
            with pytest.raises(Exception):
                await session.execute(text(
                    "INSERT INTO reports (report_type, period_start, period_end) "
                    "VALUES ('daily', :ps, :pe)"
                ), {"ps": ps, "pe": pe})
                await session.commit()


class TestTranslationsConstraints:
    """translations 表 UNIQUE(article_id, src_lang, tgt_lang, model)。"""

    @pytest.mark.asyncio
    async def test_unique_article_lang_model(self):
        settings = load_settings()
        await clean_phase2(settings)
        factory = get_session_factory(settings)
        async with factory() as session:
            await session.execute(text(
                "INSERT INTO articles "
                "(source_url, url_hash, content_hash, title, content_text, status) "
                "VALUES ('https://x.com/tr', 'uh-tr', 'ch-tr', 'T', 'C', 'done')"
            ))
            await session.commit()
            aid = (await session.execute(
                text("SELECT id FROM articles WHERE url_hash='uh-tr'")
            )).scalar()

            await session.execute(text(
                "INSERT INTO translations "
                "(article_id, src_lang, tgt_lang, model, translated_title) "
                "VALUES (:a, 'en', 'zh', 'fake', '标题1')"
            ), {"a": aid})
            await session.commit()
            with pytest.raises(Exception):
                await session.execute(text(
                    "INSERT INTO translations "
                    "(article_id, src_lang, tgt_lang, model, translated_title) "
                    "VALUES (:a, 'en', 'zh', 'fake', '标题2')"
                ), {"a": aid})
                await session.commit()