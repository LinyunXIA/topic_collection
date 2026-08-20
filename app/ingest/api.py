"""配置化 API 连接器 — Phase 2 切片 2.7（DESIGN §10.2）

fetch_api(feed) -> list[FeedItem]：读取 feeds.config_json 含 {endpoint, method, params, headers, rate_limit_per_hour, items_path, mapper} → httpx → jmespath → FeedItem
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

try:
    import jmespath
except ImportError:
    jmespath = None

from app.ingest.base import FeedItem

logger = logging.getLogger(__name__)


def _map_to_feed_item(doc: dict, mapper: dict, lang: str = "en") -> FeedItem:
    """按 mapper 将 doc 映射为 FeedItem"""

    def _search(path: str | list, d: dict):
        if jmespath is None:
            # 回退：简单点号取数
            cur = d
            for part in str(path).split("."):
                if isinstance(cur, dict):
                    cur = cur.get(part, "")
                else:
                    return ""
            return cur or ""
        if isinstance(path, list):
            # [field_for_text, fallback_url] 形式
            for p in path:
                v = jmespath.search(p, d)
                if v:
                    return v
            return ""
        return jmespath.search(path, d) or ""

    title = _search(mapper.get("title", "title"), doc) or "(no title)"
    url = _search(mapper.get("url", "url"), doc) or ""
    author = _search(mapper.get("author", "by"), doc) or ""
    time_raw = _search(mapper.get("time", "time"), doc)
    content = _search(mapper.get("content", "content"), doc) or ""

    # 时间解析
    published_at = None
    try:
        if isinstance(time_raw, (int, float)):
            published_at = datetime.fromtimestamp(float(time_raw), tz=timezone.utc)
        elif isinstance(time_raw, str) and time_raw:
            # 尝试 ISO
            published_at = datetime.fromisoformat(time_raw.replace("Z", "+00:00"))
    except Exception:
        published_at = None

    # source_url 用 mapper 提取的稳定字段，无则回退 content 的 url 或 title
    source_url = url or _search("id", doc) or _search("html_url", doc) or title

    return FeedItem(
        feed_id=0,
        source_url=str(source_url),
        title=str(title),
        content_text=str(content),
        content_html=None,
        author=str(author),
        published_at=published_at,
        etag=None,
        last_modified=None,
    )


async def fetch_api(feed: dict | Any) -> list[FeedItem]:
    """配置化 API 抓取（DESIGN §10.2）"""
    # feed 可能是 dict row 或 Feed ORM，统一取 config_json
    if isinstance(feed, dict):
        cfg = feed.get("config_json") or {}
        feed_id = feed.get("id", 0)
    else:
        cfg = getattr(feed, "config_json", {}) or {}
        feed_id = getattr(feed, "id", 0)

    if not cfg or not cfg.get("endpoint"):
        logger.warning("fetch_api: feed %s 无 endpoint 配置，跳过", feed_id)
        return []

    endpoint = cfg["endpoint"]
    method = cfg.get("method", "GET")
    params = cfg.get("params", {})
    headers = cfg.get("headers", {})
    items_path = cfg.get("items_path", "$")
    mapper = cfg.get("mapper", {})
    lang = cfg.get("language_hint", "en")
    id_to_detail = cfg.get("id_to_detail")

    # 速率限制：本机简单 sleep，已有 per_host 限速在外层
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        resp = await client.request(method, endpoint, params=params)
        resp.raise_for_status()
        data = resp.json()
        if jmespath:
            ids = jmespath.search(items_path, data) or []
        else:
            ids = data if isinstance(data, list) else []

        # 若有 id_to_detail，需二次拉取详情
        if id_to_detail and isinstance(ids, list) and ids and isinstance(ids[0], (str, int)):
            results: list[FeedItem] = []
            max_items = cfg.get("max_items_per_fetch", 50)
            for id_ in ids[:max_items]:
                detail_url = id_to_detail["endpoint_template"].format(id=id_)
                detail_method = id_to_detail.get("method", "GET")
                try:
                    detail = await client.request(detail_method, detail_url)
                    detail.raise_for_status()
                    doc = detail.json()
                    item = _map_to_feed_item(doc, mapper, lang)
                    item.feed_id = feed_id
                    results.append(item)
                except Exception as e:
                    logger.warning("fetch_api detail %s 失败: %s", id_, e)
                    continue
            return results
        # 否则直接映射
        if isinstance(ids, dict):
            # jmespath 返回单对象而非列表
            ids = [ids]
        if not isinstance(ids, list):
            ids = [data]
        results = []
        for doc in ids:
            if not isinstance(doc, dict):
                continue
            item = _map_to_feed_item(doc, mapper, lang)
            item.feed_id = feed_id
            results.append(item)
        return results
