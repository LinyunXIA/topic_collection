from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime

from feedkicker import feishu, minimax, store, wiki
from feedkicker.config import load_config
from feedkicker.topic import fetch_selected_topics

log = logging.getLogger(__name__)


def _topic_title(rec: dict) -> str:
    fields = rec.get("fields") or {}
    for k in ("话题名称", "标题", "title", "Topic", "name"):
        v = fields.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, list) and v and isinstance(v[0], str) and v[0].strip():
            return v[0].strip()
    rid = rec.get("record_id") or rec.get("id") or ""
    return rid or "未命名话题"


def _outline_to_md(outline: dict, label: str) -> str:
    title = outline.get("title") or label
    slides = outline.get("slides") or []
    lines = [f"## {label}", "", f"**{title}**", ""]
    for idx, s in enumerate(slides, 1):
        heading = s.get("heading") or f"第{idx}页"
        bullets = s.get("bullets") or []
        note = s.get("speaker_note") or s.get("speakerNote") or ""
        lines.append(f"### {idx}. {heading}")
        for b in bullets:
            lines.append(f"- {b}")
        if note:
            lines.append(f"> 备注：{note}")
        lines.append("")
    md_json = json.dumps(outline, ensure_ascii=False, indent=2)
    lines.append(f"```json\n{md_json}\n```")
    lines.append("")
    return "\n".join(lines)


def _build_combined_md(title: str, tool_outline: dict, principle_outline: dict) -> str:
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    header = f"# {title} · 大纲归档 {date_str}\n"
    tool_md = _outline_to_md(tool_outline, "工具类大纲")
    princ_md = _outline_to_md(principle_outline, "原理类大纲")
    return f"{header}\n{tool_md}\n---\n\n{princ_md}\n"


