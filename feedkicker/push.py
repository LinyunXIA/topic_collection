from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

from feedkicker import feishu, publish, site, store
from feedkicker.config import load_config
from feedkicker.fetch import fetch_feed

log = logging.getLogger(__name__)

PUSH_FAIL_STREAK_KEY = "push_fail_streak"
SOS_THRESHOLD = 3


def _local_day_bounds_utc() -> tuple[str, str]:
    now_local = datetime.now().astimezone()
    start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        (start + timedelta(days=1)).astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    )


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

    detail_url: str | None = None
    day_path: str | None = None
    if cfg.site.enabled and not dry_run:
        local_today = datetime.now().astimezone()
        day_str = local_today.strftime("%Y-%m-%d")
        day_path = f"daily/{day_str}.html"

    if day_path:
        store.mark_pushed(conn, pending, now)
        marked_now = True
    else:
        marked_now = False

    if day_path:
        since_iso, until_iso = _local_day_bounds_utc()
        day_items = [
            it for it in store.select_pushed_since(conn, since_iso)
            if it["pushed_at"] < until_iso
        ]
        html_page = site.render_daily(day_items, local_today.date(), [f.name for f in cfg.feeds])
        ok_daily = publish.publish_file(
            cfg.site.repo,
            cfg.site.branch,
            day_path,
            html_page,
            f"feeds: {day_str} 汇总页更新",
        )
        archives = _collect_archives(conn)
        ok_index = publish.publish_file(
            cfg.site.repo,
            cfg.site.branch,
            "index.html",
            site.render_index(archives),
            f"feeds: 归档目录更新 {day_str}",
        )
        if ok_daily and ok_index:
            detail_url = f"{cfg.site.base_url}/{day_path}"
            publish.wait_published(detail_url)
        else:
            log.error("GitHub Pages 发布失败，摘要卡将不带详情链接")

    payload = feishu.build_card(
        pending,
        feed_fails,
        [f.name for f in cfg.feeds],
        top_n=cfg.site.top_n if cfg.site.enabled else 0,
        detail_url=detail_url,
    )

    if dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        log.info("dry-run：共 %d 条待推，已打印 payload 未发送", len(pending))
        return 0

    if not marked_now:
        store.mark_pushed(conn, pending, now)

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
                f"最近一班 {len(pending)} 条已入档：{cfg.site.base_url}/index.html"
                if cfg.site.enabled
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

    store.update_first_run_all(conn, cfg.feeds, now)
    return 0 if ok else 1


def _collect_archives(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT DISTINCT substr(pushed_at, 1, 10) AS d, COUNT(*) AS c"
        " FROM articles WHERE pushed_at IS NOT NULL GROUP BY d ORDER BY d DESC LIMIT 60"
    ).fetchall()
    return [{"date": r[0], "count": r[1], "path": f"daily/{r[0]}.html"} for r in rows]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="tc-push",
        description="抓取 RSS 订阅源，发布汇总页并把摘要卡推送到飞书",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印卡片 payload 不发布不发送")
    parser.add_argument("--config", default=None, help="指定 config.yaml 路径")
    parser.add_argument("--db", default=None, help="sqlite 路径（覆盖 TC_DB 与 --env 推导）")
    parser.add_argument(
        "--env",
        default=None,
        choices=["dev", "test", "prod"],
        help="运行环境，决定默认 db 路径 data/tc-{env}.sqlite3（覆盖 TC_APP_ENV）",
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
