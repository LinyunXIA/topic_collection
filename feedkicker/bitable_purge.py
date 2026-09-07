"""多维表格侧的滚动清理：按「推送时间」客户端过滤过期记录并批量删除。

删除不可恢复，安全约定：
- 只由 tc-purge 显式调用，默认 dry-run 只计数，--apply 才发 +record-delete；
- 首屏 record-list 失败安全返回零删除（防误判全表过期）；
- 仅操作传入的资讯归档 Base（cfg.bitable），不碰 salon 选题 Base；
- 删除按 200/批 +record-delete --yes，批失败即终止。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from feedkicker import bitable

log = logging.getLogger(__name__)

_CHUNK = 200


def cutoff_date_shanghai(days: int, now: datetime | None = None) -> str:
    """上海时区下 now-days 的 %Y-%m-%d 日期串（字典序即时间序）。"""
    ref = now if now is not None else datetime.now(bitable.SHANGHAI)
    return (ref - timedelta(days=days)).astimezone(bitable.SHANGHAI).strftime("%Y-%m-%d")


def _pushed_date(fields: dict[str, Any]) -> str | None:
    """fields 中「推送时间」→ 上海 %Y-%m-%d；兼容 epoch 毫秒/ISO/纯日期。"""
    raw = bitable._cell_str(fields.get("推送时间"))
    return bitable._shanghai_date(raw) if raw else None


def _list_records(
    app_token: str, table_id: str
) -> tuple[list[tuple[str, dict[str, Any]]], bool]:
    """分页拉全表，返回 ([(record_id, fields)], 首屏是否成功)。

    兼容 records 包装与 fields+data 行式两种响应；中途页失败时已扫描记录
    仍返回（删除只针对其中过期者，未扫到的留待下轮巡检）。
    """
    out: list[tuple[str, dict[str, Any]]] = []
    offset = 0
    first = True
    while True:
        proc = bitable._run(
            [
                "base", "+record-list",
                "--base-token", app_token,
                "--table-id", table_id,
                "--limit", str(_CHUNK),
                "--offset", str(offset),
                "--json",
            ],
            timeout=120,
        )
        if not bitable._ok(proc):
            if first:
                log.warning("purge：首屏 record-list 失败，跳过本次 bitable 清理")
                return [], False
            log.warning("purge：第 %d 页拉取失败，仅处理已扫描记录", offset // _CHUNK + 1)
            return out, True
        first = False
        data = bitable._data(proc)
        records: list[dict[str, Any]] = data.get("records") or []
        if records:
            for rec in records:
                rid = str(rec.get("record_id") or rec.get("id") or rec.get("recordId") or "")
                fds = rec.get("fields") or rec.get("record") or {}
                out.append((rid, fds if isinstance(fds, dict) else {}))
            if len(records) < _CHUNK:
                return out, True
            offset += _CHUNK
            continue
        fields: list[Any] = data.get("fields") or []
        rows: list[Any] = data.get("data") or []
        if not fields or not rows:
            return out, True
        rids = data.get("record_ids") or data.get("recordIds") or data.get("ids") or []
        idx_push = fields.index("推送时间") if "推送时间" in fields else -1
        for i, r in enumerate(rows):
            rid = str(rids[i] if i < len(rids) else "")
            if isinstance(r, dict):
                fds = r.get("fields") or r.get("values") or r
                out.append((rid, fds if isinstance(fds, dict) else {}))
            elif isinstance(r, list) and idx_push >= 0 and idx_push < len(r):
                out.append((rid, {"推送时间": r[idx_push]}))
        if len(rows) < _CHUNK:
            return out, True
        offset += _CHUNK


def purge_expired_records(
    app_token: str, table_id: str, cutoff_date: str, dry_run: bool = False
) -> tuple[int, int, int]:
    """删除推送时间早于 cutoff_date（%Y-%m-%d 字典序比较）的记录。

    返回 (deleted, expired, scanned)；dry-run 时 deleted=0。
    """
    pairs, list_ok = _list_records(app_token, table_id)
    if not list_ok:
        return 0, 0, 0
    expired: list[str] = []
    for rid, fds in pairs:
        if not rid:
            continue
        d = _pushed_date(fds)
        if d and d < cutoff_date:
            expired.append(rid)
    if dry_run:
        log.info("purge dry-run：扫描 %d 条，过期 %d 条（未删除）", len(pairs), len(expired))
        return 0, len(expired), len(pairs)
    deleted = 0
    for i in range(0, len(expired), _CHUNK):
        batch = expired[i : i + _CHUNK]
        proc = bitable._run(
            [
                "base", "+record-delete",
                "--base-token", app_token,
                "--table-id", table_id,
                "--json", json.dumps({"record_id_list": batch}, ensure_ascii=False),
                "--yes",
            ],
            timeout=300,
        )
        if not bitable._ok(proc):
            log.warning("purge：第 %d 批删除失败（%d 条），终止", i // _CHUNK + 1, len(batch))
            break
        deleted += len(batch)
    log.info("purge：扫描 %d 条，过期 %d 条，删除 %d 条", len(pairs), len(expired), deleted)
    return deleted, len(expired), len(pairs)
