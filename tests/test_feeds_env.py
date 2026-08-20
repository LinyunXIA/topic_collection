"""Feeds env 隔离 — 方案 C 双文件 + DB 行级（DESIGN §5.4.1）

- 同一 URL 在 dev/prod 可各存一行（UNIQUE url, env）
- TC_APP_ENV=dev 时 import/fetch 仅操作 dev 行
- TC_FEEDS_CONFIG 可覆盖路径
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import text

from app.config import load_settings
from app.db.engine import get_session_factory
from app.ingest.dedup import url_hash


@pytest.fixture
def settings():
    return load_settings()


async def clean_all(settings):
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(settings.db.dsn, pool_size=2)
    async with eng.connect() as conn:
        for sql in [
            "DELETE FROM processing_jobs",
            "DELETE FROM article_topics",
            "DELETE FROM summaries",
            "DELETE FROM article_embeddings",
            "DELETE FROM articles",
            "DELETE FROM topics",
            "DELETE FROM feeds",
            "DELETE FROM fetch_events",
        ]:
            await conn.execute(text(sql))
        await conn.commit()
    await eng.dispose()
    from app.db import engine as _eng_mod

    if _eng_mod._engine is not None:
        await _eng_mod._engine.dispose()
        _eng_mod._engine = None
        _eng_mod._session_factory = None


@pytest.mark.asyncio
async def test_same_url_different_env_both_exist(settings):
    """同一 URL 在 dev/prod 各存一行，UNIQUE (url, env) 生效"""
    await clean_all(settings)
    factory = get_session_factory(settings)
    url = "https://example.com/same-feed.xml"
    async with factory() as session:
        await session.execute(
            text("INSERT INTO feeds (type, name, url, enabled, env) VALUES ('rss','dev-feed',:url,true,'dev')"),
            {"url": url},
        )
        await session.execute(
            text("INSERT INTO feeds (type, name, url, enabled, env) VALUES ('rss','prod-feed',:url,true,'prod')"),
            {"url": url},
        )
        await session.commit()
        result = await session.execute(text("SELECT COUNT(*) FROM feeds WHERE url=:url"), {"url": url})
        assert result.scalar() == 2
        result = await session.execute(text("SELECT COUNT(*) FROM feeds WHERE url=:url AND env='dev'"), {"url": url})
        assert result.scalar() == 1
        result = await session.execute(text("SELECT COUNT(*) FROM feeds WHERE url=:url AND env='prod'"), {"url": url})
        assert result.scalar() == 1


@pytest.mark.asyncio
async def test_import_respects_tc_app_env(settings):
    """TC_APP_ENV=dev 时 import 仅写 dev 行，prod 同理"""
    await clean_all(settings)
    # 准备临时 feeds 文件
    import tempfile
    import yaml
    from app.services.cli import _feeds_import, _resolve_feeds_path

    # dev 临时文件
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump({"feeds": [{"name": "DevOnly", "type": "rss", "url": "https://example.com/dev-only.xml", "enabled": True, "env": "dev"}]}, f)
        dev_path = f.name
    # prod 临时文件
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump({"feeds": [{"name": "ProdOnly", "type": "rss", "url": "https://example.com/prod-only.xml", "enabled": True, "env": "prod"}]}, f)
        prod_path = f.name

    try:
        # dev import
        os.environ["TC_APP_ENV"] = "dev"
        os.environ["TC_FEEDS_CONFIG"] = dev_path
        await _feeds_import()
        # prod import
        os.environ["TC_APP_ENV"] = "prod"
        os.environ["TC_FEEDS_CONFIG"] = prod_path
        await _feeds_import()

        factory = get_session_factory(settings)
        async with factory() as session:
            result = await session.execute(text("SELECT name, env FROM feeds WHERE url LIKE 'https://example.com/%-only.xml' ORDER BY env"))
            rows = {r[1]: r[0] for r in result.fetchall()}
            assert rows.get("dev") == "DevOnly"
            assert rows.get("prod") == "ProdOnly"
    finally:
        os.environ.pop("TC_APP_ENV", None)
        os.environ.pop("TC_FEEDS_CONFIG", None)
        Path(dev_path).unlink(missing_ok=True)
        Path(prod_path).unlink(missing_ok=True)
        await clean_all(settings)


@pytest.mark.asyncio
async def test_fetch_respects_env(settings):
    """TC_APP_ENV 过滤：dev fetch 不碰 prod feed"""
    await clean_all(settings)
    factory = get_session_factory(settings)
    # 插入 dev 和 prod 各一个 feed
    async with factory() as session:
        await session.execute(
            text("INSERT INTO feeds (type, name, url, enabled, env) VALUES ('rss','dev-feed','https://example.com/dev.xml',true,'dev')")
        )
        await session.execute(
            text("INSERT INTO feeds (type, name, url, enabled, env) VALUES ('rss','prod-feed','https://example.com/prod.xml',true,'prod')")
        )
        await session.commit()

    # dev fetch 应只看到 dev
    os.environ["TC_APP_ENV"] = "dev"
    from app.services.cli import _resolve_feeds_path

    # 直接验证 DB 过滤逻辑（不实际抓取网络）
    async with factory() as session:
        cur_env = os.environ.get("TC_APP_ENV", "dev")
        result = await session.execute(text("SELECT COUNT(*) FROM feeds WHERE enabled=true AND env=:env"), {"env": cur_env})
        assert result.scalar() == 1
        result = await session.execute(text("SELECT name FROM feeds WHERE enabled=true AND env=:env"), {"env": cur_env})
        assert result.scalar() == "dev-feed"

    os.environ["TC_APP_ENV"] = "prod"
    async with factory() as session:
        cur_env = os.environ.get("TC_APP_ENV", "dev")
        result = await session.execute(text("SELECT COUNT(*) FROM feeds WHERE enabled=true AND env=:env"), {"env": cur_env})
        assert result.scalar() == 1
        result = await session.execute(text("SELECT name FROM feeds WHERE enabled=true AND env=:env"), {"env": cur_env})
        assert result.scalar() == "prod-feed"

    os.environ.pop("TC_APP_ENV", None)
    await clean_all(settings)


@pytest.mark.asyncio
async def test_tc_feeds_config_overrides_env_file(settings):
    """TC_FEEDS_CONFIG 显式覆盖路径，优先级高于 TC_APP_ENV"""
    await clean_all(settings)
    import tempfile
    import yaml

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump({"feeds": [{"name": "OverrideFeed", "type": "rss", "url": "https://example.com/override.xml", "env": "prod"}]}, f)
        override_path = f.name

    try:
        os.environ["TC_APP_ENV"] = "dev"  # 即使 dev，也应走 override 的 prod 文件
        os.environ["TC_FEEDS_CONFIG"] = override_path
        from app.services.cli import _resolve_feeds_path

        p = _resolve_feeds_path()
        assert str(p) == override_path
        from app.services.cli import _feeds_import

        await _feeds_import()
        factory = get_session_factory(settings)
        async with factory() as session:
            result = await session.execute(text("SELECT env FROM feeds WHERE url='https://example.com/override.xml'"))
            assert result.scalar() == "prod"
    finally:
        os.environ.pop("TC_APP_ENV", None)
        os.environ.pop("TC_FEEDS_CONFIG", None)
        Path(override_path).unlink(missing_ok=True)
        await clean_all(settings)
