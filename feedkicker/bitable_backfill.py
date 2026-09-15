from __future__ import annotations

import json
import logging
from typing import Any

from feedkicker import bitable_lark, topic_records
from feedkicker.bitable_dates import _cell_str as _cell_str
from feedkicker.bitable_dates import _shanghai_date as _shanghai_date

log = logging.getLogger(__name__)


def _env_ok(vals: dict[str, Any], env_name: str | None) -> bool:
    """dev/test 共享 Base 下按「环境」过滤；字段缺失/为空不额外过滤（向后兼容）。"""
    if env_name is None:
        return True
    env = _cell_str(vals.get("环境"))
    return not env or env == env_name


def _row_vals(fields: list[str], r: Any) -> dict[str, Any]:
    if isinstance(r, dict):
        vals = r.get("fields") or r.get("values")
        return vals if isinstance(vals, dict) else r
    if isinstance(r, list):
        return {fields[j]: r[j] for j in range(min(len(fields), len(r)))}
    return {}


def backfill_empty_archive_dates(
    app_token: str, table_id: str, env_name: str | None = None, dry_run: bool = False
) -> int:
    verb = bitable_lark._has_batch_verb()
    if not verb:
        raise RuntimeError("lark-cli 不可用，无法 backfill")
    env_args = (
        ["--field-id", "环境", "--field-id", "归档日期", "--field-id", "推送时间"]
        if env_name is not None
        else []
    )
    offset = 0
    prev_fp = ""
    to_fix: list[tuple[str, str]] = []
    total_scanned = 0
    while True:
        bitable_lark._guard_offset(offset)
        bitable_lark.guard_pages(offset // bitable_lark._CHUNK + 1)
        proc = bitable_lark._run(
            [
                "base", "+record-list",
                "--base-token", app_token,
                "--table-id", table_id,
                "--limit", "200",
                "--offset", str(offset),
                "--json",
                *env_args,
            ],
            timeout=120,
        )
        if proc is None:
            log.error("backfill：lark-cli 执行失败(无返回)，中止以免不完整回填（已扫描 %d 条）", total_scanned)
            raise RuntimeError("lark-cli 执行失败(无返回)，backfill 中止")
        if not bitable_lark._ok(proc):
            log.warning("backfill：第 %d 页拉取失败，中止以免不完整回填（已扫描 %d 条）", offset // 200 + 1, total_scanned)
            raise RuntimeError(f"backfill 第 {offset // 200 + 1} 页拉取失败，中止")
        data = bitable_lark._data(proc)
        prev_fp = bitable_lark._page_guard(prev_fp, data)
        has_rec = isinstance(data.get("records"), list) or isinstance(data.get("items"), list)
        fields_ok = isinstance(data.get("fields"), list)
        rows_ok = isinstance(data.get("data"), list)
        if not (has_rec or fields_ok or topic_records.row_ids(data)):
            raise RuntimeError(
                f"backfill：record-list 响应无法识别（无 records/fields+data），中止以免不完整回填: {str(data)[:200]}"
            )
        records: list[Any] = data.get("records") or data.get("items") or []
        fields: list[str] = data.get("fields") or []
        rows: list[Any] = data.get("data") or []
        rids: list[Any] = topic_records.row_ids(data)
        pairs: list[tuple[str, dict[str, Any]]] = []
        if has_rec:
            if records and not all(isinstance(rec, dict) for rec in records):
                raise RuntimeError(f"backfill：records 子项非 dict，中止以免误判: {str(records)[:200]}")
            for i, rec in enumerate(records):
                fds = rec.get("fields") or rec.get("record") or {}
                rid = rids[i] if i < len(rids) else ""
                pairs.append((str(rid or ""), fds if isinstance(fds, dict) else {}))
            page_size = len(records)
        elif fields_ok and rows_ok and fields and rows:
            for i, r in enumerate(rows):
                rid = (
                    r.get("record_id") or r.get("id") or (rids[i] if i < len(rids) else "")
                    if isinstance(r, dict)
                    else (rids[i] if i < len(rids) else "")
                )
                pairs.append((str(rid or ""), _row_vals(fields, r)))
            page_size = len(rows)
        elif topic_records.row_ids(data):
            raise RuntimeError("backfill：响应仅含 record_id_list 而无 fields/data 行，无法解析，中止以免不完整回填")
        else:
            break
        for rid, vals in pairs:
            total_scanned += 1
            if not _env_ok(vals, env_name):
                continue
            if _cell_str(vals.get("归档日期")).strip():
                continue
            cand = _cell_str(vals.get("推送时间"))
            d = _shanghai_date(cand) if cand else None
            if d and rid:
                to_fix.append((rid, d))
        if page_size < 200:
            break
        offset += 200
    if dry_run:
        log.info("backfill dry-run：扫描 %d 条，待修复 %d 条（未写入）", total_scanned, len(to_fix))
        return len(to_fix)
    fixed = 0
    failed = 0
    for i in range(0, len(to_fix), bitable_lark._CHUNK):
        chunk = to_fix[i : i + bitable_lark._CHUNK]
        payload = json.dumps({"update_records": {rid: {"归档日期": d} for rid, d in chunk}}, ensure_ascii=False)
        proc = bitable_lark._run(
            ["base", verb, "--base-token", app_token, "--table-id", table_id, "--json", payload],
            timeout=300,
        )
        if bitable_lark._ok(proc):
            fixed += len(chunk)
        else:
            failed += len(chunk)
            log.warning("Bitable 批量回填失败（第 %d 批 %d 条）", i // bitable_lark._CHUNK + 1, len(chunk))
    log.info("backfill 完成：扫描 %d 条，修复 %d 条，失败 %d 条", total_scanned, fixed, failed)
    if failed and not fixed:
        raise RuntimeError(f"backfill 全部写入失败（failed={failed}），中止（调用方 rc2）")
    if failed:
        raise RuntimeError(f"backfill 部分写入失败（fixed={fixed}, failed={failed}），中止（调用方 rc2）")
    return fixed
