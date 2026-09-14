from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from feedkicker import bitable_lark

log = logging.getLogger(__name__)


def _cell_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, dict):
        for k in ("link", "text", "value", "title"):
            if k in v and isinstance(v[k], str):
                return v[k]
            if k in v and v[k] is not None:
                return str(v[k])
        vals = [str(x) for x in v.values() if isinstance(x, str) and x]
        return vals[0] if vals else ""
    if isinstance(v, list):
        if v and isinstance(v[0], str):
            return v[0]
        if v and isinstance(v[0], dict):
            for k in ("link", "text", "value"):
                if k in v[0]:
                    return str(v[0][k])
        return ""
    return str(v)


def _shanghai_date(s: str) -> str | None:
    if not s or not s.strip():
        return None
    s = s.strip()
    if s.isdigit():
        try:
            iv = int(s)
            if iv > 1_000_000_000_000:
                iv = iv // 1000
            dt = datetime.fromtimestamp(iv, tz=UTC).astimezone(bitable_lark.SHANGHAI)
            return dt.strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            return None
    try:
        if "T" in s or s.endswith("Z") or "+" in s[10:]:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=bitable_lark.SHANGHAI)
            else:
                dt = dt.astimezone(bitable_lark.SHANGHAI)
            return dt.strftime("%Y-%m-%d")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)  # noqa: DTZ007
                dt = dt.replace(tzinfo=bitable_lark.SHANGHAI)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=bitable_lark.SHANGHAI)
        else:
            dt = dt.astimezone(bitable_lark.SHANGHAI)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return None


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
        records: list[Any] = data.get("records") or []
        fields: list[str] = data.get("fields") or []
        rows: list[Any] = data.get("data") or []
        rids: list[Any] = data.get("record_ids") or data.get("recordIds") or data.get("ids") or []
        pairs: list[tuple[str, dict[str, Any]]] = []
        if records and not all(isinstance(rec, dict) for rec in records):
            raise RuntimeError(f"backfill：records 子项非 dict，中止以免误判: {str(records)[:200]}")
        if records:
            for rec in records:
                fds = rec.get("fields") or rec.get("record") or {}
                rid = rec.get("record_id") or rec.get("id") or rec.get("recordId") or ""
                pairs.append((str(rid or ""), fds if isinstance(fds, dict) else {}))
            page_size = len(records)
        elif fields and rows:
            for i, r in enumerate(rows):
                rid = (
                    r.get("record_id") or r.get("id") or (rids[i] if i < len(rids) else "")
                    if isinstance(r, dict)
                    else (rids[i] if i < len(rids) else "")
                )
                pairs.append((str(rid or ""), _row_vals(fields, r)))
            page_size = len(rows)
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
    for i in range(0, len(to_fix), bitable_lark._CHUNK):
        chunk = to_fix[i : i + bitable_lark._CHUNK]
        if verb == "+record-batch-update":
            payload = json.dumps({"update_records": {rid: {"归档日期": d} for rid, d in chunk}}, ensure_ascii=False)
            proc = bitable_lark._run(
                ["base", verb, "--base-token", app_token, "--table-id", table_id, "--json", payload],
                timeout=300,
            )
            if bitable_lark._ok(proc):
                fixed += len(chunk)
            else:
                log.warning("Bitable 批量回填失败（第 %d 批 %d 条）", i // bitable_lark._CHUNK + 1, len(chunk))
        else:
            for rid, d in chunk:
                payload = json.dumps({"record_id": rid, "fields": {"归档日期": d}}, ensure_ascii=False)
                proc = bitable_lark._run(
                    ["base", verb, "--base-token", app_token, "--table-id", table_id, "--json", payload],
                    timeout=60,
                )
                if bitable_lark._ok(proc):
                    fixed += 1
                else:
                    log.warning("Bitable 单条回填失败 %s", rid)
    log.info("backfill 完成：扫描 %d 条，修复 %d 条", total_scanned, fixed)
    return fixed
