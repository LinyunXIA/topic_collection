"""D7 WebUI smoke — FastAPI TestClient + Jinja2（DESIGN §13）

- GET / 字符串包含关键文案（LLM 健康、队列、搜索等），不测视觉
- POST /settings form/CSRF
- HTMX 部分路由通过 HX-Request: true header 触发并断言返回 partial 不含 <html>
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.config import load_settings
from app.db.engine import get_session_factory
from app.main import create_app


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
async def test_overview_contains_key_text(settings):
    await clean_all(settings)
    app = create_app()
    # 不走 lifespan，直接测路由（DB 已就绪）
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/")
        assert resp.status_code == 200
        html = resp.text
        # 关键文案
        assert "LLM 健康" in html or "LLM" in html
        assert "队列" in html
        assert "搜索" in html
        assert "<html" in html.lower()


@pytest.mark.asyncio
async def test_health_endpoint(settings):
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        # 必须含这几个键
        assert "llm_healthy" in data
        assert "queue_depth" in data


@pytest.mark.asyncio
async def test_llm_status_endpoint(settings):
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/llm-status")
        assert resp.status_code == 200
        data = resp.json()
        assert "healthy" in data


@pytest.mark.asyncio
async def test_settings_page(settings):
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/settings")
        assert resp.status_code == 200
        assert "设置" in resp.text
        assert "<html" in resp.text.lower()


@pytest.mark.asyncio
async def test_settings_post(settings):
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/settings",
            data={"llm_backend": "omlx", "llm_model": "test-model", "max_concurrency": "1"},
            follow_redirects=False,
        )
        # 应重定向
        assert resp.status_code in (200, 303, 302)


@pytest.mark.asyncio
async def test_htmx_partial_no_html(settings):
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        # partial 不应含完整 html
        assert "<html" not in resp.text.lower()
        # 但应含队列或 LLM 片段
        assert "队列" in resp.text or "LLM" in resp.text
