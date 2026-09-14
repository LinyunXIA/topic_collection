from __future__ import annotations

import calendar
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import feedparser
import httpx

log = logging.getLogger(__name__)

_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime(_ISO_FMT)


def iso_utc(struct_time) -> str | None:
    if not struct_time:
        return None
    dt = datetime.fromtimestamp(calendar.timegm(struct_time), tz=UTC)
    return dt.strftime(_ISO_FMT)


def _idna_host(host: str) -> str:
    try:
        return host.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return host.lower()


def canonicalize(url: str) -> str:
    """跨源去重键：去 fragment、保留 query，host 归一（IDNA/小写）。

    netloc 重组保留 userinfo 与 IPv6 方括号，省略默认端口（http:80/https:443），
    避免非法 URL 写入卡片/归档或漏去重（#207）。归一规则变更会让 guid-less 源
    旧行 entry_key 与新 key 不一致，升级首轮可能重复推卡一次（一次性，#229）。
    """
    parts = urlsplit((url or "").strip())
    if not parts.netloc:
        return urlunsplit((parts.scheme, "", parts.path, parts.query, ""))
    try:
        host = _idna_host(parts.hostname or "")
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = parts.port
        default_port = (parts.scheme == "http" and port == 80) or (
            parts.scheme == "https" and port == 443
        )
        userinfo = parts.netloc.rsplit("@", 1)[0] + "@" if "@" in parts.netloc else ""
        netloc = f"{userinfo}{host}" + (f":{port}" if port and not default_port else "")
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


def normalize_entry(entry: Any) -> dict[str, Any]:
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


def parse_content(content: bytes) -> list[dict[str, Any]]:
    parsed = feedparser.parse(content)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        raise ValueError(f"feed 解析失败: {getattr(parsed, 'bozo_exception', '')}")
    if not parsed.entries:
        raise ValueError("feed 无条目")
    return [normalize_entry(e) for e in parsed.entries]


def fetch_feed(url: str, http_conf: Any) -> list[dict[str, Any]]:
    resp = httpx.get(
        url,
        timeout=http_conf.timeout_seconds,
        headers={"User-Agent": http_conf.user_agent},
        follow_redirects=True,
    )
    if resp.status_code // 100 != 2:
        raise ValueError(f"HTTP {resp.status_code}: {url}")
    return parse_content(resp.content)
