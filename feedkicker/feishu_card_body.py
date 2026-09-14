"""卡片 body 元素组装（自 feishu_card 抽出，保持 feishu_card.py ≤200 行，DESIGN §21.2）。

超限裁剪顺序（#222）：先剥 description、再从 selected 头部丢最旧条目、
最后从 wiki_urls 尾部丢链接并在卡片提示截断数。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

_CARD_TZ = ZoneInfo("Asia/Shanghai")


def local_now() -> datetime:
    return datetime.now(_CARD_TZ)


def _assemble(
    content: str,
    feed_fails: int,
    dropped: int,
    total: int = 0,
    detail_url: str | None = None,
    detail_label: str = "📰 详情见多维表格",
    wiki_urls: list[str] | None = None,
    wiki_label: str = "📖 查看大纲",
    wiki_dropped: int = 0,
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
    if wiki_dropped:
        elements += [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"… 已截断 {wiki_dropped} 个大纲链接"}}
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
