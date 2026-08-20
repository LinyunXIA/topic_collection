"""报告 — Phase 2（DESIGN §14 2.5）"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session, get_settings
from app.config import Settings

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


@router.get("/reports", response_class=HTMLResponse)
async def reports_list(request: Request, report_type: str | None = None, limit: int = 20, session: AsyncSession = Depends(get_session)):
    sql = "SELECT id, report_type, period_start, period_end, status, created_at FROM reports ORDER BY created_at DESC LIMIT :limit"
    result = await session.execute(text(sql), {"limit": limit})
    reports = [dict(r) for r in result.mappings().all()]
    return templates.TemplateResponse(request, "reports/list.html", {"reports": reports})


@router.get("/reports/{report_id}", response_class=HTMLResponse)
async def report_view(report_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    result = await session.execute(text("SELECT * FROM reports WHERE id=:id"), {"id": report_id})
    row = result.mappings().first()
    if not row:
        return HTMLResponse("Not Found", status_code=404)
    return templates.TemplateResponse(request, "reports/view.html", {"report": dict(row)})


@router.get("/reports/{report_id}/export.md", response_class=PlainTextResponse)
async def report_export(report_id: int, session: AsyncSession = Depends(get_session)):
    result = await session.execute(text("SELECT content_md FROM reports WHERE id=:id"), {"id": report_id})
    row = result.first()
    if not row:
        return HTMLResponse("Not Found", status_code=404)
    return row[0] or ""


@router.post("/reports/{report_id}/retry")
async def report_retry(report_id: int, session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings)):
    from app.services.reports import generate_daily_report

    # 简化：重跑日报
    result = await session.execute(text("SELECT report_type, period_start FROM reports WHERE id=:id"), {"id": report_id})
    row = result.mappings().first()
    if not row:
        return HTMLResponse("Not Found", status_code=404)
    # 触发重新生成（用当前时间）
    from datetime import datetime

    await generate_daily_report(session, datetime.now(), settings, None)
    return {"ok": True}
