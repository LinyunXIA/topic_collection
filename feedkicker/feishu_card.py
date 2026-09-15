"""飞书互动卡片构建：markdown 转义、payload 组装、超长截断、action 剥离。"""

from __future__ import annotations

import json
import re
from typing import Any

from feedkicker.feishu_card_body import _assemble
from feedkicker.fetch import dedup_key

_MAX_BODY_BYTES = 20000
SIGN_RESERVE_BYTES = 128
_MD_SPECIAL = re.compile(r"([\\`*_\[\]()#])")
_AMP_ENTITY = re.compile(r"&(?=[#\w]+;)")


def _dedup_by_url_key(
    new_items: list[dict[str, Any]], feed_order: list[str]
) -> list[dict[str, Any]]:
    """跨源去重：同 `dedup_key(url)` 合并为一条，主归属取 `feed_order` 最靠前的源（#289/#325）。

    与归档/提炼侧共用同一去重键（含 tracking 剥离），否则「同文不同 utm」在归档侧合并、
    卡片侧重复渲染。其余同 URL 条目的来源名并入主条目 `also_seen`（渲染为「亦见 X + Y」）；
    空 URL/非 URL 不参与合并以免误并不同条目。去重在 top_n 截断与 `_assemble` 之前，计数按
    去重后口径。
    """
    rank = {name: i for i, name in enumerate(feed_order)}
    groups: dict[str, list[int]] = {}
    for idx, it in enumerate(new_items):
        key = dedup_key(str(it.get("url") or ""))
        groups.setdefault(key or f"\x00idx{idx}", []).append(idx)

    kept: list[dict[str, Any]] = []
    for idxs in groups.values():
        primary = min(idxs, key=lambda i: (rank.get(new_items[i]["feed_id"], len(rank)), i))
        item = dict(new_items[primary])
        others: list[str] = []
        for i in idxs:
            name = new_items[i]["feed_id"]
            if name != item["feed_id"] and name not in others:
                others.append(name)
        if others:
            item["also_seen"] = others
        kept.append(item)
    return kept


def escape_inline(text: str | None) -> str:
    """转义 markdown 元字符并实体化尖括号，防 lark_md 标签/`@all` 注入（#337/#R10-06）。

    仅把「实体样式」的 `&`（`&lt;`/`&#60;`，即 `&` 后紧跟 `[#\\w]+;`）转义为 `&amp;`，
    普通 `&`（`AI & 医疗`）保持原样——既拦 `&lt;at …&gt;` 绕过，又不污染用户可见文本。
    """
    text = (text or "").replace("\r", "").replace("\n", " ")
    text = _AMP_ENTITY.sub("&amp;", text).replace("<", "&lt;").replace(">", "&gt;")
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

    同 `dedup_key(url)` 跨源去重（#289/#325）先于 top_n 截断；超限裁剪（#222/#233）：
    剥 description → 丢全局 time_key 最小（最旧）者 → 丢 wiki_urls 尾部并提示。
    """
    def time_key(item: dict[str, Any]) -> str:
        return item.get("published_at") or item.get("first_seen") or ""

    deduped = _dedup_by_url_key(new_items, feed_order)
    by_feed: dict[str, list[dict[str, Any]]] = {}
    for it in deduped:
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
                note = f"… 还有 {h} 条" + ("，详情见多维表格" if detail_url else "")
                parts.append(escape_inline(note))

        def close_prev() -> None:
            if prev is not None:
                hidden_note(prev)
                parts.append("")

        for name, it in selected:
            if name != prev:
                close_prev()
                parts.append(f"**{escape_inline(name)}**")
                prev = name
            url = str(it.get("url") or "")
            display = escape_inline(it.get("title")) or escape_inline(url)
            if url:
                target = url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
                parts.append(f"[{display}]({target})")
            elif display:
                parts.append(display)
            also = it.get("also_seen") or []
            if also:
                parts.append(escape_inline(f"亦见 {' + '.join(also)}"))
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
            idx = min(range(len(selected)), key=lambda i: time_key(selected[i][1]))
            selected.pop(idx)
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
