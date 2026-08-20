"""概览页 — Phase 2（DESIGN §14 2.1.2）

GET / 流水线统计/队列/LLM 健康/最近 20 篇/源健康
GET/POST /settings 模型与并发配置（settings_api 另起，这里仅概览）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_llm_client, get_session, get_settings
from app.config import Settings
from app.llm.client import LLMClient

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


@router.get("/", response_class=HTMLResponse)
async def overview(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    llm_client: LLMClient | None = Depends(get_llm_client),
):
    """概览页：统计、队列、LLM 横幅、最近文章、源健康（DB 失败时降级为 0/空列表，不阻塞渲染）"""
    # 统计（降级：DB 不可用时返回 0/{}）
    try:
        result = await session.execute(text("SELECT COUNT(*) FROM articles WHERE dedupe_of IS NULL"))
        total_articles = result.scalar() or 0
    except Exception:
        total_articles = 0
    try:
        result = await session.execute(text("SELECT COUNT(*) FROM articles WHERE created_at > now() - INTERVAL '1 day' AND dedupe_of IS NULL"))
        today_articles = result.scalar() or 0
    except Exception:
        today_articles = 0
    try:
        result = await session.execute(text("SELECT status, COUNT(*) FROM processing_jobs GROUP BY status"))
        queue = {row[0]: row[1] for row in result.fetchall()}
    except Exception:
        queue = {}
    try:
        result = await session.execute(text("SELECT COUNT(*) FROM articles WHERE status='pending'"))
        pending = result.scalar() or 0
    except Exception:
        pending = 0

    # 最近 20 篇
    try:
        result = await session.execute(text("SELECT id, title, status, lang, published_at FROM articles WHERE dedupe_of IS NULL ORDER BY fetched_at DESC LIMIT 20"))
        recent = [dict(row) for row in result.mappings().all()]
    except Exception:
        recent = []

    # 源健康
    try:
        result = await session.execute(text("SELECT name, fetch_status, fetch_failures, last_error FROM feeds ORDER BY fetch_failures DESC LIMIT 10"))
        feeds = [dict(row) for row in result.mappings().all()]
    except Exception:
        feeds = []

    # LLM 健康（非阻塞，失败则 unknown）
    llm_healthy = None
    llm_error = None
    if llm_client:
        try:
            st = await llm_client.healthcheck()
            llm_healthy = st.healthy
            llm_error = st.error
        except Exception as e:
            llm_healthy = False
            llm_error = str(e)
    else:
        llm_healthy = None

    # 若HTMX请求，只返回横幅 partial
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request, "partials/health_banner.html", {"llm_healthy": llm_healthy, "llm_error": llm_error, "queue": queue}
        )

    return templates.TemplateResponse(
        request,
        "overview.html",
        {
            "total_articles": total_articles,
            "today_articles": today_articles,
            "queue": queue,
            "pending": pending,
            "recent": recent,
            "feeds": feeds,
            "llm_healthy": llm_healthy,
            "llm_error": llm_error,
        },
    )
