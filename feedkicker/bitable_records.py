from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from feedkicker import bitable_lark, bitable_schema
from feedkicker.bitable_reseed import purge_all_records as purge_all_records
from feedkicker.fetch import canonicalize, utc_now_iso

log = logging.getLogger(__name__)


def _cell(
    item: dict[str, Any], now_iso: str | None = None, env_name: str | None = None
) -> dict[str, Any]:
    def fmt_dt(iso: str | None) -> str | None:
        if not iso:
            return None
        try:
            dt = datetime.fromisoformat(iso).astimezone(bitable_lark.SHANGHAI)
        except ValueError:
            return None
        return dt.strftime("%Y-%m-%d %H:%M")

    cell = {
        "标题": item.get("title") or item.get("url") or "",
        "链接": item.get("url") or "",
        "来源": item.get("feed_id") or "",
        "摘要": item.get("description") or "",
        "发布时间": fmt_dt(item.get("published_at")),
        "推送时间": fmt_dt(item.get("pushed_at")) or fmt_dt(now_iso),
        "归档日期": (fmt_dt(item.get("pushed_at")) or fmt_dt(now_iso) or fmt_dt(item.get("first_seen")) or "")[:10],
    }
    if env_name:
        cell["环境"] = env_name
    return cell


def existing_links(app_token: str, table_id: str) -> set[str]:
    """拉取表内全部已有链接（分页）。

    拉不到已有链接集合时必须中止：返回空集会让全量被当新记录写入，造成重复行。
    兼容 records 包装与 fields+data 行式两种形态；其余形态一律抛错（A3）。
    不按「环境」过滤：dev/test 共享 Base 下跨环境 URL 也去重，test 视图可能缺行（非丢失，#229）。
    """
    links: set[str] = set()
    offset = 0
    prev_fp = ""
    while True:
        bitable_lark._guard_offset(offset)
        proc = bitable_lark._run(
            [
                "base", "+record-list",
                "--base-token", app_token,
                "--table-id", table_id,
                "--field-id", "链接",
                "--limit", "200",
                "--offset", str(offset),
                "--json",
            ],
            timeout=120,
        )
        if not bitable_lark._ok(proc):
            raise RuntimeError("拉取多维表格已有链接失败，中止本次同步以避免重复写入")
        data = bitable_lark._data(proc)
        if not isinstance(data, dict):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise RuntimeError(f"多维表格已有链接响应不是 JSON 对象，中止本次同步: {str(data)[:200]}")
        prev_fp = bitable_lark._page_guard(prev_fp, data)
        records = data.get("records")
        if isinstance(records, list):
            for rec in records:
                fds = rec.get("fields") or rec.get("record") or {}
                v = fds.get("链接")
                v = v.get("link") if isinstance(v, dict) else v
                if v:
                    links.add(canonicalize(v))
            if len(records) < bitable_lark._CHUNK:
                break
            offset += bitable_lark._CHUNK
            continue
        fields = data.get("fields")
        rows = data.get("data")
        if not isinstance(fields, list) or not isinstance(rows, list):
            raise RuntimeError(f"多维表格已有链接响应无法识别，中止本次同步: {str(data)[:200]}")
        if "链接" not in fields:
            break
        i_link = fields.index("链接")
        for r in rows:
            v = r[i_link]
            v = v.get("link") if isinstance(v, dict) else v
            if v:
                links.add(canonicalize(v))
        if len(rows) < 200:
            break
        offset += 200
    return links


def sync_records(
    app_token: str,
    table_id: str,
    items: list[dict[str, Any]],
    env_name: str | None = None,
    now_iso: str | None = None,
) -> bool:
    if not items:
        return True
    if now_iso is None:
        now_iso = utc_now_iso()
    seen_links = existing_links(app_token, table_id)
    picked: list[dict[str, Any]] = []
    batch_seen: set[str] = set()
    skipped = 0
    for it in items:
        key = canonicalize(it.get("url") or "")
        if key and (key in seen_links or key in batch_seen):
            skipped += 1
            continue
        if key:
            batch_seen.add(key)
        picked.append(it)
    if skipped:
        log.info("跨源/已存在去重跳过 %d 条", skipped)
    if not picked:
        log.info("全部条目已存在于表内，无需写入")
        return True
    total_ok = True
    for i in range(0, len(picked), bitable_lark._CHUNK):
        chunk = picked[i : i + bitable_lark._CHUNK]
        payload = {"create_records": [_cell(it, now_iso, env_name) for it in chunk]}
        with bitable_lark._json_arg(payload) as (jflag, jval):
            proc = bitable_lark._run(
                [
                    "base", "+record-batch-create",
                    "--base-token", app_token,
                    "--table-id", table_id,
                    jflag, jval,
                ],
                timeout=300,
            )
        if not bitable_lark._ok(proc):
            total_ok = False
            log.warning("Bitable 批量写入失败（第 %d 批 %d 条）", i // bitable_lark._CHUNK + 1, len(chunk))
    return total_ok


def sync_env(bt, app_env: str, conn, now_iso=None) -> int:
    from feedkicker import store

    info = bitable_schema.ensure_initialized(bt, app_env)
    if not info["app_token"] or not info["table_id"]:
        raise RuntimeError(f"Base 初始化不完整: {info}")
    unsynced = store.select_unsynced(conn)
    log.info("多维表格待同步 %d 条 → %s", len(unsynced), info["url"])
    if not unsynced:
        return 0
    env_name = app_env if app_env in ("dev", "test") else None
    ok = sync_records(info["app_token"], info["table_id"], unsynced, env_name=env_name, now_iso=now_iso)
    if not ok:
        raise RuntimeError("存在未成功的批次，保留待重试")
    store.mark_synced(conn, unsynced, now_iso or utc_now_iso())
    return len(unsynced)
