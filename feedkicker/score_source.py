"""F38 沙龙话题清单读取与组批：全表分页读 5 输入字段 + 恒 ≤MAX_SCORE_BATCH 组批（DESIGN §26.2）。"""

from __future__ import annotations

import logging
from typing import Any

from feedkicker import bitable_lark
from feedkicker.config_models import MAX_SCORE_BATCH as MAX_SCORE_BATCH
from feedkicker.topic_records import _extract_records

log = logging.getLogger(__name__)

SCORE_FIELDS = ("话题名称", "可使用工具", "相关AI原理", "资讯链接", "出处来源")


def _normalize(record: dict[str, Any]) -> dict[str, Any]:
    """行归一：仅保留 SCORE_FIELDS 与 `record_id`（其余列丢弃，F41 按 record_id 定位既有行）。"""
    fields = record.get("fields")
    fields = fields if isinstance(fields, dict) else {}
    row: dict[str, Any] = {"record_id": record.get("record_id") or ""}
    for name in SCORE_FIELDS:
        row[name] = fields.get(name)
    return row


def read_rows(app_token: str, table_id: str, limit: int = 0) -> list[dict[str, Any]]:
    """分页读目标表全表行并归一到 5 输入字段；`limit>0` 最多 N 行，`limit=0` 全部。

    经 lark-cli `base +record-list`，复用 `bitable_lark` 的 offset/页数/页指纹三重兜底与
    `_extract_records` 解析（不可识别响应 raise RuntimeError，#326）；rc/业务失败即 raise，
    不得把「读半张表」当成功（对齐 `extract_write.existing_index`）。
    """
    if not app_token or not table_id:
        raise ValueError("app_token 与 table_id 均不能为空")
    rows: list[dict[str, Any]] = []
    offset = 0
    prev_fp = ""
    while True:
        bitable_lark._guard_offset(offset)
        bitable_lark.guard_pages(offset // bitable_lark._CHUNK + 1)
        args = ["base", "+record-list", "--base-token", app_token, "--table-id", table_id]
        for name in SCORE_FIELDS:
            args += ["--field-id", name]
        args += ["--limit", str(bitable_lark._CHUNK), "--offset", str(offset), "--json"]
        proc = bitable_lark._run(args, timeout=120)
        if not bitable_lark._ok(proc):
            raise RuntimeError("拉取沙龙话题清单失败，中止打分以避免读半张表")
        data = bitable_lark._data(proc)
        prev_fp = bitable_lark._page_guard(prev_fp, data)
        records = _extract_records(data)
        rows.extend(_normalize(rec) for rec in records)
        if limit and len(rows) >= limit:
            return rows[:limit]
        if len(records) < bitable_lark._CHUNK:
            break
        offset += bitable_lark._CHUNK
    return rows


def group_batches(
    rows: list[dict[str, Any]], size: int = MAX_SCORE_BATCH
) -> list[list[dict[str, Any]]]:
    """按 `size` 切批，单批恒 ≤MAX_SCORE_BATCH；空输入返回 []（DESIGN §26.8 批上限）。"""
    width = min(max(1, size), MAX_SCORE_BATCH)
    return [rows[i : i + width] for i in range(0, len(rows), width)]


def plan_pending(
    rows: list[dict[str, Any]], provider: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(待打分, 跳过) 规划：F38 未读既有分，全部行待打分、跳过集为空。

    F41 将按 provider 目标列（MMax/DS 打分列）读既有值实现「只补空」；本骨架先保留同形
    接口，使 dry-run 清单与后续写入规划同源。
    """
    log.debug("F38 骨架：provider=%s 既有分跳过统计留待 F41，当前全部视为待打分", provider)
    return list(rows), []
