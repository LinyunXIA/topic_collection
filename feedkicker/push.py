from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime, timedelta

from feedkicker import bitable, feishu, store
from feedkicker.config import load_config
from feedkicker.fetch import fetch_feed

log = logging.getLogger(__name__)

PUSH_FAIL_STREAK_KEY = "push_fail_streak"
SOS_THRESHOLD = 3


def run(cfg, conn, dry_run: bool = False) -> int:
    now_dt = datetime.now(UTC)
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

    # 首跑标记只盖成功抓到内容的源：新源首跑即失败时保留未首跑状态，
    # 恢复后仍按冷启动窗口过滤历史（F4），避免全量历史当新条目推送
    ok_feed_objs = [f for f in cfg.feeds if f.name in set(ok_feeds)]

    pending = store.select_pending(conn)
    if not pending:
        store.update_first_run_all(conn, ok_feed_objs, now)
        log.info("运行完成：无新条目，失败源 %d 个", feed_fails)
        return 0

    detail_url: str | None = None
    if cfg.bitable.enabled and (cfg.bitable.url or cfg.bitable.app_token):
        detail_url = cfg.bitable.url or bitable.base_url(cfg.bitable.app_token)

    if cfg.bitable.enabled and not dry_run:
        try:
            synced_n = bitable.sync_env(cfg.bitable, cfg.app_env, conn, now)
            if synced_n:
                detail_url = cfg.bitable.url or bitable.base_url(cfg.bitable.app_token)
                log.info("多维表格已写入 %d 条", synced_n)
        except Exception as e:
            log.warning("多维表格同步未完成（不影响推送，保留待重试）: %s", e)

    top_n = cfg.site.top_n if (cfg.site.enabled or cfg.bitable.enabled) else 0
    payload = feishu.build_card(
        pending,
        feed_fails,
        [f.name for f in cfg.feeds],
        top_n=top_n,
        detail_url=detail_url,
        detail_label="📰 详情见多维表格",
    )

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
    if not ok and detail_url:
        log.warning("带按钮卡片发送失败，降级为纯链接卡片重试一次")
        ok = feishu.send(
            feishu.strip_actions(payload),
            cfg.feishu_webhook,
            cfg.http.timeout_seconds,
            cfg.http.user_agent,
            secret=cfg.feishu_secret,
        )

    if ok:
        store.mark_pushed(conn, pending, now)
        for name in ok_feeds:
            store.clear_fail(conn, name)
        streak = int(store.get_meta(conn, PUSH_FAIL_STREAK_KEY, "0"))
        if streak:
            log.info("推送恢复，清零连败计数（此前 %d 次）", streak)
        store.set_meta(conn, PUSH_FAIL_STREAK_KEY, "0")
        log.info("推送成功：%d 条新条目，失败源 %d 个", len(pending), feed_fails)
    else:
        streak = int(store.get_meta(conn, PUSH_FAIL_STREAK_KEY, "0")) + 1
        store.set_meta(conn, PUSH_FAIL_STREAK_KEY, str(streak))
        log.warning("推送未成功，连败 %d 次", streak)
        if streak >= SOS_THRESHOLD and cfg.feishu_webhook:
            sos = (
                f"⚠️ feedkicker 连续 {streak} 次推送失败，请检查机器人状态/网络。"
                f"最近一班 {len(pending)} 条已入档，详情见在线表格。"
                if detail_url
                else f"⚠️ feedkicker 连续 {streak} 次推送失败，请检查机器人状态。最近一班 {len(pending)} 条已入档。"
            )
            feishu.send_text(
                sos,
                cfg.feishu_webhook,
                cfg.http.timeout_seconds,
                cfg.http.user_agent,
                secret=cfg.feishu_secret,
            )
            store.set_meta(conn, PUSH_FAIL_STREAK_KEY, "0")

    store.update_first_run_all(conn, ok_feed_objs, now)
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="tc-push",
        description="抓取 RSS 订阅源，写入在线表格归档并把摘要卡推送到飞书",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印卡片 payload 不发送")
    parser.add_argument("--config", default=None, help="指定 config-{env}.yaml 路径")
    parser.add_argument("--db", default=None, help="sqlite 路径（覆盖 TC_DB 与 --env 推导）")
    parser.add_argument(
        "--env",
        default=None,
        choices=["dev", "test", "prod"],
        help="运行环境，决定默认配置文件与 db 路径（覆盖 TC_APP_ENV）",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    try:
        cfg = load_config(args.config, args.db, app_env=args.env)
    except Exception as e:
        log.error("%s", e)
        return 2

    conn = store.connect(cfg.db_path)
    log.info("运行开始：环境=%s，db=%s", cfg.app_env, cfg.db_path)
    try:
        return run(cfg, conn, dry_run=args.dry_run)
    except Exception:
        log.exception("未捕获异常")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
