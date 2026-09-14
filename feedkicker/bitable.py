from __future__ import annotations

import json
import logging
from typing import Any

from feedkicker import bitable_backfill, bitable_lark, bitable_records, bitable_schema, bitable_views
from feedkicker.bitable_backfill import (
    _cell_str as _cell_str,
    _shanghai_date as _shanghai_date,
    backfill_empty_archive_dates as backfill_empty_archive_dates,
)
from feedkicker.bitable_lark import (
    SHANGHAI as SHANGHAI,
    _CHUNK as _CHUNK,
    _LARK_CANDIDATES as _LARK_CANDIDATES,
    _data as _data,
    _has_batch_verb as _has_batch_verb,
    _json_arg as _json_arg,
    _markdown_record_ids as _markdown_record_ids,
    _ok as _ok,
    _parse as _parse,
    _run as _run,
    _shanghai_tz as _shanghai_tz,
    lark_bin as lark_bin,
)
from feedkicker.bitable_records import (
    _cell as _cell,
    existing_links as existing_links,
    purge_all_records as purge_all_records,
    sync_env as sync_env,
    sync_records as sync_records,
)
from feedkicker.bitable_schema import (
    BASE_TITLE as BASE_TITLE,
    BASE_TITLE_DEFAULT as BASE_TITLE_DEFAULT,
    BASE_TITLE_DEV_TEST as BASE_TITLE_DEV_TEST,
    BASE_TITLES as BASE_TITLES,
    TABLE_NAME as TABLE_NAME,
    VIEW_NAME as VIEW_NAME,
    _FIELDS as _FIELDS,
    base_url as base_url,
    create_base as create_base,
    create_table as create_table,
    ensure_initialized as ensure_initialized,
    fields_for as fields_for,
    find_base_by_title as find_base_by_title,
    get_table_id as get_table_id,
)
from feedkicker.bitable_views import (
    _view_id as _view_id,
    create_date_view as create_date_view,
    ensure_archive_date_field as ensure_archive_date_field,
    set_tenant_readonly as set_tenant_readonly,
    setup_view as setup_view,
)
from feedkicker.log_setup import PLAIN_FORMAT, setup_logging

log = logging.getLogger(__name__)


def _tokens_ready(bt: Any) -> bool:
    """app_token/table_id 已配置且非占位，可做只读预览。"""
    return bool(bt.app_token and bt.table_id and "<" not in bt.app_token and "<" not in bt.table_id)


def _dry_run_plan(cfg: Any, args: Any) -> None:
    """dry-run 只读预览：不建 Base、不 patch、不清空、不写记录。"""
    bt = cfg.bitable
    log.info("dry-run：不建 Base、不写表；以下仅预览将执行的动作")
    if args.init:
        log.info("dry-run：将补归档日期字段/「按来源」「按日期」视图/组织内只读分享（未执行）")
    if args.reseed:
        if _tokens_ready(bt):
            env_name = cfg.app_env if cfg.app_env in ("dev", "test") else None
            n, purge_ok = bitable_records.purge_all_records(
                bt.app_token, bt.table_id, dry_run=True, env_name=env_name
            )
            if not purge_ok:
                log.warning("dry-run：清空预览拉取失败，%d 条仅为已扫描部分", n)
            log.info("dry-run：将清空 %d 条记录后全量重灌（未删除；prod markdown 路径仅数首屏）", n)
        else:
            log.info("dry-run：Base 未配置或为占位 token，跳过 reseed 预览")
    if args.backfill or args.fix_archive_date:
        if _tokens_ready(bt):
            env_name = cfg.app_env if cfg.app_env in ("dev", "test") else None
            try:
                n = bitable_backfill.backfill_empty_archive_dates(
                    bt.app_token, bt.table_id, env_name=env_name, dry_run=True
                )
            except RuntimeError as e:
                log.warning("dry-run：backfill 预览不可用：%s", e)
            else:
                log.info("dry-run：归档日期将回填 %d 条（未写入）", n)
        else:
            log.info("dry-run：Base 未配置或为占位 token，跳过 backfill 预览")
    log.info("dry-run：跳过 sync_env（不写记录）")


