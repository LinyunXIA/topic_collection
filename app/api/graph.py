"""图谱 — Phase 2（DESIGN §14 2.4）"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.services.graph import graph_json, graph_node_articles

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


@router.get("/graph", response_class=HTMLResponse)
async def graph_page(request: Request):
    return templates.TemplateResponse(request, "graph/page.html", {"request": request})


@router.get("/api/graph.json")
async def graph_data(
    topic_id: int | None = None,
    entity_type: str | None = None,
    since_days: int | None = None,
    max_nodes: int = 300,
    session: AsyncSession = Depends(get_session),
):
    data = await graph_json(session, topic_id=topic_id, entity_type=entity_type, since_days=since_days, max_nodes=max_nodes)
    return JSONResponse(data)


@router.get("/api/graph/node/{node_id}/articles")
async def node_articles(node_id: int, session: AsyncSession = Depends(get_session)):
    arts = await graph_node_articles(session, node_id)
    return {"articles": arts}
