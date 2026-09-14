"""--reseed 前置清空（#197/#218）：返回 (deleted, ok)。

dev/test 共享 Base 必须按「环境」只清本环境行；ok=False 表示未清净
（首屏/中途页/批删失败），调用方必须中止重灌，不得在未清净的表上
reset+sync 假报完成。dry_run 绝不发 +record-delete。
"""

from __future__ import annotations

import json
import logging
import re

from feedkicker import bitable_backfill, bitable_lark, bitable_purge

log = logging.getLogger(__name__)


def _markdown_has_data_row(stdout: str) -> bool:
    """表头（`| _record_id |`）与分隔行（`| --- |`）之外是否存在数据行（#242）。

    整页删净后重拉只剩表头属正常空表；有数据行却解析不出 record id 才是列序异常。
    """
    for line in stdout.splitlines():
        s = line.strip()
        if not s.startswith("|") or re.match(r"^\|\s*_record_id\s*\|", s):
            continue
        if all(set(cell) <= set("-: ") for cell in s.strip("|").split("|")):
            continue
        return True
    return False


def _delete_batches(app_token: str, table_id: str, ids: list[str]) -> tuple[int, bool]:
    deleted = 0
    for i in range(0, len(ids), bitable_lark._CHUNK):
        batch = ids[i : i + bitable_lark._CHUNK]
        proc = bitable_lark._run(
            [
                "base", "+record-delete",
                "--base-token", app_token,
                "--table-id", table_id,
                "--json", json.dumps({"record_id_list": batch}, ensure_ascii=False),
                "--yes",
            ],
            timeout=300,
        )
        if not bitable_lark._ok(proc):
            log.warning("批量删除失败（第 %d 批 %d 条），清理未完成", i // bitable_lark._CHUNK + 1, len(batch))
            return deleted, False
        deleted += len(batch)
    return deleted, True


def _env_record_ids(app_token: str, table_id: str, env_name: str) -> tuple[list[str], bool]:
    pairs, list_ok, complete = bitable_purge._list_records(app_token, table_id, env_name)
    if not list_ok or not complete:
        return [], False
    ids: list[str] = []
    envless = 0
    for rid, fds in pairs:
        if not rid:
            continue
        if bitable_backfill._cell_str(fds.get("环境")) == env_name:
            ids.append(rid)
        else:
            envless += 1
    if envless:
        log.warning("reseed：%d 条「环境」为空或不匹配的行保守保留不删（env=%s）", envless, env_name)
    return ids, True


def purge_all_records(
    app_token: str, table_id: str, dry_run: bool = False, env_name: str | None = None
) -> tuple[int, bool]:
    """清空记录（--reseed 前置），返回 (deleted, ok)。

    env_name 非空（dev/test 共享 Base）走 JSON +「环境」字段：只删匹配行，
    跨环境行保留；env_name=None（prod）沿用 markdown 快路径（删一屏拉一屏），
    dry_run 在 markdown 路径只数首屏（markdown list 无 offset 翻不了页）。
    """
    if env_name is not None:
        ids, listed = _env_record_ids(app_token, table_id, env_name)
        if not listed:
            log.warning("按环境拉取记录失败，无法确认清理范围（环境=%s）", env_name)
            return 0, False
        if dry_run:
            log.info("reseed dry-run：环境 %s 待清空 %d 条（未删除）", env_name, len(ids))
            return len(ids), True
        return _delete_batches(app_token, table_id, ids)

    deleted = 0
    pages = 0
    while True:
        pages += 1
        bitable_lark.guard_pages(pages)
        proc = bitable_lark._run(
            ["base", "+record-list", "--base-token", app_token,
             "--table-id", table_id, "--limit", "200",
             "--format", "markdown"],
            timeout=120,
        )
        if not bitable_lark._ok(proc):
            log.warning("首屏记录拉取失败，清理未完成")
            return deleted, False
        ids = bitable_lark._markdown_record_ids(proc.stdout if proc is not None else "")
        if not ids:
            raw = (proc.stdout or "").strip() if proc is not None else ""
            if raw and _markdown_has_data_row(raw):
                log.warning("reseed：markdown 输出含数据行但解析不出 record id（列序异常），中止清理")
                return deleted, False
            return deleted, True
        if dry_run:
            log.info("reseed dry-run：首屏 %d 条待清空（未删除）", len(ids))
            return len(ids), True
        d = bitable_lark._run(
            ["base", "+record-delete", "--base-token", app_token,
             "--table-id", table_id,
             "--json", json.dumps({"record_id_list": ids}, ensure_ascii=False),
             "--yes"],
            timeout=300,
        )
        if not bitable_lark._ok(d):
            log.warning("批量删除失败，终止清空")
            return deleted, False
        deleted += len(ids)
        if len(ids) < 200:
            return deleted, True
