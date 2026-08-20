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
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    """更新设置（演示：仅回显，实际持久化需写 config.yaml + 重启）"""
    # Phase 2 骨架：只做表单校验 + 调 service（薄封装），不直接写 DB
    # 这里仅重定向回设置页并带 toast 参数
    # 真实持久化由 app/services/settings.py 负责（P2 完整）
    return RedirectResponse(url="/settings?saved=1", status_code=303)
