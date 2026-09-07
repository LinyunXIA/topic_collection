"""飞书互动卡片构建：markdown 转义、payload 组装、超长截断、action 剥离。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

_MAX_BODY_BYTES = 20000
_MD_SPECIAL = re.compile(r"([\\`*_\[\]()#])")


_CARD_TZ = ZoneInfo("Asia/Shanghai")


def escape_inline(text: str | None) -> str:
    text = (text or "").replace("\r", "").replace("\n", " ")
    return _MD_SPECIAL.sub(r"\\\1", text)


def local_now() -> datetime:
    return datetime.now(_CARD_TZ)


def _assemble(
    content: str,
    feed_fails: int,
    dropped: int,
    total: int = 0,
    detail_url: str | None = None,
    detail_label: str = "📰 详情见在线表格",
    wiki_urls: list[str] | None = None,
    wiki_label: str = "📖 查看大纲",
) -> dict[str, Any]:
    wiki_urls = [u for u in (wiki_urls or []) if u]
    if not (content or "").strip() and wiki_urls:
        content = f"📖 已生成 {len(wiki_urls)} 份大纲"
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": content}}
    ]
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
            },
            {"tag": "div", "text": {"tag": "lark_md", "content": f"[{detail_label}]({detail_url})"}},
        ]
    if wiki_urls:
        for idx, url in enumerate(wiki_urls, 1):
            label = f"{wiki_label} {idx}" if len(wiki_urls) > 1 else wiki_label
            btn = {"tag": "button", "text": {"tag": "plain_text", "content": label},
                   "url": url, "type": "default"}
            elements += [
                {"tag": "action", "actions": [btn]},
                {"tag": "div", "text": {"tag": "lark_md", "content": f"[{label}]({url})"}},
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


def strip_actions(payload: dict[str, Any]) -> dict[str, Any]:
    card = json.loads(json.dumps(payload, ensure_ascii=False))
    card["card"]["elements"] = [
        el for el in card.get("card", {}).get("elements") or [] if el.get("tag") != "action"
    ]
    return card


def build_card(
    new_items: list[dict[str, Any]],
    feed_fails: int,
    feed_order: list[str],
    top_n: int = 0,
    detail_url: str | None = None,
    max_bytes: int = _MAX_BODY_BYTES,
    detail_label: str = "📰 详情见在线表格",
    wiki_urls: list[str] | None = None,
    wiki_label: str = "📖 查看大纲",
) -> dict[str, Any]:
    by_feed: dict[str, list[dict[str, Any]]] = {}
    for it in new_items:
        by_feed.setdefault(it["feed_id"], []).append(it)
    ordered = [name for name in feed_order if name in by_feed]
    ordered += [name for name in by_feed if name not in set(feed_order)]

    selected: list[tuple[str, dict[str, Any]]] = []
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

        def hidden_note(feed_name: str | None) -> None:
            if feed_name is None:
                return
            h = hidden_by_feed.get(feed_name, 0)
            if h and any(n == feed_name for n, _ in selected):
                parts.append(escape_inline(f"… 还有 {h} 条，详情见在线表格"))

        def close_prev() -> None:
            if prev is not None:
                hidden_note(prev)
                parts.append("")

        for name, it in selected:
            if name != prev:
                close_prev()
                parts.append(f"**{escape_inline(name)}**")
                prev = name
            display = escape_inline(it["title"]) or escape_inline(it["url"])
            parts.append(f"[{display}]({it['url'].replace(')', '%29')})")
            if show_desc:
                desc = (it.get("description") or "").strip()
                if desc:
                    parts.append(escape_inline(desc))
        if selected:
            hidden_note(prev)
        return "\n".join(parts).rstrip("\n")

    total = sum(len(v) for v in by_feed.values())

    def fits(show_desc: bool, dropped: int) -> bool:
        body = json.dumps(
            _assemble(
                render(show_desc),
                feed_fails,
                dropped,
                total,
                detail_url,
                detail_label,
                wiki_urls,
                wiki_label,
            ),
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

    return _assemble(
        render(show_desc),
        feed_fails,
        dropped,
        total,
        detail_url,
        detail_label,
        wiki_urls,
        wiki_label,
    )
