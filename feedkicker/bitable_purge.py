"""多维表格侧的滚动清理：按「推送时间」客户端过滤过期记录并批量删除。

删除不可恢复，安全约定：
- 只由 tc-purge 显式调用，默认 dry-run 只计数，--apply 才发 +record-delete；
- 首屏 record-list 失败安全返回零删除（防误判全表过期）；
- 仅操作传入的资讯归档 Base（cfg.bitable），不碰 salon 选题 Base；
- 删除按 200/批 +record-delete --yes，批失败即终止；
- 结果附 listed_ok/applied_ok/complete，purge 依此判定是否写入 meta（#160/#180）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from feedkicker import bitable_backfill, bitable_lark

log = logging.getLogger(__name__)

_CHUNK = 200


def cutoff_date_shanghai(days: int, now: datetime | None = None) -> str:
    """上海时区下 now-days 的 %Y-%m-%d 日期串（字典序即时间序）。"""
    ref = now if now is not None else datetime.now(bitable_lark.SHANGHAI)
    return (ref - timedelta(days=days)).astimezone(bitable_lark.SHANGHAI).strftime("%Y-%m-%d")


def _pushed_date(fields: dict[str, Any]) -> str | None:
    """fields 「推送时间」→ 上海 %Y-%m-%d；为空回退「归档日期」（#198 存量行）。

    兼容 epoch 毫秒/ISO/纯日期；归档早于 mark_pushed 时推送时间为空，
    无回退则这些行永不进入保留窗口（F22 契约失效）。
    """
    raw = bitable_backfill._cell_str(fields.get("推送时间"))
    if raw:
        d = bitable_backfill._shanghai_date(raw)
        if d:
            return d
    arch = bitable_backfill._cell_str(fields.get("归档日期"))
    return bitable_backfill._shanghai_date(arch) if arch else None


def _list_records(
    app_token: str, table_id: str, env_name: str | None = None
) -> tuple[list[tuple[str, dict[str, Any]]], bool, bool]:
    """分页拉全表，返回 ([(record_id, fields)], 首屏成功, 全量读完)。

    兼容 records 包装与 fields+data 行式两种响应；中途页失败时已扫描记录
    仍返回（删除只针对其中过期者，未扫到的留待下轮巡检），complete=False，
    purge 依此不写入 meta 成功时间（#180）；env_name 非空时请求带出「环境」
    字段供调用方按环境过滤（#208）。
    """
    out: list[tuple[str, dict[str, Any]]] = []
    offset = 0
    first = True
    prev_fp = ""
    env_args = (
        ["--field-id", "环境", "--field-id", "推送时间", "--field-id", "归档日期"]
        if env_name is not None
        else []
    )
    while True:
        bitable_lark._guard_offset(offset)
        proc = bitable_lark._run(
            [
                "base", "+record-list",
                "--base-token", app_token,
                "--table-id", table_id,
                "--limit", str(_CHUNK),
                "--offset", str(offset),
                "--json",
                *env_args,
            ],
            timeout=120,
        )
        if not bitable_lark._ok(proc):
            if first:
                log.warning("purge：首屏 record-list 失败，跳过本次 bitable 清理")
                return [], False, False
            log.warning("purge：第 %d 页拉取失败，仅处理已扫描记录", offset // _CHUNK + 1)
            return out, True, False
        first = False
        data = bitable_lark._data(proc)
        prev_fp = bitable_lark._page_guard(prev_fp, data)
        records: list[dict[str, Any]] = data.get("records") or []
        if records:
            for rec in records:
                rid = str(rec.get("record_id") or rec.get("id") or rec.get("recordId") or "")
                fds = rec.get("fields") or rec.get("record") or {}
                out.append((rid, fds if isinstance(fds, dict) else {}))
            if len(records) < _CHUNK:
                return out, True, True
            offset += _CHUNK
            continue
        fields: list[Any] = data.get("fields") or []
        rows: list[Any] = data.get("data") or []
        if not fields or not rows:
            return out, True, True
        rids = data.get("record_ids") or data.get("recordIds") or data.get("ids") or []
        idx_push = fields.index("推送时间") if "推送时间" in fields else -1
        idx_arch = fields.index("归档日期") if "归档日期" in fields else -1
        idx_env = fields.index("环境") if "环境" in fields else -1
        for i, r in enumerate(rows):
            rid = str(rids[i] if i < len(rids) else "")
            if isinstance(r, dict):
                fds = r.get("fields") or r.get("values") or r
                out.append((rid, fds if isinstance(fds, dict) else {}))
            elif isinstance(r, list):
                vals: dict[str, Any] = {}
                for name, idx in (("推送时间", idx_push), ("归档日期", idx_arch), ("环境", idx_env)):
                    if 0 <= idx < len(r):
                        vals[name] = r[idx]
                if vals:
                    out.append((rid, vals))
        if len(rows) < _CHUNK:
            return out, True, True
        offset += _CHUNK


@dataclass(frozen=True)
class PurgeOutcome:
    deleted: int = 0
    expired: int = 0
    scanned: int = 0
    listed_ok: bool = False
    applied_ok: bool = False
    complete: bool = False

    @property
    def ok(self) -> bool:
        """首屏可读、全量分页读完且删除批全部成功才算整段成功。"""
        return self.listed_ok and self.applied_ok and self.complete


def purge_expired_records_outcome(
    app_token: str,
    table_id: str,
    cutoff_date: str,
    dry_run: bool = False,
    env_name: str | None = None,
) -> PurgeOutcome:
    """删除推送时间早于 cutoff_date（%Y-%m-%d 字典序比较）的记录。

    dry-run 时 deleted=0；首屏 list 失败返回全零且 listed_ok=False；
    env_name 非空（dev/test）时仅删「环境」匹配行，prod/None 不过滤（#208）。
    """
    pairs, list_ok, complete = _list_records(app_token, table_id, env_name)
    if not list_ok:
        return PurgeOutcome()
    expired: list[str] = []
    for rid, fds in pairs:
        if not rid:
            continue
        if env_name is not None and bitable_backfill._cell_str(fds.get("环境")) != env_name:
            continue
        d = _pushed_date(fds)
        if d and d < cutoff_date:
            expired.append(rid)
    if dry_run:
        log.info("purge dry-run：扫描 %d 条，过期 %d 条（未删除）", len(pairs), len(expired))
        return PurgeOutcome(
            0, len(expired), len(pairs), listed_ok=True, applied_ok=True, complete=complete
        )
    deleted = 0
    batch_ok = True
    for i in range(0, len(expired), _CHUNK):
        batch = expired[i : i + _CHUNK]
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
            log.warning("purge：第 %d 批删除失败（%d 条），终止", i // _CHUNK + 1, len(batch))
            batch_ok = False
            break
        deleted += len(batch)
    log.info("purge：扫描 %d 条，过期 %d 条，删除 %d 条", len(pairs), len(expired), deleted)
    return PurgeOutcome(
        deleted, len(expired), len(pairs), listed_ok=True, applied_ok=batch_ok, complete=complete
    )


def purge_expired_records(
    app_token: str, table_id: str, cutoff_date: str, dry_run: bool = False
) -> tuple[int, int, int]:
    """DESIGN §20.2 的 (deleted, expired, scanned) 视图（兼容既有调用）。"""
    outcome = purge_expired_records_outcome(app_token, table_id, cutoff_date, dry_run)
    return outcome.deleted, outcome.expired, outcome.scanned
