"""Ingest 数据类 — DESIGN §3/§6

FeedItem: RSS/API 抓取后的标准化条目。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class FeedItem:
    """抓取后标准化的文章条目。"""
    feed_id: int
    source_url: str
    title: str
    content_text: str = ""           # 纯文本正文
    content_html: str = ""           # 原始 HTML（按需保留）
    author: str | None = None
    published_at: datetime | None = None
    etag: str | None = None
    last_modified: str | None = None
