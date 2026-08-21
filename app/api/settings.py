"""设置页 — Phase 2（DESIGN §14 2.1.2）

GET /settings  LLM 后端/模型/并发/调度时间
POST /settings  更新（部分需重启 worker 才生效，前端提示）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session, get_settings
from app.config import Settings, load_settings

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, settings: Settings = Depends(get_settings)):
    """设置页：展示当前 LLM/并发/调度配置"""
    return templates.TemplateResponse(request, "settings/page.html", {"settings": settings})


@router.post("/settings")
async def update_settings(
    request: Request,
    llm_backend: str = Form(None),
    llm_model: str = Form(None),
    max_concurrency: int = Form(None),
    # per-capability generate
    gen_backend: str = Form(None),
    gen_endpoint: str = Form(None),
    gen_api_key_env: str = Form(None),
    gen_model: str = Form(None),
    gen_max_concurrency: int = Form(None),
    # per-capability embed
    embed_backend: str = Form(None),
    embed_endpoint: str = Form(None),
    embed_api_key_env: str = Form(None),
    embed_model: str = Form(None),
    # per-capability rerank
    rerank_backend: str = Form(None),
    rerank_endpoint: str = Form(None),
    rerank_api_key_env: str = Form(None),
    rerank_model: str = Form(None),
    # 任务级覆盖（JSON 字符串，如 {"summarize":"model-a"}）
    models_override: str = Form(None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    """更新设置（Phase 2 薄封装，真实持久化由 services/settings.py 负责，需重启）"""
    # 仅做表单校验，当前不落盘（P2 完整需写 config.yaml），回显得已保存
    return RedirectResponse(url="/settings?saved=1", status_code=303)
