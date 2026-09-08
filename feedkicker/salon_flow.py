from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from feedkicker import minimax, salon_md, salon_notify, store, wiki, wiki_home
from feedkicker.config import load_config
from feedkicker.topic import fetch_selected_topics

log = logging.getLogger(__name__)


def run(cfg, conn, dry_run: bool = False) -> int:
    app_token = cfg.salon.app_token
    table_id = cfg.salon.table_id
    selected: list[dict[str, Any]]
    if (not app_token or not table_id) and dry_run:
        selected = [
            {"record_id": "recGWg8Kb9kUDI", "fields": {"讨论状态": ["已选题"], "话题名称": "示例已选题话题"}}
        ]
        app_token = app_token or "stub_app"
        table_id = table_id or "stub_tbl"
    elif not app_token or not table_id:
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

    wiki_space = cfg.salon.wiki_space_id or cfg.wiki.space_id
    wiki_parent = cfg.salon.wiki_parent_token or cfg.wiki.parent_token
    wiki_app = cfg.wiki.app_token or cfg.salon.app_token

    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    wiki_urls: list[str] = []
    success_count = 0

    for rec in selected:
        rid = str(rec.get("record_id") or rec.get("id") or "")
        if not rid:
            log.warning("跳过无 record_id 的记录: %s", rec)
            continue

        fields: dict[str, Any] = rec.get("fields") or {}
        cur_status_val = fields.get("讨论状态")
        if isinstance(cur_status_val, list):
            cur_status = cur_status_val[0] if cur_status_val else ""
        elif isinstance(cur_status_val, str):
            cur_status = cur_status_val
        else:
            cur_status = ""

        if cur_status != "已选题":
            continue

        last_status = store.get_ppt_last_status(conn, rid)
        ppt_synced_is_null = not store.is_ppt_synced(conn, rid)

        if not ppt_synced_is_null and last_status == "已选题":
            log.info("跳过已处理 %s (last_status=已选题)", rid)
            continue

        if dry_run and last_status == "已选题" and not ppt_synced_is_null:
            continue

        title = salon_md.topic_title(rec)

        if dry_run:
            tool_outline, principle_outline = salon_md.stub_outlines(title)
        else:
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

        combined_md = salon_md.build_combined_md(title, tool_outline, principle_outline)
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

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
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
