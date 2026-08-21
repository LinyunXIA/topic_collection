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
async def feeds_list(request: Request, env: str | None = None, session: AsyncSession = Depends(get_session)):
    # 支持 ?env=dev|prod 过滤，未传则显示全部（方案 C 双文件隔离，但 DB 行级仍需 env 区分）
    import os

    cur_env = env or os.environ.get("TC_APP_ENV", "dev")
    try:
        if env:
            result = await session.execute(text("SELECT id, name, url, type, enabled, env, fetch_status FROM feeds WHERE env=:env ORDER BY id"), {"env": env})
        else:
            result = await session.execute(text("SELECT id, name, url, type, enabled, env, fetch_status FROM feeds ORDER BY id"))
        feeds = [dict(r) for r in result.mappings().all()]
    except Exception:
        # 旧库无 env 列回退
        result = await session.execute(text("SELECT id, name, url, type, enabled, fetch_status FROM feeds ORDER BY id"))
        feeds = [dict(r) for r in result.mappings().all()]
        for f in feeds:
            f["env"] = cur_env
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
    env: str = Form("dev"),
    config_json: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    import json as _json
    import os
    from fastapi.responses import JSONResponse

    # 优先取表单 env，否则按当前 TC_APP_ENV
    env = env or os.environ.get("TC_APP_ENV", "dev")
    # config_json 校验（API/scrape 类型）
    cfg = None
    if config_json and config_json.strip():
        try:
            cfg = _json.loads(config_json)
        except Exception as e:
            return HTMLResponse(f"config_json 非法 JSON: {e}", status_code=422)
    cfg_str = _json.dumps(cfg, ensure_ascii=False) if cfg is not None else None
    try:
        await session.execute(
            text(
                "INSERT INTO feeds (type, name, url, enabled, env, config_json) "
                "VALUES (:type, :name, :url, :enabled, :env, CAST(:cfg AS jsonb)) "
                "ON CONFLICT (url, env) DO UPDATE SET name=EXCLUDED.name, type=EXCLUDED.type, enabled=EXCLUDED.enabled, config_json=EXCLUDED.config_json"
            ),
            {"type": type, "name": name, "url": url, "enabled": enabled, "env": env, "cfg": cfg_str},
        )
    except Exception:
        # 旧库无 env/config_json 列回退
        try:
            await session.execute(
                text("INSERT INTO feeds (type, name, url, enabled, config_json) VALUES (:type, :name, :url, :enabled, CAST(:cfg AS jsonb)) ON CONFLICT DO NOTHING"),
                {"type": type, "name": name, "url": url, "enabled": enabled, "cfg": cfg_str},
            )
        except Exception:
            await session.execute(text("INSERT INTO feeds (type, name, url, enabled) VALUES (:type, :name, :url, :enabled) ON CONFLICT DO NOTHING"), {"type": type, "name": name, "url": url, "enabled": enabled})
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
    env: str = Form("dev"),
    config_json: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    import json as _json

    cfg = None
    if config_json and config_json.strip():
        try:
            cfg = _json.loads(config_json)
        except Exception as e:
            return HTMLResponse(f"config_json 非法 JSON: {e}", status_code=422)
    cfg_str = _json.dumps(cfg, ensure_ascii=False) if cfg is not None else None
    try:
        await session.execute(
            text("UPDATE feeds SET name=:name, url=:url, type=:type, enabled=:enabled, env=:env, config_json=CAST(:cfg AS jsonb) WHERE id=:id"),
            {"name": name, "url": url, "type": type, "enabled": enabled, "env": env, "cfg": cfg_str, "id": feed_id},
        )
    except Exception:
        try:
            await session.execute(text("UPDATE feeds SET name=:name, url=:url, type=:type, enabled=:enabled, config_json=CAST(:cfg AS jsonb) WHERE id=:id"), {"name": name, "url": url, "type": type, "enabled": enabled, "cfg": cfg_str, "id": feed_id})
        except Exception:
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
