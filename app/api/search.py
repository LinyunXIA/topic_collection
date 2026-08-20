"""搜索 — Phase 2（DESIGN §14 2.6）

GET /search?q=&mode=&use_rerank=&page=
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_llm_client, get_session, get_settings
from app.config import Settings
from app.llm.client import LLMClient
from app.services.search import search as hybrid_search

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


@router.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: str = "",
    mode: str = "hybrid",
    use_rerank: bool = False,
    page: int = 1,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    llm_client: LLMClient | None = Depends(get_llm_client),
):
    if not q:
        return templates.TemplateResponse(request, "search/results.html", {"results": [], "q": q, "mode": mode})
    resp = await hybrid_search(session, q, settings, llm_client, mode=mode, limit=20, use_rerank=use_rerank, page=page, page_size=20)
    return templates.TemplateResponse(request, "search/results.html", {"results": resp.results, "q": q, "mode": mode, "use_rerank": use_rerank})
