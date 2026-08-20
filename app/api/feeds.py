"""Feed 管理 — Phase 2（DESIGN §14 2.1.2）

GET /feeds 列表 + 筛选
POST /feeds 新增/编辑
POST /feeds/{id}/fetch 立即抓取
POST /feeds/{id}/disable 禁用
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


@router.get("/feeds", response_class=HTMLResponse)
async def feeds_list(request: Request, session: AsyncSession = Depends(get_session)):
    result = await session.execute(text("SELECT id, name, url, type, enabled, fetch_status FROM feeds ORDER BY id"))
    feeds = [dict(r) for r in result.mappings().all()]
    # HTMX partial
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(request, "partials/feeds_table.html", {"feeds": feeds})
    return templates.TemplateResponse(request, "feeds/list.html", {"feeds": feeds})


@router.get("/feeds/new", response_class=HTMLResponse)
async def feeds_new(request: Request):
    return templates.TemplateResponse(request, "feeds/edit.html", {"feed": None})


@router.post("/feeds")
async def feeds_create(
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    type: str = Form("rss"),
    enabled: bool = Form(True),
    session: AsyncSession = Depends(get_session),
):
    await session.execute(text("INSERT INTO feeds (type, name, url, enabled) VALUES (:type, :name, :url, :enabled)"), {"type": type, "name": name, "url": url, "enabled": enabled})
    await session.commit()
    return RedirectResponse(url="/feeds", status_code=303)


@router.get("/feeds/{feed_id}/edit", response_class=HTMLResponse)
async def feeds_edit(feed_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    result = await session.execute(text("SELECT * FROM feeds WHERE id=:id"), {"id": feed_id})
    feed = result.mappings().first()
    if not feed:
        return HTMLResponse("Not Found", status_code=404)
    return templates.TemplateResponse(request, "feeds/edit.html", {"feed": dict(feed)})


@router.post("/feeds/{feed_id}")
async def feeds_update(
    feed_id: int,
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    type: str = Form("rss"),
    enabled: bool = Form(True),
    session: AsyncSession = Depends(get_session),
):
    await session.execute(text("UPDATE feeds SET name=:name, url=:url, type=:type, enabled=:enabled WHERE id=:id"), {"name": name, "url": url, "type": type, "enabled": enabled, "id": feed_id})
    await session.commit()
    return RedirectResponse(url=f"/feeds/{feed_id}/edit", status_code=303)


@router.post("/feeds/{feed_id}/fetch")
async def feeds_fetch(feed_id: int, session: AsyncSession = Depends(get_session)):
    # 立即抓取（简化：仅标记）
    await session.execute(text("UPDATE feeds SET last_error='manual fetch triggered' WHERE id=:id"), {"id": feed_id})
    await session.commit()
    return {"ok": True, "feed_id": feed_id}


@router.post("/feeds/{feed_id}/disable")
async def feeds_disable(feed_id: int, session: AsyncSession = Depends(get_session)):
    await session.execute(text("UPDATE feeds SET enabled=false WHERE id=:id"), {"id": feed_id})
    await session.commit()
    return {"ok": True}
