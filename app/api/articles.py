"""文章详情 — Phase 2（DESIGN §14 2.2.4）

GET /articles/{id} 详情（7 Tab：原文/摘要/翻译/实体/相关话题/Wiki）
POST /articles/{id}/retry/{task} 手动重试
GET /api/articles/{id}/translate_status 翻译轮询（translating徽标）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session, get_settings
from app.config import Settings

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


@router.get("/articles", response_class=HTMLResponse)
async def list_articles(
    request: Request,
    feed: str | None = None,
    topic: str | None = None,
    status: str | None = None,
    q: str | None = None,
    page: int = 1,
    session: AsyncSession = Depends(get_session),
):
    """文章列表（筛选+分页，Phase 2 骨架）"""
    # 简化：不做复杂筛选，仅按 dedupe_of IS NULL 分页
    result = await session.execute(
        text("SELECT id, title, status, lang, published_at FROM articles WHERE dedupe_of IS NULL ORDER BY fetched_at DESC LIMIT 20 OFFSET :off"),
        {"off": (page - 1) * 20},
    )
    articles = [dict(row) for row in result.mappings().all()]
    return templates.TemplateResponse(request, "articles/list.html", {"articles": articles, "page": page})


@router.get("/articles/{article_id}", response_class=HTMLResponse)
async def article_detail(
    article_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """文章详情 7 Tab（翻译 Tab 空时显示 CTA）"""
    result = await session.execute(
        text("SELECT a.*, s.summary_text, s.key_points_json FROM articles a LEFT JOIN summaries s ON s.article_id=a.id AND s.lang='zh' WHERE a.id=:aid"),
        {"aid": article_id},
    )
    row = result.mappings().first()
    if not row:
        return HTMLResponse("Not Found", status_code=404)
    # 翻译
    tr = await session.execute(
        text("SELECT translated_title, translated_content FROM translations WHERE article_id=:aid AND tgt_lang='zh' ORDER BY id DESC LIMIT 1"),
        {"aid": article_id},
    )
    tro = tr.mappings().first()
    translating = False
    if not tro:
        # 检查是否有 queued/running translate job
        jr = await session.execute(
            text("SELECT 1 FROM processing_jobs WHERE article_id=:aid AND task='translate' AND status IN ('queued','running') LIMIT 1"),
            {"aid": article_id},
        )
        translating = jr.first() is not None
    return templates.TemplateResponse(
        request,
        "articles/detail.html",
        {"article": dict(row), "translation": dict(tro) if tro else None, "translating": translating},
    )


@router.get("/api/articles/{article_id}/translate_status")
async def translate_status(article_id: int, session: AsyncSession = Depends(get_session)):
    """翻译轮询：translating true/false + 内容"""
    tr = await session.execute(
        text("SELECT translated_content FROM translations WHERE article_id=:aid AND tgt_lang='zh' ORDER BY id DESC LIMIT 1"),
        {"aid": article_id},
    )
    row = tr.first()
    if row and row[0]:
        return {"translating": False, "translated": True}
    jr = await session.execute(
        text("SELECT 1 FROM processing_jobs WHERE article_id=:aid AND task='translate' AND status IN ('queued','running') LIMIT 1"),
        {"aid": article_id},
    )
    if jr.first():
        return {"translating": True, "translated": False}
    return {"translating": False, "translated": False}


@router.post("/articles/{article_id}/retry/{task}")
async def retry_task(article_id: int, task: str, session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings)):
    """手动重试（薄封装，走 pipeline enqueue）"""
    from app.pipeline import enqueue_jobs
    from sqlalchemy import text as sql_text

    # 取 content_hash
    result = await session.execute(sql_text("SELECT content_hash FROM articles WHERE id=:aid"), {"aid": article_id})
    ch = result.scalar()
    if not ch:
        return JSONResponse({"error": "article not found"}, status_code=404)
    # 仅允许白名单 task
    if task not in ("summarize", "translate", "topics", "wiki", "embed_core", "embed_summary", "extract_entities"):
        return JSONResponse({"error": "unknown task"}, status_code=422)
    await enqueue_jobs(session, article_id, [task], ch)
    await session.commit()
    return {"ok": True, "task": task}
