#!/usr/bin/env python3
"""抓取并查看 RSS/Atom 源的原始返回格式 —— Phase 2+ 排错小工具

用途：用户想看某个 feed 源到底返回了什么（状态码 / Content-Type / 原始 XML 字段），
用来排「字段名对不上 / 解析失败 / 抓不到」这类问题。

用法（在项目根目录，用项目 venv 运行）：
    .venv/bin/python scripts/customization/fetch_rss_raw.py https://hnrss.org/frontpage
    .venv/bin/python scripts/customization/fetch_rss_raw.py <url> --parse
    .venv/bin/python scripts/customization/fetch_rss_raw.py <url> --bytes 2000 --parse --limit 3

外部规则：
  - 走共享出口 app.core.egress.safe_get(is_feed=True)（PRD §12 / #78 白名单）。
  - 任意非白名单域名抓取需设置环境变量 FEED_FETCH_ALLOW_ALL=1，否则被拦截并给出提示。
"""

from __future__ import annotations

import argparse
import asyncio
import sys


async def run(url: str, raw_bytes: int, do_parse: bool, limit: int, timeout: float) -> int:
    from app.core.egress import safe_get

    print(f"请求 URL : {url}")

    try:
        resp = await safe_get(url, timeout=timeout, is_feed=True, follow_redirects=True)
    except Exception as e:
        # egress 白名单拦截（含 FEED_FETCH_ALLOW_ALL 未开启）
        msg = str(e)
        if "白名单" in msg or "not in whitelist" in msg or "不在白名单" in msg:
            print("❌ 被共享出口白名单拦截：", msg, file=sys.stderr)
            print("   → 非白名单 RSS 域名抓取需先设置：FEED_FETCH_ALLOW_ALL=1", file=sys.stderr)
            print("     （或在根目录 security/web_site_list.yaml 对应环境段加入该域名）", file=sys.stderr)
            return 1
        print("❌ 请求失败：", msg, file=sys.stderr)
        return 1

    status = resp.status_code
    ctype = resp.headers.get("content-type", "")
    final_url = str(resp.url)
    body = resp.content
    print(f"最终 URL : {final_url}")
    print(f"HTTP     : {status} {resp.reason_phrase}")
    print(f"类型     : {ctype}")
    print(f"字节数   : {len(body)}")

    if status != 200:
        print(f"⚠️  非 200 响应（{status}）。原始响应体（前 {raw_bytes} 字节）：", file=sys.stderr)
        print(body[:raw_bytes].decode("utf-8", errors="replace"), file=sys.stderr)
        return 1
    if not body:
        print("⚠️  响应体为空。")
        return 1

    # 原始返回格式（重点）
    print(f"\n──── 原始返回（前 {raw_bytes} 字节）────")
    print(body[:raw_bytes].decode("utf-8", errors="replace"))

    # 结构化解析（可选）
    if do_parse:
        import feedparser

        text = body.decode("utf-8", errors="replace")
        d = feedparser.parse(text)
        print("\n──── 结构化解析（feedparser）────")
        print(f"解析错误(bozo): {d.bozo}")
        print(f"feed 类型      : {d.get('version')}")
        print(f"feed 标题      : {d.feed.get('title')}")
        print(f"feed 链接      : {d.feed.get('link')}")
        print(f"条目总数       : {len(d.entries)}")
        print(f"feed 字段      : {sorted(d.feed.keys())}")
        for i, e in enumerate(d.entries[:limit]):
            print(f"\n[{i}] {e.get('title')}")
            print(f"    字段: {sorted(e.keys())}")
            for k in ("title", "link", "published", "published_parsed", "summary", "id", "guid", "author"):
                if k in e:
                    print(f"    {k}: {str(e[k])[:120]}")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="抓取并查看 RSS/Atom 源的原始返回格式（Phase 2+ 排错工具）"
    )
    parser.add_argument("url", help="feed URL")
    parser.add_argument("--bytes", type=int, default=4000, help="打印原始体前 N 字节（默认 4000）")
    parser.add_argument("--parse", action="store_true", help="额外用 feedparser 解析并展示字段结构")
    parser.add_argument("--limit", type=int, default=2, help="--parse 时检查前 N 条（默认 2）")
    parser.add_argument("--timeout", type=float, default=30.0, help="请求超时秒数（默认 30）")
    args = parser.parse_args()

    rc = asyncio.run(run(args.url, args.bytes, args.parse, args.limit, args.timeout))
    sys.exit(rc)


if __name__ == "__main__":
    main()