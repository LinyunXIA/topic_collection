"""主题管理 — CRUD + 关键词快路径（DESIGN §6/§14）

match_keywords(): jieba 切词后匹配主题关键词，命中即记 article_topics(method='keyword')。
"""

from __future__ import annotations

import asyncio
import json
import logging

import jieba
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


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