def run(cfg, conn, dry_run: bool = False) -> int:
    app_token = cfg.salon.app_token
    table_id = cfg.salon.table_id
    if (not app_token or not table_id) and dry_run:
        selected = [{"record_id": "recGWg8Kb9kUDI", "fields": {"讨论状态": ["已选题"], "话题名称": "示例已选题话题"}}]
        app_token = app_token or "stub_app"
        table_id = table_id or "stub_tbl"
    elif not app_token or not table_id:
        log.warning("salon 未配置 app_token/table_id，跳过")
        return 0
    else:
        try:
            selected = fetch_selected_topics(app_token, table_id)
        except Exception as e:
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
    try:
        unsynced_topics = store.select_unsynced_topics(conn)
        _unsynced_keys = {r.get("entry_key") for r in unsynced_topics}
    except Exception:  # noqa: BLE001
        _unsynced_keys = None

    for rec in selected:
        rid = rec.get("record_id") or rec.get("id") or ""
        if not rid:
            log.warning("跳过无 record_id 的记录: %s", rec)
            continue

        fields = rec.get("fields") or {}
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

        if _unsynced_keys is not None and rid in _unsynced_keys:
            ppt_synced_is_null = True
        else:
            ppt_synced_is_null = True
            try:
                row = conn.execute("SELECT ppt_synced_at FROM articles WHERE entry_key = ?", (rid,)).fetchone()
                if row is not None and row[0] is not None:
                    ppt_synced_is_null = False
                elif row is not None:
                    ppt_synced_is_null = True
                else:
                    ppt_synced_is_null = True
            except Exception:  # noqa: BLE001
                ppt_synced_is_null = True

        if not ppt_synced_is_null and last_status == "已选题":
            log.info("跳过已处理 %s (last_status=已选题)", rid)
            continue

        if dry_run and last_status == "已选题" and not ppt_synced_is_null:
            continue

        title = _topic_title(rec)

        if dry_run and (not cfg.minimax.api_key or cfg.minimax.api_key.strip().startswith('<')):
            tool_outline = {"title": f"{title} · 工具类大纲", "slides": [{"heading": f"工具页{i}", "bullets": ["要点A", "要点B", "要点C"], "speaker_note": "备注"} for i in range(1, 6)]}
            principle_outline = {"title": f"{title} · 原理类大纲", "slides": [{"heading": f"原理页{i}", "bullets": ["要点A", "要点B", "要点C"], "speaker_note": "备注"} for i in range(1, 6)]}
        else:
            try:
                tool_outline = minimax.gen_outline(
                    title,
                    kind="tool",
                    api_key=cfg.minimax.api_key,
                    base_url=cfg.minimax.base_url,
                    model=cfg.minimax.model,
                )
            except Exception as e:
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
            except Exception as e:
                log.warning("topic %s 原理类大纲生成失败: %s", rid, e)
                continue

        combined_md = _build_combined_md(title, tool_outline, principle_outline)
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
        except Exception as e:
            log.warning("topic %s Wiki 写入失败: %s", rid, e)
            continue

        wiki_urls.append(url)
        log.info("topic %s Wiki 已创建: %s", rid, url)

        if dry_run:
            continue

        try:
            exists = conn.execute("SELECT 1 FROM articles WHERE entry_key = ?", (rid,)).fetchone()
            if exists is None:
                conn.execute(
                    "INSERT INTO articles (feed_id, entry_key, title, url, description, published_at, first_seen, pushed_at, ppt_synced_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL) ON CONFLICT (feed_id, entry_key) DO NOTHING",
                    (table_id, rid, title, url, combined_md[:500], None, now_iso),
                )
                conn.commit()
        except Exception as e:
            log.warning("插入占位 article 失败 %s: %s", rid, e)

        try:
            store.mark_ppt_synced(conn, [rid], now_iso)
        except Exception as e:
            log.warning("mark_ppt_synced 失败 %s: %s", rid, e)

        try:
            store.set_ppt_last_status(conn, rid, "已选题")
        except Exception as e:
            log.warning("set_ppt_last_status 失败 %s: %s", rid, e)

        success_count += 1

    if wiki_urls and not dry_run:
        log.info("本轮成功 %d 条，Wiki: %s", success_count, wiki_urls)
    elif dry_run:
        log.info("dry-run 完成，待处理 %d 条，Wiki 预览 %d 个", len(wiki_urls), len(wiki_urls))
        if wiki_urls:
            print(json.dumps({"wiki_urls": wiki_urls}, ensure_ascii=False, indent=2))
    else:
        log.info("本轮无新增 Wiki")

    if wiki_urls:
        payload = feishu.build_card([], 0, [], wiki_urls=wiki_urls)
        if dry_run:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            log.info("dry-run 卡片预览已打印（含 %d 个 Wiki 链接）", len(wiki_urls))
        else:
            try:
                ok = feishu.send(
                    payload,
                    cfg.feishu_webhook,
                    cfg.http.timeout_seconds,
                    cfg.http.user_agent,
                    secret=cfg.feishu_secret,
                )
                if not ok:
                    log.warning("Wiki 卡片发送失败，降级为纯链接卡片重试一次")
                    ok = feishu.send(
                        feishu.strip_actions(payload),
                        cfg.feishu_webhook,
                        cfg.http.timeout_seconds,
                        cfg.http.user_agent,
                        secret=cfg.feishu_secret,
                    )
                if ok:
                    log.info("Wiki 卡片已推送 %d 个链接", len(wiki_urls))
                else:
                    log.warning("Wiki 卡片推送失败")
            except Exception as e:  # noqa: BLE001
                log.warning("Wiki 卡片推送异常: %s", e)

    return 0


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
    except Exception as e:
        log.error("%s", e)
        return 2

    conn = store.connect(cfg.db_path)
    log.info("salon_flow 运行开始：环境=%s，db=%s，dry_run=%s", cfg.app_env, cfg.db_path, args.dry_run)
    try:
        return run(cfg, conn, dry_run=args.dry_run)
    except Exception:
        log.exception("未捕获异常")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
