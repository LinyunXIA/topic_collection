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
    """文章列表（筛选+分页，Phase 2）"""
    # 动态 WHERE 构建（§3.1: feed/topic/status/q）
    where = ["a.dedupe_of IS NULL"]
    params: dict = {"off": (page - 1) * 20, "limit": 20}
    join_topic = False
    if feed:
        # feed 可能是 id 或 name
        if feed.isdigit():
            where.append("a.feed_id = :feed_id")
            params["feed_id"] = int(feed)
        else:
            where.append("a.feed_id IN (SELECT id FROM feeds WHERE name = :feed_name)")
            params["feed_name"] = feed
    if topic:
        join_topic = True
        where.append("a.id IN (SELECT article_id FROM article_topics at JOIN topics t ON t.id = at.topic_id WHERE t.name = :topic_name)")
        params["topic_name"] = topic
    if status:
        where.append("a.status = :status")
        params["status"] = status
    if q and q.strip():
        # q 优先走 FTS，若 tsv 为空则降级 ILIKE
        try:
            from app.db.fts import jieba_join_async

            q_joined = await jieba_join_async(q.strip())
            where.append("a.tsv @@ websearch_to_tsquery('simple', :q)")
            params["q"] = q_joined
        except Exception:
            where.append("(a.title ILIKE :qlike OR a.content_text ILIKE :qlike)")
            params["qlike"] = f"%{q.strip()}%"
    where_sql = " AND ".join(where)
    # HTMX partial 支持
    is_hx = request.headers.get("HX-Request") == "true"
    template = "partials/article_row.html" if is_hx else "articles/list.html"
    # 查询
    sql = f"SELECT a.id, a.title, a.status, a.lang, a.published_at, a.feed_id FROM articles a WHERE {where_sql} ORDER BY a.fetched_at DESC LIMIT :limit OFFSET :off"
    result = await session.execute(text(sql), params)
    articles = [dict(row) for row in result.mappings().all()]
    # 分页总数（简化：不计总数，仅传 page）
    ctx = {"articles": articles, "page": page, "feed": feed, "topic": topic, "status": status, "q": q}
    if is_hx:
        return templates.TemplateResponse(request, template, ctx)
    return templates.TemplateResponse(request, "articles/list.html", ctx)


@router.get("/articles/{article_id}", response_class=HTMLResponse)
async def article_detail(
    article_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """文章详情 7 Tab（翻译 Tab 空时显示 CTA，含 entities/topics/wiki）"""
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
    # 实体列表（canonical_name_zh + type + confidence）
    entities = []
    try:
        er = await session.execute(
            text(
                "SELECT e.id, e.canonical_name_zh, e.entity_type, e.confidence, ae.surface "
                "FROM article_entities ae JOIN entities e ON e.id = ae.entity_id "
                "WHERE ae.article_id = :aid ORDER BY ae.confidence DESC LIMIT 20"
            ),
            {"aid": article_id},
        )
        entities = [dict(r) for r in er.mappings().all()]
    except Exception:
        pass
    # 话题列表
    topics = []
    try:
        tr2 = await session.execute(
            text(
                "SELECT t.name, at.score, at.method FROM article_topics at "
                "JOIN topics t ON t.id = at.topic_id WHERE at.article_id = :aid ORDER BY at.score DESC"
            ),
            {"aid": article_id},
        )
        topics = [dict(r) for r in tr2.mappings().all()]
    except Exception:
        pass
    # Wiki 词条（article kind）
    wiki = None
    try:
        wr = await session.execute(
            text("SELECT id, title, slug, content_md FROM wiki_pages WHERE kind='article' AND ref_id=:aid LIMIT 1"),
            {"aid": article_id},
        )
        wrow = wr.mappings().first()
        if wrow:
            wiki = dict(wrow)
    except Exception:
        pass
    return templates.TemplateResponse(
        request,
        "articles/detail.html",
        {
            "article": dict(row),
            "translation": dict(tro) if tro else None,
            "translating": translating,
            "entities": entities,
            "topics": topics,
            "wiki": wiki,
        },
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
