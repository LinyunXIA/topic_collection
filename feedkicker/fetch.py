from __future__ import annotations

import calendar
import logging
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import feedparser
import httpx

log = logging.getLogger(__name__)

_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime(_ISO_FMT)


def iso_utc(struct_time) -> str | None:
    if not struct_time:
        return None
    dt = datetime.fromtimestamp(calendar.timegm(struct_time), tz=timezone.utc)
    return dt.strftime(_ISO_FMT)


def canonicalize(url: str) -> str:
    parts = urlsplit((url or "").strip())
    if not parts.netloc:
        return urlunsplit((parts.scheme, "", parts.path, parts.query, ""))
    try:
        host = (parts.hostname or "").lower()
        netloc = f"{host}:{parts.port}" if parts.port else host
    except ValueError:
        netloc = parts.netloc.lower()
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


def entry_key_of(entry) -> str:
    guid = (entry.get("id") or "").strip()
    if guid:
        return guid
    link = canonicalize(entry.get("link") or "")
    if link:
        return link
    return "title:" + " ".join((entry.get("title") or "").strip().lower().split())


def normalize_entry(entry) -> dict:
    title = (entry.get("title") or "").strip()
    url = canonicalize(entry.get("link") or "")
    description = entry.get("summary") or entry.get("description") or ""
    return {
        "entry_key": entry_key_of(entry),
        "title": title,
        "url": url,
        "description": description,
        "published_at": iso_utc(entry.get("published_parsed")),
    }


def parse_content(content: bytes) -> list[dict]:
    parsed = feedparser.parse(content)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        raise ValueError(f"feed 解析失败: {getattr(parsed, 'bozo_exception', '')}")
    if not parsed.entries:
        raise ValueError("feed 无条目")
    return [normalize_entry(e) for e in parsed.entries]


def fetch_feed(url: str, http_conf) -> list[dict]:
    resp = httpx.get(
        url,
        timeout=http_conf.timeout_seconds,
        headers={"User-Agent": http_conf.user_agent},
        follow_redirects=True,
    )
    if resp.status_code // 100 != 2:
        raise ValueError(f"HTTP {resp.status_code}: {url}")
    return parse_content(resp.content)
