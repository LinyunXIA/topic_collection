"""Wiki 浏览 — Phase 2（DESIGN §14 2.1.2）"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


@router.get("/wiki", response_class=HTMLResponse)
async def wiki_index(request: Request, q: str | None = None, kind: str | None = None, session: AsyncSession = Depends(get_session)):
    result = await session.execute(text("SELECT id, kind, title, slug FROM wiki_pages ORDER BY updated_at DESC LIMIT 50"))
    pages = [dict(r) for r in result.mappings().all()]
    return templates.TemplateResponse(request, "wiki/index.html", {"pages": pages, "q": q, "kind": kind})


@router.get("/wiki/{slug}", response_class=HTMLResponse)
async def wiki_page(slug: str, request: Request, session: AsyncSession = Depends(get_session)):
    result = await session.execute(text("SELECT * FROM wiki_pages WHERE slug=:slug"), {"slug": slug})
    row = result.mappings().first()
    if not row:
        return HTMLResponse("Not Found", status_code=404)
    return templates.TemplateResponse(request, "wiki/page.html", {"page": dict(row)})


@router.get("/wiki/{slug}/raw", response_class=PlainTextResponse)
async def wiki_raw(slug: str, session: AsyncSession = Depends(get_session)):
    result = await session.execute(text("SELECT content_md FROM wiki_pages WHERE slug=:slug"), {"slug": slug})
    row = result.first()
    if not row:
        return HTMLResponse("Not Found", status_code=404)
    return row[0] or ""
