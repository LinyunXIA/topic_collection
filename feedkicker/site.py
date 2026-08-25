from __future__ import annotations

import html
from datetime import datetime

from feedkicker.fetch import canonicalize


def _esc(text: str | None) -> str:
    return html.escape(text or "", quote=True)


_CSS = """body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
max-width:860px;margin:0 auto;padding:24px 16px;color:#1f2329;background:#fafafa}
h1{font-size:22px}h2{font-size:17px;margin-top:28px;border-left:4px solid #3370ff;padding-left:10px}
.meta{color:#86909c;font-size:13px;margin-bottom:20px}
article{background:#fff;border-radius:8px;padding:12px 16px;margin:10px 0;box-shadow:0 1px 2px rgba(0,0,0,.05)}
article a{color:#1f2329;text-decoration:none;font-weight:600;font-size:15px}
article a:hover{color:#3370ff}
.desc{margin-top:6px;color:#4e5969;font-size:13px;line-height:1.6;word-break:break-word}
.via{font-size:12px;color:#86909c;margin-top:4px}
footer{margin-top:36px;color:#86909c;font-size:12px;text-align:center}
nav a{color:#3370ff;text-decoration:none;font-size:13px}"""


def dedup_items(items: list[dict]) -> list[dict]:
    by_url: dict[str, dict] = {}
    order: list[str] = []
    for it in items:
        key = canonicalize(it["url"]) or it["entry_key"]
        if key in by_url:
            first = by_url[key]
            if it["feed_id"] not in first["_feeds"]:
                first["_feeds"].append(it["feed_id"])
            continue
        copy = dict(it)
        copy["_feeds"] = [it["feed_id"]]
        by_url[key] = copy
        order.append(key)
    return [by_url[k] for k in order]


def _entry_html(it: dict, feed_name: str) -> str:
    title = _esc(it["title"]) or _esc(it["url"])
    bits: list[str] = []
    if it.get("published_at"):
        bits.append(_esc(it["published_at"]))
    others = [f for f in it["_feeds"] if f != feed_name]
    if others:
        bits.append(f"亦见 {_esc(' + '.join(others))}")
    meta = f'<div class="via">{" · ".join(bits)}</div>' if bits else ""
    desc_html = ""
    d = (it.get("description") or "").strip()
    if d:
        desc_html = f'<div class="desc">{_esc(d)}</div>'
    return (
        f"<article>"
        f"<a href=\"{_esc(it['url'])}\" target=\"_blank\" rel=\"noopener\">{title}</a>"
        f"{meta}{desc_html}</article>"
    )


def render_daily(items: list[dict], day, feed_order: list[str]) -> str:
    uniq = dedup_items(items)
    by_feed: dict[str, list[dict]] = {}
    for it in uniq:
        primary = next((f for f in feed_order if f in it["_feeds"]), it["_feeds"][0])
        by_feed.setdefault(primary, []).append(it)

    ordered_names = [f for f in feed_order if f in by_feed] + [
        f for f in by_feed if f not in set(feed_order)
    ]
    sections = "".join(
        f"<h2>{_esc(name)} <small>({len(by_feed[name])})</small></h2>"
        + "".join(_entry_html(it, name) for it in by_feed[name])
        for name in ordered_names
    )
    body = sections or "<p>今日暂无条目</p>"
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Feeds 汇总 · {day.strftime('%Y-%m-%d')}</title><style>{_CSS}</style></head>
<body>
<h1>Feeds 汇总 · {day.strftime('%Y年%m月%d日')}</h1>
<div class="meta">去重后 {len(uniq)} 条 / 原始 {len(items)} 条</div>
<nav><a href="../index.html">← 归档目录</a></nav>
{body}
<footer>feedkicker v0.2 · generated {datetime.now():%Y-%m-%d %H:%M:%S}</footer>
</body></html>"""


def render_index(archives: list[dict]) -> str:
    rows = "".join(
        f"<article><a href=\"{_esc(a['path'])}\">{_esc(a['date'])}</a>"
        f"<span class='via'> · {a['count']} 条</span></article>"
        for a in sorted(archives, key=lambda x: x["date"], reverse=True)
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Feeds 汇总 · 归档</title><style>{_CSS}</style></head>
<body>
<h1>Feeds 汇总 · 每日归档</h1>
{rows or "<p>暂无归档</p>"}
<footer>feedkicker v0.2</footer>
</body></html>"""
