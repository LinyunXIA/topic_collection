"""RSS/Atom 抓取 — DESIGN §6/§10

feedparser 解析 + ETag/304 条件请求 + 每域限速 + 全局并发控制。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser
import httpx

from app.config import Settings
from app.ingest.base import FeedItem

logger = logging.getLogger(__name__)


class FeedFetcher:
    """RSS/Atom 抓取器：ETag/304 + 每域限速。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._semaphore = asyncio.Semaphore(settings.ingestion.global_concurrency)
        self._host_last_request: dict[str, float] = {}
        self._per_host_interval = settings.ingestion.per_host_interval_ms / 1000.0

    async def _rate_limit(self, host: str) -> None:
        """每域限速：同一 host 上一次抓取结束到下一次起手的最小间隔。"""
        import time

        now = time.monotonic()
        last = self._host_last_request.get(host, 0)
        wait = self._per_host_interval - (now - last)
        if wait > 0:
            await asyncio.sleep(wait)
        self._host_last_request[host] = time.monotonic()

    async def fetch_feed(
        self,
        feed_id: int,
        url: str,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> tuple[list[FeedItem], str | None, str | None]:
        """抓取单个 feed，返回 (items, new_etag, new_last_modified)。

        使用 ETag/304 条件请求，减少带宽。
        """
        from urllib.parse import urlparse

        host = urlparse(url).hostname or url
        headers: dict[str, str] = {
            "User-Agent": self.settings.ingestion.user_agent,
        }
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        async with self._semaphore:
            await self._rate_limit(host)
            try:
                async with httpx.AsyncClient(
                    timeout=30,
                    follow_redirects=True,
                ) as client:
                    resp = await client.get(url, headers=headers)

                    if resp.status_code == 304:
                        logger.info("304 Not Modified: %s", url)
                        return [], etag, last_modified

                    resp.raise_for_status()

            except httpx.HTTPStatusError as e:
                logger.error("HTTP %d 抓取失败: %s", e.response.status_code, url)
                raise
            except Exception as e:
                logger.error("抓取失败: %s — %s", url, e)
                raise

        new_etag = resp.headers.get("etag", etag)
        new_last_modified = resp.headers.get("last-modified", last_modified)

        # 解析 feed
        feed = feedparser.parse(resp.text)
        items: list[FeedItem] = []
        for entry in feed.entries:
            item = self._parse_entry(feed_id, entry)
            if item:
                items.append(item)

        logger.info("抓取 %s: %d 条", url, len(items))
        return items, new_etag, new_last_modified

    def _parse_entry(self, feed_id: int, entry: Any) -> FeedItem | None:
        """将 feedparser entry 转为 FeedItem。"""
        link = entry.get("link", "")
        title = entry.get("title", "")
        if not link or not title:
            return None

        # 提取纯文本内容
        content_text = ""
        content_html = ""
        if hasattr(entry, "content") and entry.content:
            for c in entry.content:
                if c.get("type", "").startswith("text/html"):
                    content_html = c.get("value", "")
                else:
                    content_text = c.get("value", "")
        if not content_text and hasattr(entry, "summary"):
            content_text = entry.get("summary", "")

        # 去除 HTML 标签（简单处理，后续 cleaner 会做完整清洗）
        if content_text and "<" in content_text:
            import re
            content_text = re.sub(r"<[^>]+>", "", content_text)

        # 解析发布时间
        published_at = None
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try:
                published_at = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass

        return FeedItem(
            feed_id=feed_id,
            source_url=link,
            title=title.strip(),
            content_text=content_text.strip(),
            content_html=content_html,
            author=entry.get("author"),
            published_at=published_at,
        )
