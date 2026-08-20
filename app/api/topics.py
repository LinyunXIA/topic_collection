"""主题 — Phase 2（DESIGN §14）"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


@router.get("/topics", response_class=HTMLResponse)
async def topics_list(request: Request, session: AsyncSession = Depends(get_session)):
    result = await session.execute(text("SELECT id, name, description, keywords_json, enabled FROM topics ORDER BY id"))
    topics = [dict(r) for r in result.mappings().all()]
    # 跨源聚合：简化为 topics 本身
    return templates.TemplateResponse(request, "topics/list.html", {"topics": topics})


@router.get("/topics/new", response_class=HTMLResponse)
async def topics_new(request: Request):
    return templates.TemplateResponse(request, "topics/edit.html", {"topic": None})


@router.post("/topics")
async def topics_create(
    request: Request,
    name: str = Form(...),
    keywords_csv: str = Form(""),
    description: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    keywords = [k.strip() for k in keywords_csv.split(",") if k.strip()]
    import json

    await session.execute(text("INSERT INTO topics (name, description, keywords_json) VALUES (:name, :desc, :kw)"), {"name": name, "desc": description, "kw": json.dumps(keywords, ensure_ascii=False)})
    await session.commit()
    result = await session.execute(text("SELECT id FROM topics WHERE name=:name ORDER BY id DESC LIMIT 1"), {"name": name})
    tid = result.scalar()
    return RedirectResponse(url=f"/topics/{tid}/edit", status_code=303)


@router.get("/topics/{topic_id}/edit", response_class=HTMLResponse)
async def topics_edit(topic_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    result = await session.execute(text("SELECT * FROM topics WHERE id=:id"), {"id": topic_id})
    row = result.mappings().first()
    if not row:
        return HTMLResponse("Not Found", status_code=404)
    return templates.TemplateResponse(request, "topics/edit.html", {"topic": dict(row)})


@router.post("/topics/{topic_id}")
async def topics_update(
    topic_id: int,
    request: Request,
    name: str = Form(...),
    keywords_csv: str = Form(""),
    description: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    import json

    keywords = [k.strip() for k in keywords_csv.split(",") if k.strip()]
    await session.execute(text("UPDATE topics SET name=:name, description=:desc, keywords_json=:kw WHERE id=:id"), {"name": name, "desc": description, "kw": json.dumps(keywords, ensure_ascii=False), "id": topic_id})
    await session.commit()
    return RedirectResponse(url=f"/topics/{topic_id}/edit", status_code=303)


@router.post("/topics/{topic_id}/reclassify")
async def topics_reclassify(topic_id: int, session: AsyncSession = Depends(get_session)):
    from app.config import load_settings
    from app.services.topics import reclassify_recent

    settings = load_settings()
    n = await reclassify_recent(session, topic_id, settings)
    await session.commit()
    return {"ok": True, "requeued": n}