def main(argv: list[str] | None = None) -> int:
    """bitable 运维 CLI：非 --init/--reseed 需既有 Base；并发 reseed 无互斥，按单点运维执行（#229）。"""
    import argparse

    from feedkicker import store
    from feedkicker.config import load_config

    parser = argparse.ArgumentParser(prog="tc-bitable")
    parser.add_argument("--config", default=None, help="指定 config-{env}.yaml 路径")
    parser.add_argument("--db", default=None, help="sqlite 路径（覆盖 TC_DB 与 --env 推导）")
    parser.add_argument("--env", default=None, choices=["dev", "test", "prod"])
    parser.add_argument("--init", action="store_true", help="补字段/视图/组织内只读分享")
    exclusive = parser.add_mutually_exclusive_group()
    exclusive.add_argument("--reseed", action="store_true", help="清空表内记录后全量重灌")
    exclusive.add_argument("--backfill", action="store_true", help="回填存量空归档日期")
    exclusive.add_argument("--fix-archive-date", action="store_true", help="回填存量空归档日期（--backfill 别名）")
    parser.add_argument("--dry-run", action="store_true", help="只读预览：不建 Base、不写表、不清空")
    args = parser.parse_args(argv)

    setup_logging(logging.INFO, PLAIN_FORMAT)
    cfg = load_config(args.config, args.db, app_env=args.env)
    if not cfg.bitable.enabled:
        log.info("bitable 未启用（%s）", cfg.app_env)
        return 0
    if args.dry_run:
        _dry_run_plan(cfg, args)
        return 0

    if args.init and ("<" in cfg.bitable.app_token or "<" in cfg.bitable.table_id):
        log.error("拒绝 --init：app_token/table_id 仍为 `<...>` 占位（config 未填写），请先配置真实 Base token")
        return 2

    try:
        have_lark = bool(bitable_lark.lark_bin())
    except FileNotFoundError:
        have_lark = False
    if not have_lark:
        log.error("找不到 lark-cli，拒绝执行非 dry-run 动作（请先安装 @larksuite/cli 并完成 auth login）")
        return 2

    if not (args.init or args.reseed) and not _tokens_ready(cfg.bitable):
        log.error(
            "拒绝执行：Base 未配置或为占位 token（仅 --init/--reseed 可创建/修复 Base，其余动作需既有 Base）"
        )
        return 2
    if args.reseed and not _tokens_ready(cfg.bitable):
        log.error(
            "拒绝 --reseed：Base 未配置或为占位 token（需既有且非占位 app_token/table_id），不执行先建后清"
        )
        return 2
    if args.init and not _tokens_ready(cfg.bitable):
        log.warning("--init 将创建/修复 Base：当前 app_token/table_id 为空或为占位")

    info = bitable_schema.ensure_initialized(cfg.bitable, cfg.app_env)
    log.info("Base: %s", info["url"])

    if args.init:
        bitable_views.ensure_archive_date_field(info["app_token"], info["table_id"])
        if bitable_views.setup_view(info["app_token"], info["table_id"]):
            log.info("「按来源」分组视图已设置")
        if bitable_views.create_date_view(info["app_token"], info["table_id"]):
            log.info("「按日期」分组视图已创建")
        if bitable_views.set_tenant_readonly(info["app_token"]):
            log.info("分享已设为组织内只读")
        print(json.dumps(info, ensure_ascii=False))

    conn = store.connect(cfg.db_path)
    try:
        if args.reseed:
            reset = store.reset_bitable_synced(conn)
            log.info("已重置 %d 条同步标记（先清标记后清表，任一中断点均可自愈重灌）", reset)
            env_name = cfg.app_env if cfg.app_env in ("dev", "test") else None
            n, purge_ok = bitable_records.purge_all_records(
                info["app_token"], info["table_id"], env_name=env_name
            )
            if not purge_ok:
                log.error("清理未完成（已删 %d 条），中止 reseed；标记已清，下轮可自愈重灌", n)
                return 2
            log.info("已清空 %d 条旧记录，准备重灌", n)
        if args.backfill or args.fix_archive_date:
            env_name = cfg.app_env if cfg.app_env in ("dev", "test") else None
            try:
                n = bitable_backfill.backfill_empty_archive_dates(
                    info["app_token"], info["table_id"], env_name=env_name, dry_run=False
                )
            except Exception as e:  # noqa: BLE001
                log.error("backfill 失败: %s", e)
                return 2
            log.info("归档日期回填：%d 条", n)
        synced = bitable_records.sync_env(cfg.bitable, cfg.app_env, conn)
        log.info("同步完成：%d 条", synced)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
