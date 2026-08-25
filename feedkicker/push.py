from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

from feedkicker import feishu, store
from feedkicker.config import load_config
from feedkicker.fetch import fetch_feed

log = logging.getLogger(__name__)


def run(cfg, conn, dry_run: bool = False) -> int:
    now_dt = datetime.now(timezone.utc)
    now = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    feed_fails = 0
    ok_feeds: list[str] = []

    for feed in cfg.feeds:
        try:
            entries = fetch_feed(feed.url, cfg.http)
            store.download(conn, feed.name, entries, now)
            ok_feeds.append(feed.name)
            if store.is_first_run(conn, feed.name):
                cutoff = (now_dt - timedelta(days=cfg.bootstrap_days)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                store.promise_skip_old(conn, feed.name, cutoff, now)
        except Exception as e:
            feed_fails += 1
            store.bump_fail(conn, feed.name, feed.url)
            log.error("源 %s (%s) 抓取失败: %s", feed.name, feed.url, e)

    pending = store.select_pending(conn)
    if not pending:
        store.update_first_run_all(conn, cfg.feeds, now)
        log.info("运行完成：无新条目，失败源 %d 个", feed_fails)
        return 0

    payload = feishu.build_card(pending, feed_fails, [f.name for f in cfg.feeds])

    if dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        log.info("dry-run：共 %d 条待推，已打印 payload 未发送", len(pending))
        return 0

    ok = feishu.send(
        payload,
        cfg.feishu_webhook,
        cfg.http.timeout_seconds,
        cfg.http.user_agent,
        secret=cfg.feishu_secret,
    )
    if ok:
        store.mark_pushed(conn, pending, now)
        for name in ok_feeds:
            store.clear_fail(conn, name)
        log.info("推送成功：%d 条新条目，失败源 %d 个", len(pending), feed_fails)
    else:
        log.warning("推送未成功，%d 条保留待下次重试", len(pending))
    store.update_first_run_all(conn, cfg.feeds, now)
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="tc-push",
        description="抓取 RSS 订阅源，把新增条目聚成一张飞书汇总卡片推送",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印卡片 payload 不发送")
    parser.add_argument("--config", default=None, help="指定 config.yaml 路径")
    parser.add_argument("--db", default=None, help="sqlite 路径（覆盖 TC_DB）")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    try:
        cfg = load_config(args.config, args.db)
    except Exception as e:
        log.error("%s", e)
        return 2

    conn = store.connect(cfg.db_path)
    try:
        return run(cfg, conn, dry_run=args.dry_run)
    except Exception:
        log.exception("未捕获异常")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
