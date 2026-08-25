from __future__ import annotations

import json
import logging
import re
from datetime import datetime

import httpx

log = logging.getLogger(__name__)

_MAX_BODY_CHARS = 30000
_MD_SPECIAL = re.compile(r"([\\`*_\[\]()#])")


def escape_inline(text: str | None) -> str:
    text = (text or "").replace("\r", "").replace("\n", " ")
    return _MD_SPECIAL.sub(r"\\\1", text)


def local_now() -> datetime:
    return datetime.now()


def build_card(new_items: list[dict], feed_fails: int, feed_order: list[str]) -> dict:
    by_feed: dict[str, list[dict]] = {}
    for it in new_items:
        by_feed.setdefault(it["feed_id"], []).append(it)
    ordered = [name for name in feed_order if name in by_feed]
    ordered += [name for name in by_feed if name not in set(feed_order)]

    parts: list[str] = []
    for name in ordered:
        parts.append(f"**{escape_inline(name)}**")
        for it in by_feed[name]:
            display = escape_inline(it["title"]) or escape_inline(it["url"])
            parts.append(f"[{display}]({it['url']})")
            desc = (it.get("description") or "").strip()
            if desc:
                parts.append(escape_inline(desc))
        parts.append("")
    content = "\n".join(parts).rstrip("\n")

    elements = [{"tag": "div", "text": {"tag": "markdown", "content": content}}]
    if feed_fails:
        elements += [
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": f"⚠ {feed_fails} 个源失败"}},
        ]
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"Feeds 汇总  {local_now():%H:%M}",
                },
                "template": "blue",
            },
            "elements": elements,
        },
    }


def send(payload: dict, webhook_url: str, timeout_seconds: float, user_agent: str) -> bool:
    if not webhook_url:
        log.warning("跳过推送：webhook 为空")
        return False
    try:
        body = json.dumps(payload, ensure_ascii=False)
        if len(body) > _MAX_BODY_CHARS:
            log.warning("payload 超长（%d 字符），截断到 %d", len(body), _MAX_BODY_CHARS)
            body = body[:_MAX_BODY_CHARS]
        resp = httpx.post(
            webhook_url,
            content=body.encode("utf-8"),
            timeout=timeout_seconds,
            headers={"Content-Type": "application/json", "User-Agent": user_agent},
        )
        if resp.status_code != 200:
            log.warning("推送失败：HTTP %d", resp.status_code)
            return False
        data = resp.json()
        code = data.get("StatusCode", data.get("code", 0))
        if code != 0:
            log.warning("推送被飞书拒绝，业务码非 0：%s", data)
            return False
        return True
    except Exception as e:
        log.warning("推送异常: %s", e)
        return False
