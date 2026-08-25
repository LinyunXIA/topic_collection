from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time
from datetime import datetime

import httpx

log = logging.getLogger(__name__)

_MAX_BODY_BYTES = 20000
_MD_SPECIAL = re.compile(r"([\\`*_\[\]()#])")


def escape_inline(text: str | None) -> str:
    text = (text or "").replace("\r", "").replace("\n", " ")
    return _MD_SPECIAL.sub(r"\\\1", text)


def local_now() -> datetime:
    return datetime.now()


def gen_sign(timestamp: str, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def _assemble(content: str, feed_fails: int, dropped: int) -> dict:
    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": content}}]
    if feed_fails:
        elements += [
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": f"⚠ {feed_fails} 个源失败"}},
        ]
    if dropped:
        elements += [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"… 已截断 {dropped} 条旧条目"}}
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


def build_card(new_items: list[dict], feed_fails: int, feed_order: list[str], max_bytes: int = _MAX_BODY_BYTES) -> dict:
    by_feed: dict[str, list[dict]] = {}
    for it in new_items:
        by_feed.setdefault(it["feed_id"], []).append(it)
    ordered = [name for name in feed_order if name in by_feed]
    ordered += [name for name in by_feed if name not in set(feed_order)]
    selected = [it for name in ordered for it in by_feed[name]]

    def render(items: list[dict], with_desc: bool) -> str:
        parts: list[str] = []
        prev_feed: str | None = None
        for it in items:
            if it["feed_id"] != prev_feed:
                if prev_feed is not None:
                    parts.append("")
                parts.append(f"**{escape_inline(it['feed_id'])}**")
                prev_feed = it["feed_id"]
            display = escape_inline(it["title"]) or escape_inline(it["url"])
            parts.append(f"[{display}]({it['url']})")
            if with_desc:
                desc = (it.get("description") or "").strip()
                if desc:
                    parts.append(escape_inline(desc))
        return "\n".join(parts)

    def fits(items: list[dict], with_desc: bool, dropped: int) -> bool:
        body = json.dumps(
            _assemble(render(items, with_desc), feed_fails, dropped),
            ensure_ascii=False,
        )
        return len(body.encode("utf-8")) <= max_bytes

    with_desc = True
    dropped = 0
    while not fits(selected, with_desc, dropped):
        if with_desc:
            with_desc = False
            continue
        if not selected:
            break
        selected.pop()
        dropped += 1

    return _assemble(render(selected, with_desc), feed_fails, dropped)


def send(
    payload: dict,
    webhook_url: str,
    timeout_seconds: float,
    user_agent: str,
    secret: str = "",
) -> bool:
    if not webhook_url:
        log.warning("跳过推送：webhook 为空")
        return False
    try:
        body_payload = dict(payload)
        if secret:
            ts = str(int(time.time()))
            body_payload["timestamp"] = ts
            body_payload["sign"] = gen_sign(ts, secret)
        body = json.dumps(body_payload, ensure_ascii=False)
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
