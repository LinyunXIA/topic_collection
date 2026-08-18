"""全文检索：jieba 预切词 + tsvector 维护 — DESIGN §5.3

写入：to_tsvector('simple', jieba_join(text))  -- 不要用 array_to_tsquery
查询：websearch_to_tsquery('simple', jieba(q))  -- 不要用裸 to_tsquery
"""

from __future__ import annotations

import asyncio
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
    两段拼接用同一 jieba_join 确保关键词通道一致。
    """
    parts = [title, content_text]
    if summary_text:
        parts.append(summary_text)
    if key_points_text:
        parts.append(key_points_text)

    raw = " ".join(parts)
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
