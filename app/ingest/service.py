"""抓取服务层 — 统一 fetch → dedup → clean → insert → tsv → enqueue 流水线（fix #9.1）

scheduler 与 CLI 各自实现完整流水线会导致 dedup / enqueue 规则两边漂移。
本模块把"单 feed 全流程"收敛到一个函数，调用方只负责：
  - 选 feed 列表
  - 错误聚合（写入 fetch_failures）
  - 用户可见输出（console / logger）
"""

from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.fts import update_article_tsv
from app.ingest.dedup import content_hash, url_hash, apply_exact_dedup
from app.ingest.feeds import FeedFetcher
from app.pipeline import enqueue_jobs
from app.services.cleaner import clean_article
from app.services.topics import match_keywords

logger = logging.getLogger(__name__)


# 进度回调：调用方传入以接管用户可见输出（CLI = console.print, scheduler = logger.info）。
# 阶段：fetched / truncated / inserted / keywords / done
ProgressFn = Callable[[str, dict], None] | None


async def fetch_and_store(
    session: AsyncSession,
    feed: dict,
    fetcher: FeedFetcher,
    *,
    count: int | None = None,
    progress: ProgressFn = None,
) -> tuple[int, int]:
    """抓取单个 feed 并入库：去重 → 清洗 → 入 articles → tsv → enqueue → match_keywords。

    Returns:
        (new_count, truncated_count) —— 新增文章数 + 因 --count 截断丢弃的条数。

    Args:
        session: 已开启的 AsyncSession（调用方负责 commit）。
        feed: 单行 feeds 字典，含 id/name/url/etag/last_modified。
        fetcher: FeedFetcher 实例。
        count: 仅取前 N 条（DESIGN P1+.2）；None = 不限。
        progress: 阶段回调 (stage, payload)；CLI 用来打 console，scheduler 留 None。

    事务边界：本函数只在一个 feed 上 commit 一次；多 feed 由调用方在外层循环。
    """
    items, new_etag, new_lm = await fetcher.fetch_feed(
        feed_id=feed["id"],
        url=feed["url"],
        etag=feed.get("etag"),
        last_modified=feed.get("last_modified"),
    )

    truncated = 0
    if count is not None and len(items) > count:
        truncated = len(items) - count
        items = items[:count]
        # 写 fetch_events 审计（CLI --count 路径）
        await session.execute(
            text(
                "INSERT INTO fetch_events (feed_id, event_type, ok, item_count) "
                "VALUES (:fid, 'fetch_count_limited', true, :cnt)"
            ),
            {"fid": feed["id"], "cnt": truncated},
        )
        if progress:
            progress("truncated", {"feed": feed["name"], "kept": count, "dropped": truncated})

    new_count = 0
    keyword_hits_total = 0
    for item in items:
        uh = url_hash(item.source_url)
        ch = content_hash(item.content_text)

        # 精确去重双闸：url_hash 同 / content_hash 同 → winner mention_count+1 + 审计，跳过
        # fix #32：传入原始 content_text 供空/过短短路判断
        if await apply_exact_dedup(session, feed["id"], uh, ch, content_text=item.content_text):
            continue

        cleaned = await clean_article(item.content_html or item.content_text, item.title)
        status = "unparseable" if not cleaned["is_parseable"] else "pending"

        # INSERT articles（ON CONFLICT (url_hash) DO NOTHING 保证幂等）
        await session.execute(
            text(
                "INSERT INTO articles "
                "(feed_id, source_url, url_hash, content_hash, title, "
                " author, published_at, content_text, content_md, "
                " lang, word_count, status) "
                "VALUES (:fid, :url, :uh, :ch, :title, "
                " :author, :pub, :ct, :cm, "
                " :lang, :wc, :status) "
                "ON CONFLICT (url_hash) DO NOTHING "
                "RETURNING id"
            ),
            {
                "fid": feed["id"],
                "url": item.source_url,
                "uh": uh,
                "ch": ch,
                "title": item.title,
                "author": item.author,
                "pub": item.published_at,
                "ct": cleaned["content_text"],
                "cm": cleaned["content_md"],
                "lang": cleaned["lang"],
                "wc": cleaned["word_count"],
                "status": status,
            },
        )

        # 取 article_id（RETURNING 在 ON CONFLICT DO NOTHING 下可能空，回退 SELECT）
        art_result = await session.execute(
            text("SELECT id FROM articles WHERE url_hash=:uh"),
            {"uh": uh},
        )
        art_row = art_result.first()
        if art_row:
            await update_article_tsv(
                session, art_row[0],
                title=item.title or "",
                content_text=cleaned["content_text"] or "",
            )

        if art_row and status == "pending":
            await enqueue_jobs(session, art_row[0], ["embed_core", "summarize"], ch)
            matched = await match_keywords(session, art_row[0])
            if matched:
                keyword_hits_total += len(matched)
                if progress:
                    progress("keywords", {"feed": feed["name"], "count": len(matched)})

        new_count += 1

    # 更新 feed 元数据（etag / last_modified / 状态）
    await session.execute(
        text(
            "UPDATE feeds SET etag=:etag, last_modified=:lm, "
            "last_fetched_at=now(), fetch_status='ok', fetch_failures=0 "
            "WHERE id=:fid"
        ),
        {"etag": new_etag, "lm": new_lm, "fid": feed["id"]},
    )

    if progress:
        progress("done", {"feed": feed["name"], "new": new_count})

    return new_count, truncated