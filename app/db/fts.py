"""全文检索：jieba 预切词 + tsvector 维护 — DESIGN §5.3

写入：to_tsvector('simple', jieba_join(text))  -- 不要用 array_to_tsquery
查询：websearch_to_tsquery('simple', jieba(q))  -- 不要用裸 to_tsquery
"""

from __future__ import annotations

import asyncio
import json
import logging

import jieba
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def jieba_join(text_content: str) -> str:
    """用 jieba 切词后以空格拼接，供 to_tsvector('simple', ...) 使用。"""
    tokens = jieba.cut_for_search(text_content)
    return " ".join(t.strip() for t in tokens if t.strip())


async def jieba_join_async(text_content: str) -> str:
    """CPU 密集切词走线程池，不阻塞事件循环（DESIGN §2）。"""
    return await asyncio.to_thread(jieba_join, text_content)


async def update_article_tsv(
    session: AsyncSession,
    article_id: int,
    title: str,
    content_text: str,
    summary_text: str = "",
    key_points_text: str = "",
) -> None:
    """刷新文章的 tsv 列（两阶段设计，DESIGN §5.3）。

    阶段一（入库时）：title + content_text
    阶段二（summarize 后）：+ summary_text + key_points_text

    tsv 是整列覆盖写，所以每次调用都必须重建**完整**索引，否则后一阶段会
    抹掉前一阶段的段落。调用方只传自己那一段即可——缺失的段落在此从库里补齐：
    - title/content_text 都为空 → 从 articles 读回（阶段二场景）
    - summary/key_points 都为空 → 从 summaries 读回（阶段一重跑 / backfill 场景）

    两段拼接用同一 jieba_join 确保关键词通道一致。
    """
    if not title and not content_text:
        row = (
            await session.execute(
                text("SELECT title, content_text FROM articles WHERE id = :aid"),
                {"aid": article_id},
            )
        ).mappings().first()
        if row:
            title = row["title"] or ""
            content_text = row["content_text"] or ""

    if not summary_text and not key_points_text:
        row = (
            await session.execute(
                text(
                    "SELECT summary_text, key_points_json FROM summaries "
                    "WHERE article_id = :aid AND lang = 'zh' "
                    "ORDER BY id DESC LIMIT 1"
                ),
                {"aid": article_id},
            )
        ).mappings().first()
        if row:
            summary_text = row["summary_text"] or ""
            kp = row["key_points_json"] or []
            if isinstance(kp, str):
                try:
                    kp = json.loads(kp)
                except ValueError:
                    kp = []
            if isinstance(kp, list):
                key_points_text = " ".join(str(k) for k in kp)

    parts = [title, content_text]
    if summary_text:
        parts.append(summary_text)
    if key_points_text:
        parts.append(key_points_text)

    raw = " ".join(p for p in parts if p)
    joined = await jieba_join_async(raw)

    await session.execute(
        text(
            "UPDATE articles SET tsv = to_tsvector('simple', :joined) WHERE id = :aid"
        ),
        {"joined": joined, "aid": article_id},
    )


async def search_articles_fts(
    session: AsyncSession,
    query: str,
    limit: int = 20,
) -> list[int]:
    """关键词全文搜索，返回 article_id 列表。使用 websearch_to_tsquery（DESIGN §5.3）。"""
    q_joined = await jieba_join_async(query)
    result = await session.execute(
        text(
            "SELECT id FROM articles "
            "WHERE tsv @@ websearch_to_tsquery('simple', :q) "
            "AND dedupe_of IS NULL "
            "ORDER BY ts_rank(tsv, websearch_to_tsquery('simple', :q)) DESC "
            "LIMIT :limit"
        ),
        {"q": q_joined, "limit": limit},
    )
    return [row[0] for row in result.fetchall()]
