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


def _assemble(
    content: str,
    feed_fails: int,
    dropped: int,
    total: int = 0,
    detail_url: str | None = None,
    detail_label: str = "📰 详情见在线表格",
) -> dict:
    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": content}}]
    if feed_fails:
        elements += [
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": f"⚠ {feed_fails} 个源失败"}},
        ]
    if detail_url and total:
        elements += [
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": detail_label},
                        "url": detail_url,
                        "type": "default",
                    }
                ],
            }
        ]
        elements += [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"[{detail_label}]({detail_url})"}}
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


def strip_actions(payload: dict) -> dict:
    card = json.loads(json.dumps(payload, ensure_ascii=False))
    card["card"]["elements"] = [
        el for el in card.get("card", {}).get("elements") or []
        if el.get("tag") != "action"
    ]
    return card


def build_card(
    new_items: list[dict],
    feed_fails: int,
    feed_order: list[str],
    top_n: int = 0,
    detail_url: str | None = None,
    max_bytes: int = _MAX_BODY_BYTES,
    detail_label: str = "📰 详情见在线表格",
) -> dict:
    by_feed: dict[str, list[dict]] = {}
    for it in new_items:
        by_feed.setdefault(it["feed_id"], []).append(it)
    ordered = [name for name in feed_order if name in by_feed]
    ordered += [name for name in by_feed if name not in set(feed_order)]

    selected: list[tuple[str, dict]] = []
    hidden_by_feed: dict[str, int] = {}
    for name in ordered:
        items = by_feed[name]
        keep = items if top_n <= 0 else items[:top_n]
        selected.extend((name, it) for it in keep)
        hidden = len(items) - len(keep)
        if hidden > 0:
            hidden_by_feed[name] = hidden

    def render(show_desc: bool) -> str:
        parts: list[str] = []
        prev: str | None = None

        def close_prev():
            if prev is None:
                return
            h = hidden_by_feed.get(prev, 0)
            if h and any(n == prev for n, _ in selected):
                parts.append(escape_inline(f"… 还有 {h} 条，详情见在线表格"))
            parts.append("")

        for name, it in selected:
            if name != prev:
                close_prev()
                parts.append(f"**{escape_inline(name)}**")
                prev = name
            display = escape_inline(it["title"]) or escape_inline(it["url"])
            parts.append(f"[{display}]({it['url']})")
            if show_desc:
                desc = (it.get("description") or "").strip()
                if desc:
                    parts.append(escape_inline(desc))
        if selected:
            h = hidden_by_feed.get(prev, 0)
            if h:
                parts.append(escape_inline(f"… 还有 {h} 条，详情见在线表格"))
        return "\n".join(parts).rstrip("\n")

    total = sum(len(v) for v in by_feed.values())

    def fits(show_desc: bool, dropped: int) -> bool:
        body = json.dumps(
            _assemble(render(show_desc), feed_fails, dropped, total, detail_url, detail_label),
            ensure_ascii=False,
        )
        return len(body.encode("utf-8")) <= max_bytes

    show_desc = True
    dropped = 0
    while not fits(show_desc, dropped):
        if show_desc:
            show_desc = False
            continue
        if not selected:
            break
        selected.pop()
        dropped += 1

    return _assemble(render(show_desc), feed_fails, dropped, total, detail_url, detail_label)


def _post(
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


def send(
    payload: dict,
    webhook_url: str,
    timeout_seconds: float,
    user_agent: str,
    secret: str = "",
) -> bool:
    return _post(payload, webhook_url, timeout_seconds, user_agent, secret)


def send_text(
    text_msg: str,
    webhook_url: str,
    timeout_seconds: float,
    user_agent: str,
    secret: str = "",
) -> bool:
    payload = {"msg_type": "text", "content": {"text": text_msg}}
    return _post(payload, webhook_url, timeout_seconds, user_agent, secret)
