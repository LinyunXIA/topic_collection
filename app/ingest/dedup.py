"""去重 — URL hash + content hash（DESIGN §6）

url_hash = sha256(canonical_url)
content_hash = sha256(cleaned_text)
LLM 之前的快速精确去重；跨源近似去重（向量 cosine）在 embed_core 后触发。
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse, urlunparse

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def canonicalize_url(url: str) -> str:
    """URL 规范化：去 fragment、去 trailing slash、lowercase scheme+host。"""
    parsed = urlparse(url)
    # 去 fragment、统一 scheme/host 小写
    canonical = parsed._replace(
        fragment="",
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
    )
    path = canonical.path.rstrip("/") or "/"
    return urlunparse(canonical._replace(path=path))


def url_hash(url: str) -> str:
    """sha256(canonical_url) — 用于 articles.url_hash 唯一索引。"""
    canonical = canonicalize_url(url)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def content_hash(text: str) -> str:
    """sha256(cleaned_text) — 用于 articles.content_hash + 版本守卫。"""
    # 归一化：去多余空白
    normalized = re.sub(r"\s+", " ", text.strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# 空内容 hash（"" 归一化后仍为 ""），用于第二闸短路（fix #32）
_EMPTY_CONTENT_HASH = hashlib.sha256("".encode("utf-8")).hexdigest()
# 第二闸最小归一化长度，小于此阈值不参与 content_hash 匹配（模板化/空摘要误合并）
_MIN_CONTENT_HASH_LENGTH = 32


def is_same_content(hash1: str, hash2: str) -> bool:
    """判断两个 content hash 是否相同。"""
    return hash1 == hash2


async def apply_exact_dedup(
    session: AsyncSession,
    feed_id: int,
    uh: str,
    ch: str,
    content_text: str | None = None,
) -> int | None:
    """精确去重双闸（DESIGN §6，fix issue #8，fix #32）。

    两道闸：
    1. ``url_hash`` 命中 → winner ``mention_count+1``
    2. ``url_hash`` 不同但 ``content_hash`` 命中（且 winner ``dedupe_of IS NULL``
       且 ``fetched_at`` 在 30d 窗口内）→ winner ``mention_count+1``

    fix #32：
    - 空/过短正文（归一化后长度 < 32 或 hash 为空串 hash）跳过第二闸，
      避免无正文 feed 的所有条目碰撞为同一篇。
    - 第二闸加 ``fetched_at > now() - INTERVAL '30 days'`` 时间窗，避免跨月模板化条目误合并。

    返回 winner article_id 表示命中（调用方应跳过 insert + enqueue），
    返回 None 表示应插入新行。

    命中时记 ``fetch_events(event_type='dedup_exact')`` 审计（item_count=1）。
    注意：``fetch_events.feed_id`` 为 NOT NULL + FK，所以必须在有 feed 上下文处调用。
    """
    # 第一道闸：url_hash
    res = await session.execute(
        text("SELECT id FROM articles WHERE url_hash=:uh"),
        {"uh": uh},
    )
    winner_id = res.scalar()
    if winner_id is not None:
        await session.execute(
            text("UPDATE articles SET mention_count=mention_count+1 WHERE id=:aid"),
            {"aid": winner_id},
        )
        await session.execute(
            text(
                "INSERT INTO fetch_events (feed_id, event_type, ok, item_count) "
                "VALUES (:fid, 'dedup_exact', true, 1)"
            ),
            {"fid": feed_id},
        )
        return winner_id

    # 第二道闸：content_hash（限定 winner.dedupe_of IS NULL 排除 loser）
    # fix #32：空/过短内容跳过第二闸，时间窗 30 天
    if ch == _EMPTY_CONTENT_HASH:
        return None
    if content_text is not None:
        normalized = re.sub(r"\s+", " ", content_text.strip())
        if len(normalized) < _MIN_CONTENT_HASH_LENGTH:
            return None
    res = await session.execute(
        text(
            "SELECT id FROM articles "
            "WHERE content_hash=:ch AND dedupe_of IS NULL "
            "AND fetched_at > now() - INTERVAL '30 days' LIMIT 1"
        ),
        {"ch": ch},
    )
    winner_id = res.scalar()
    if winner_id is not None:
        await session.execute(
            text("UPDATE articles SET mention_count=mention_count+1 WHERE id=:aid"),
            {"aid": winner_id},
        )
        await session.execute(
            text(
                "INSERT INTO fetch_events (feed_id, event_type, ok, item_count) "
                "VALUES (:fid, 'dedup_exact', true, 1)"
            ),
            {"fid": feed_id},
        )
        return winner_id

    return None
