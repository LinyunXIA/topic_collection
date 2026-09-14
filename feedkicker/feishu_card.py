"""飞书互动卡片构建：markdown 转义、payload 组装、超长截断、action 剥离。"""

from __future__ import annotations

import json
import re
from typing import Any

from feedkicker.feishu_card_body import _assemble

_MAX_BODY_BYTES = 20000
SIGN_RESERVE_BYTES = 128
_MD_SPECIAL = re.compile(r"([\\`*_\[\]()#])")


def escape_inline(text: str | None) -> str:
    text = (text or "").replace("\r", "").replace("\n", " ")
    return _MD_SPECIAL.sub(r"\\\1", text)


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
    max_bytes: int = _MAX_BODY_BYTES - SIGN_RESERVE_BYTES,
    detail_label: str = "📰 详情见多维表格",
    wiki_urls: list[str] | None = None,
    wiki_label: str = "📖 查看大纲",
) -> dict[str, Any]:
    """每源保最新 `top_n` 条（时效键=published_at/first_seen），`selected` 最旧在前（#200）。

    超限裁剪（#222）：剥 description → 按拼接序 pop(0) 丢最旧（跨源近似）→ 丢 wiki_urls 尾部并提示。
    """
    def time_key(item: dict[str, Any]) -> str:
        return item.get("published_at") or item.get("first_seen") or ""

    by_feed: dict[str, list[dict[str, Any]]] = {}
    for it in new_items:
        by_feed.setdefault(it["feed_id"], []).append(it)
    ordered = [name for name in feed_order if name in by_feed]
    ordered += [name for name in by_feed if name not in set(feed_order)]

    selected: list[tuple[str, dict[str, Any]]] = []
    hidden_by_feed: dict[str, int] = {}
    for name in ordered:
        items = by_feed[name]
        newest = sorted(items, key=time_key, reverse=True)[:top_n] if top_n > 0 else items
        keep = sorted(newest, key=time_key) if top_n > 0 else newest
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
                parts.append(escape_inline(f"… 还有 {h} 条，详情见多维表格"))

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

    def fits(show_desc: bool, dropped: int, wiki_dropped: int) -> bool:
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
                wiki_dropped,
            ),
            ensure_ascii=False,
        )
        return len(body.encode("utf-8")) <= max_bytes

    show_desc = True
    dropped = 0
    wiki_dropped = 0
    while not fits(show_desc, dropped, wiki_dropped):
        if show_desc:
            show_desc = False
            continue
        if selected:
            selected.pop(0)
            dropped += 1
            continue
        if wiki_urls:
            wiki_urls = wiki_urls[:-1]
            wiki_dropped += 1
            continue
        break

    return _assemble(
        render(show_desc),
        feed_fails,
        dropped,
        total,
        detail_url,
        detail_label,
        wiki_urls,
        wiki_label,
        wiki_dropped,
    )
