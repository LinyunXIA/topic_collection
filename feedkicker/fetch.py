from __future__ import annotations

import calendar
import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import httpx

log = logging.getLogger(__name__)

_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"

_TRACKING = {"spm", "from", "fbclid", "gclid", "ref", "ref_src", "source", "mc_cid", "mc_eid"}

_BARE_HOST_RE = re.compile(r"^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?:[/:?#]|$)")


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
    避免非法 URL 写入卡片/归档或漏去重（#207）。urlsplit 遇非法 IPv6 等 ValueError 时
    回退原文（不得抛出，否则单条坏链令整卡/整源崩溃，#328）。归一规则变更会让 guid-less 源
    旧行 entry_key 与新 key 不一致，升级首轮可能重复推卡一次（一次性，#229）。
    """
    text = (url or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
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


def is_url_token(token: str) -> bool:
    """token 是否含 `://` 或形如裸域名（example.com/path）——非 URL 文本不得进入去重键（#329）。"""
    text = (token or "").strip()
    return "://" in text or bool(_BARE_HOST_RE.match(text))


def dedup_key(url: str) -> str:
    """推送/归档/提炼共享去重键（#325）：`canonicalize` 后再剥 tracking 参数并排序 query。

    三处统一复用（feishu_card 跨源去重、extract_write.link_keys、bitable_records 链接比对），
    避免「同文不同 utm」在归档侧合并、在卡片侧重复渲染（#289 未闭环）。非 URL token 返回 ""
    （调用方须过滤空键，#329）；尾斜杠/HTML 实体等归一细化见 #332/#333（P3）。
    """
    text = (url or "").strip()
    if not is_url_token(text):
        return ""
    canon = canonicalize(text)
    if not canon:
        return ""
    try:
        parts = urlsplit(canon)
    except ValueError:
        return canon
    if not parts.query:
        return canon
    try:
        pairs = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        return canon
    kept = sorted(
        (k, v) for k, v in pairs if not k.lower().startswith("utm_") and k.lower() not in _TRACKING
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


def entry_key_of(entry) -> str:
    guid = (entry.get("id") or "").strip()
    if guid:
        return guid
    link = canonicalize(entry.get("link") or "")
    if link:
        return link
    return "title:" + " ".join((entry.get("title") or "").strip().lower().split())


def normalize_entry(entry: Any) -> dict[str, Any]:
    """条目归一；link 非空但 urlsplit 失败（如非法 IPv6）时 raise，交由 parse_content 逐条隔离（#328）。"""
    title = (entry.get("title") or "").strip()
    raw_link = (entry.get("link") or "").strip()
    if raw_link:
        try:
            urlsplit(raw_link)
        except ValueError as e:
            raise ValueError(f"非法链接: {raw_link}") from e
    description = entry.get("summary") or entry.get("description") or ""
    return {
        "entry_key": entry_key_of(entry),
        "title": title,
        "url": canonicalize(raw_link),
        "description": description,
        "published_at": iso_utc(entry.get("published_parsed")),
    }


def parse_content(content: bytes) -> list[dict[str, Any]]:
    """逐条隔离坏条目：单条畸形 link 只跳过该条并 WARNING，不得丢弃整源（#328）。"""
    parsed = feedparser.parse(content)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        raise ValueError(f"feed 解析失败: {getattr(parsed, 'bozo_exception', '')}")
    if not parsed.entries:
        raise ValueError("feed 无条目")
    entries: list[dict[str, Any]] = []
    skipped = 0
    for entry in parsed.entries:
        try:
            entries.append(normalize_entry(entry))
        except Exception as exc:  # noqa: BLE001
            skipped += 1
            log.warning("跳过畸形 feed 条目: %s", exc)
    if skipped:
        log.warning("feed 跳过 %d 条畸形条目，保留 %d 条", skipped, len(entries))
    if not entries:
        raise ValueError(f"feed 条目全部畸形（{skipped} 条）")
    return entries


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
