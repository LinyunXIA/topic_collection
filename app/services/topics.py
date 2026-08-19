"""主题管理 — CRUD + 关键词快路径 + LLM 慢路径（DESIGN §6/§14）

match_keywords(): jieba 切词后匹配主题关键词，命中即记 article_topics(method='keyword')。
classify_topics(): LLM 对未命中关键词的文章打分 0-1（method='llm'）。
"""

from __future__ import annotations

import asyncio
import json
import logging

import jieba
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ── CRUD ───────────────────────────────────────────────────────────

async def create_topic(
    session: AsyncSession,
    name: str,
    keywords: list[str],
    description: str = "",
) -> int:
    """创建主题，返回 topic_id。"""
    result = await session.execute(
        text(
            "INSERT INTO topics (name, description, keywords_json, enabled) "
            "VALUES (:name, :desc, :kw, true) "
            "RETURNING id"
        ),
        {"name": name, "desc": description, "kw": json.dumps(keywords, ensure_ascii=False)},
    )
    return result.scalar()


async def list_topics(session: AsyncSession) -> list[dict]:
    """列出所有启用主题。"""
    result = await session.execute(
        text("SELECT id, name, description, keywords_json, enabled FROM topics ORDER BY id")
    )
    return [dict(row) for row in result.mappings().all()]


