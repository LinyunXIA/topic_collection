from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime

from feedkicker import minimax, salon_md, salon_notify, store, wiki, wiki_home
from feedkicker.config import load_config
from feedkicker.log_setup import setup_logging
from feedkicker.topic import fetch_selected_topics

log = logging.getLogger(__name__)


def _token_missing(value: str) -> bool:
    """空值或 `<...>` 占位均视为缺失（与 bitable._tokens_ready 同口径，#204）。"""
    return not value or "<" in value


def run(cfg, conn, dry_run: bool = False) -> int:
    if not cfg.salon.enabled:
        log.warning("salon.enabled=false，跳过周五大纲流程")
        return 0
    app_token, table_id = cfg.salon.app_token, cfg.salon.table_id
    if (_token_missing(app_token) or _token_missing(table_id)) and dry_run:
        selected = [
            {"record_id": "recStub000", "fields": {"讨论状态": ["已选题"], "话题名称": "示例已选题话题"}}
        ]
    elif _token_missing(app_token) or _token_missing(table_id):
        log.warning("salon 未配置 app_token/table_id，跳过")
        return 0
    else:
        try:
            selected = fetch_selected_topics(app_token, table_id)
        except Exception as e:  # noqa: BLE001
            log.warning("拉取已选题失败: %s", e)
            raise

    if not selected:
        log.info("无已选题，跳过")
        return 0

    wiki_space = cfg.wiki.space_id or cfg.salon.wiki_space_id
    wiki_parent = cfg.wiki.parent_token or cfg.salon.wiki_parent_token
    wiki_app = cfg.wiki.app_token or cfg.salon.app_token
    if not dry_run and (_token_missing(wiki_space) or _token_missing(wiki_parent)):
        log.warning("wiki space/parent 为空或占位，跳过建 Wiki 与标记（不产生孤儿文档）")
        return 0

    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    wiki_urls: list[str] = []
    success_count = 0
    attempted = 0
    skipped = 0

    for rec in selected:
        rid = str(rec.get("record_id") or rec.get("id") or "")
        if not rid:
            log.warning("跳过无 record_id 的记录: %s", rec)
            continue

        if salon_md.record_status(rec) != "已选题":
            skipped += 1
            continue

        last_status = store.get_ppt_last_status(conn, rid)
        ppt_synced_is_null = not store.is_ppt_synced(conn, rid)

        if not ppt_synced_is_null and last_status == "已选题":
            log.info("跳过已处理 %s (last_status=已选题)", rid)
            skipped += 1
            continue

        title = salon_md.topic_title(rec)

        if dry_run:
            tool_outline, principle_outline = salon_md.stub_outlines(title)
        else:
            attempted += 1
            try:
                tool_outline = minimax.gen_outline(
                    title,
                    kind="tool",
                    api_key=cfg.minimax.api_key,
                    base_url=cfg.minimax.base_url,
                    model=cfg.minimax.model,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("topic %s 工具类大纲生成失败: %s", rid, e)
                continue

            try:
                principle_outline = minimax.gen_outline(
                    title,
                    kind="principle",
                    api_key=cfg.minimax.api_key,
                    base_url=cfg.minimax.base_url,
                    model=cfg.minimax.model,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("topic %s 原理类大纲生成失败: %s", rid, e)
                continue

        try:
            combined_md = salon_md.build_combined_md(title, tool_outline, principle_outline)
        except Exception as e:  # noqa: BLE001
            log.warning("topic %s 大纲合并失败: %s", rid, e)
            continue
        if dry_run:
            print(json.dumps({"tool_outline": tool_outline, "principle_outline": principle_outline}, ensure_ascii=False, indent=2))
            print(combined_md[:3000])

        try:
            url = wiki.create_wiki_doc_from_md(
                wiki_app,
                wiki_space,
                wiki_parent,
                title,
                combined_md,
                dry_run=dry_run,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("topic %s Wiki 写入失败: %s", rid, e)
            continue

        wiki_urls.append(url)
        log.info("topic %s Wiki 已创建: %s", rid, url)

        if dry_run:
            continue

        store.mark_topic_archived(conn, table_id, rid, title, url, combined_md, now_iso)
        success_count += 1

    if wiki_urls and not dry_run:
        log.info("本轮成功 %d 条，Wiki: %s", success_count, wiki_urls)
    elif dry_run:
        log.info("dry-run 完成，待处理 %d 条，Wiki 预览 %d 个", len(wiki_urls), len(wiki_urls))
        if wiki_urls:
            print(json.dumps({"wiki_urls": wiki_urls}, ensure_ascii=False, indent=2))
    else:
        log.info("本轮无新增 Wiki")

    if not dry_run and attempted and not wiki_urls:
        log.warning(
            "已选题 %d 条，尝试 %d 条但 0 条成功建 Wiki（全部失败；另 %d 条跳过），请查上方 WARNING",
            len(selected),
            attempted,
            skipped,
        )

    card_ok = salon_notify.send_wiki_card(cfg, conn, wiki_urls, dry_run=dry_run)

    if wiki_urls and wiki_space and wiki_parent:
        try:
            wiki_home.update_homepage(wiki_space, wiki_parent, dry_run=dry_run)
        except Exception as e:  # noqa: BLE001
            log.warning("Wiki 首页更新失败（不影响主流程）: %s", e)

    return 0 if card_ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="feedkicker.salon_flow",
        description="已选题→双大纲→Wiki 归档",
    )
    parser.add_argument("--dry-run", action="store_true", help="仅预览不写库不落 Wiki")
    parser.add_argument("--config", default=None, help="指定 config-{env}.yaml 路径")
    parser.add_argument("--db", default=None, help="sqlite 路径（覆盖 TC_DB 与 --env 推导）")
    parser.add_argument(
        "--env",
        default=None,
        choices=["dev", "test", "prod"],
        help="运行环境，决定默认配置文件与 db 路径（覆盖 TC_APP_ENV）",
    )
    args = parser.parse_args(argv)

    setup_logging(logging.INFO)
    try:
        cfg = load_config(args.config, args.db, app_env=args.env)
    except Exception as e:  # noqa: BLE001
        log.error("%s", e)
        return 2

    conn = store.connect(cfg.db_path)
    log.info("salon_flow 运行开始：环境=%s，db=%s，dry_run=%s", cfg.app_env, cfg.db_path, args.dry_run)
    try:
        return run(cfg, conn, dry_run=args.dry_run)
    except Exception:  # noqa: BLE001
        log.exception("未捕获异常")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