async def get_topic(session: AsyncSession, topic_id: int) -> dict | None:
    """获取单个主题。"""
    result = await session.execute(
        text("SELECT id, name, description, keywords_json, enabled FROM topics WHERE id=:tid"),
        {"tid": topic_id},
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def update_topic(
    session: AsyncSession,
    topic_id: int,
    name: str | None = None,
    keywords: list[str] | None = None,
    description: str | None = None,
    enabled: bool | None = None,
) -> None:
    """更新主题字段。"""
    updates = []
    params: dict = {"tid": topic_id}
    if name is not None:
        updates.append("name=:name")
        params["name"] = name
    if keywords is not None:
        updates.append("keywords_json=:kw")
        params["kw"] = json.dumps(keywords, ensure_ascii=False)
    if description is not None:
        updates.append("description=:desc")
        params["desc"] = description
    if enabled is not None:
        updates.append("enabled=:enabled")
        params["enabled"] = enabled
    if updates:
        await session.execute(
            text(f"UPDATE topics SET {', '.join(updates)} WHERE id=:tid"),
            params,
        )


async def delete_topic(session: AsyncSession, topic_id: int) -> None:
    """删除主题（CASCADE 会清理 article_topics）。"""
    await session.execute(text("DELETE FROM topics WHERE id=:tid"), {"tid": topic_id})


# ── 关键词快路径 ───────────────────────────────────────────────────


async def match_keywords(session: AsyncSession, article_id: int) -> list[int]:
    """关键词快路径：匹配启用主题的关键词，命中即写 article_topics。

    返回命中的 topic_id 列表。
    CPU 密集（jieba）走线程池。
    """
    # 获取文章 title + content
    result = await session.execute(
        text("SELECT title, content_text FROM articles WHERE id=:aid"),
        {"aid": article_id},
    )
    row = result.mappings().first()
    if not row:
        return []

    title = row["title"] or ""
    content = row["content_text"] or ""
    text_content = f"{title} {content}"

    # 获取启用的主题及关键词
    result = await session.execute(
        text("SELECT id, name, keywords_json FROM topics WHERE enabled=true")
    )
    topics = result.mappings().all()

    if not topics:
        return []

    # jieba 切词（CPU 密集，走线程池）
    tokens = await asyncio.to_thread(lambda: list(jieba.cut_for_search(text_content)))
    token_set = set(t.lower().strip() for t in tokens if t.strip())

    matched_topic_ids: list[int] = []

    for topic in topics:
        topic_id = topic["id"]
        keywords = topic["keywords_json"] or []
        if not isinstance(keywords, list):
            continue

        # 计算命中强度
        hit_count = 0
        title_hits = 0
        for kw in keywords:
            kw_lower = kw.lower().strip()
            if not kw_lower:
                continue
            # 标题命中加权
            if kw_lower in title.lower():
                title_hits += 1
                hit_count += 2
            # 正文命中
            elif kw_lower in token_set or kw_lower in text_content.lower():
                hit_count += 1

        if hit_count > 0:
            # score 由命中强度计算（DESIGN §6）
            score = min(1.0, hit_count * 0.2 + title_hits * 0.1)

            await session.execute(
                text(
                    "INSERT INTO article_topics (article_id, topic_id, score, method) "
                    "VALUES (:aid, :tid, :score, 'keyword') "
                    "ON CONFLICT (article_id, topic_id) DO NOTHING"
                ),
                {"aid": article_id, "tid": topic_id, "score": score},
            )
            matched_topic_ids.append(topic_id)
            logger.debug(
                "主题 %s 关键词命中: article=%d, score=%.2f",
                topic["name"], article_id, score,
            )

    return matched_topic_ids


async def get_topic_articles(
    session: AsyncSession,
    topic_id: int,
    limit: int = 20,
) -> list[dict]:
    """获取主题下的文章列表（过滤 loser，DESIGN §6）。"""
    result = await session.execute(
        text(
            "SELECT a.id, a.title, a.source_url, a.published_at, "
            "  at.score, at.method "
            "FROM article_topics at "
            "JOIN articles a ON a.id = at.article_id "
            "WHERE at.topic_id=:tid AND a.dedupe_of IS NULL "
            "ORDER BY at.score DESC, a.published_at DESC "
            "LIMIT :limit"
        ),
        {"tid": topic_id, "limit": limit},
    )
    return [dict(row) for row in result.mappings().all()]


# ── LLM 慢路径 ────────────────────────────────────────────────────

async def classify_topics(
    session: AsyncSession,
    article_id: int,
    settings,
    llm_client,
) -> list[int]:
    """LLM 分类慢路径：对未命中关键词的文章打分 0-1。

    仅当关键词快路径未命中任何主题时调用。
    score >= llm_threshold 记入 article_topics(method='llm')。
    返回命中的 topic_id 列表。
    """
    from app.llm.base import GenerateRequest
    from app.llm.prompts import get_prompt
    from app.llm.structured import parse_with_repair

    # 获取文章摘要（LLM 读 summary_zh 比外文全文 token 省）
    result = await session.execute(
        text(
            "SELECT a.title, s.summary_text "
            "FROM articles a "
            "LEFT JOIN summaries s ON s.article_id = a.id AND s.lang = 'zh' "
            "WHERE a.id = :aid"
        ),
        {"aid": article_id},
    )
    row = result.mappings().first()
    if not row:
        return []

    title = row["title"] or ""
    summary = row["summary_text"] or ""
    if not summary:
        return []

    # 获取启用主题
    result = await session.execute(
        text("SELECT id, name, keywords_json FROM topics WHERE enabled=true")
    )
    topics = result.mappings().all()
    if not topics:
        return []

    topics_json = json.dumps(
        [{"id": t["id"], "name": t["name"], "keywords": t["keywords_json"] or []} for t in topics],
        ensure_ascii=False,
    )

    system, user = get_prompt("classify_topics", topics_json=topics_json, title=title, summary=summary)
    model = settings.llm.models.get("topics", settings.llm.model)

    resp = await llm_client.generate(
        GenerateRequest(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            json_mode=True,
        )
    )

    parsed = parse_with_repair(resp.text, expected_keys=["scores"])
    if not parsed or "scores" not in parsed:
        return []

    scores = parsed["scores"]
    threshold = settings.topics.llm_threshold
    matched_topic_ids: list[int] = []

    for topic in topics:
        tid = topic["id"]
        # scores key 可能是 str(topic_id) 或 int
        score = scores.get(str(tid)) or scores.get(tid)
        if score is not None and float(score) >= threshold:
            await session.execute(
                text(
                    "INSERT INTO article_topics (article_id, topic_id, score, method) "
                    "VALUES (:aid, :tid, :score, 'llm') "
                    "ON CONFLICT (article_id, topic_id) DO NOTHING"
                ),
                {"aid": article_id, "tid": tid, "score": float(score)},
            )
            matched_topic_ids.append(tid)

    return matched_topic_ids


# ── 聚合查询 ──────────────────────────────────────────────────────

async def aggregate_topic(
    session: AsyncSession,
    topic_id: int,
    limit: int = 20,
) -> list[dict]:
    """主题聚合：跨源文章列表，过滤 loser（DESIGN §6），按 score DESC, published_at DESC。"""
    result = await session.execute(
        text(
            "SELECT a.id, a.title, a.source_url, a.lang, a.published_at, "
            "  at.score, at.method "
            "FROM article_topics at "
            "JOIN articles a ON a.id = at.article_id "
            "WHERE at.topic_id = :tid AND a.dedupe_of IS NULL "
            "ORDER BY at.score DESC, a.published_at DESC "
            "LIMIT :limit"
        ),
        {"tid": topic_id, "limit": limit},
    )
    return [dict(row) for row in result.mappings().all()]


async def reclassify_recent(
    session: AsyncSession,
    topic_id: int,
    settings,
) -> int:
    """主题变更重算：删除旧 keyword 行 + 重新 match_keywords + 入队未命中文章的 topics job。

    仅重算最近 reclassify_recent_days 天的文章。
    返回入队的 topics job 数量。
    """
    from app.pipeline import enqueue_jobs

    window_days = settings.topics.reclassify_recent_days

    # 1. 删除该主题的旧 keyword 行
    await session.execute(
        text(
            "DELETE FROM article_topics "
            "WHERE topic_id = :tid AND method = 'keyword'"
        ),
        {"tid": topic_id},
    )

    # 2. 获取近窗口内的活跃文章
    result = await session.execute(
        text(
            "SELECT id, content_hash FROM articles "
            "WHERE dedupe_of IS NULL "
            "AND fetched_at > now() - INTERVAL ':days days'"
        ),
        {"days": window_days},
    )
    articles = result.mappings().all()

    # 3. 对每篇文章重新 match_keywords
    requeued = 0
    for art in articles:
        matched = await match_keywords(session, art["id"])
        if not matched:
            # 关键词未命中 → 入队 topics job 走 LLM 慢路径
            await enqueue_jobs(session, art["id"], ["topics"], art["content_hash"])
            requeued += 1

    return requeued
