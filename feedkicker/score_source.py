"""F38 沙龙话题清单读取与组批：全表分页读 5 输入字段 + 恒 ≤MAX_SCORE_BATCH 组批（DESIGN §26.2）。"""

from __future__ import annotations

import logging
from typing import Any

from feedkicker import bitable_lark
from feedkicker.config_models import MAX_SCORE_BATCH as MAX_SCORE_BATCH
from feedkicker.extract_parse import topic_key
from feedkicker.topic_records import _extract_records

log = logging.getLogger(__name__)

SCORE_FIELDS = ("话题名称", "可使用工具", "相关AI原理", "资讯链接", "出处来源")

NAME_LIMIT = 200


def name_text(name: Any) -> str:
    """话题名展示归一：折叠空白 + 截断到 NAME_LIMIT（注入展示名与匹配键同源，#377）。"""
    return " ".join(str(name or "").split())[:NAME_LIMIT]


def name_key(name: Any) -> str:
    """话题名匹配键：折叠空白后 `topic_key`，**不截断**（前 200 字相同、后缀不同者不得判同名，#377 补遗）。"""
    return topic_key(" ".join(str(name or "").split()))


def display_key(name: Any) -> str:
    """展示名匹配键：`name_text`（折叠 + ≤200 截断）后 `topic_key`；仅作「模型回显截断名」兜底匹配。"""
    return topic_key(name_text(name))


def _normalize(record: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    """行归一：5 输入字段按名保留，`*打分`/`*理由` 归一到统一键 `打分`/`理由`（不带 provider 名）。

    统一键便于横向上文与 F41 写入，不把 provider（MMax/DS）写进行键。
    """
    raw = record.get("fields")
    raw = raw if isinstance(raw, dict) else {}
    row: dict[str, Any] = {"record_id": record.get("record_id") or ""}
    for name in fields:
        if name.endswith("打分"):
            row["打分"] = raw.get(name)
        elif name.endswith("理由"):
            row["理由"] = raw.get(name)
        else:
            row[name] = raw.get(name)
    return row


def read_rows(
    app_token: str, table_id: str, limit: int = 0, fields: tuple[str, ...] | None = None
) -> list[dict[str, Any]]:
    """分页读目标表全表行并归一；`limit>0` 最多 N 行，`limit=0` 全部。

    `fields` 默认取 5 输入字段；传 `(*SCORE_FIELDS, "{label}打分", "{label}理由")` 时额外读目标
    两列并归一到统一键 `打分`/`理由`。经 lark-cli `base +record-list`，复用 `bitable_lark` 的
    offset/页数/页指纹三重兜底与 `_extract_records` 解析（不可识别响应 raise RuntimeError，参见 #326）；
    rc/业务失败即 raise，不得把「读半张表」当成功（对齐 `extract_write.existing_index`）。
    """
    if not app_token or not table_id:
        raise ValueError("app_token 与 table_id 均不能为空")
    projection = fields or SCORE_FIELDS

    def fetch(offset: int):
        args = ["base", "+record-list", "--base-token", app_token, "--table-id", table_id]
        for name in projection:
            args += ["--field-id", name]
        args += ["--limit", str(bitable_lark._CHUNK), "--offset", str(offset), "--json"]
        return bitable_lark._run(args, timeout=120)

    rows: list[dict[str, Any]] = []
    for proc, data in bitable_lark.iter_record_pages(fetch):
        if not bitable_lark._ok(proc):
            raise RuntimeError("拉取沙龙话题清单失败，中止打分以避免读半张表")
        records = _extract_records(data)
        rows.extend(_normalize(rec, projection) for rec in records)
        if limit and len(rows) >= limit:
            return rows[:limit]
        if len(records) < bitable_lark._CHUNK:
            break
    return rows


def group_batches(
    rows: list[dict[str, Any]], size: int = MAX_SCORE_BATCH
) -> list[list[dict[str, Any]]]:
    """按 `size` 切批，单批恒 ≤MAX_SCORE_BATCH；空输入返回 []（DESIGN §26.8 批上限）。"""
    width = min(max(1, size), MAX_SCORE_BATCH)
    return [rows[i : i + width] for i in range(0, len(rows), width)]


def _has_score(row: dict[str, Any]) -> bool:
    """已有分数 = `打分` 列非空（**仅看打分列**）；`理由` 有值但 `打分` 空视为未完成，须重算补齐两列。"""
    value = row.get("打分")
    if isinstance(value, list):
        return any(str(v).strip() for v in value)
    return value is not None and bool(str(value).strip())


def plan_pending(
    rows: list[dict[str, Any]], provider: str, force: bool = False
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(待打分, 跳过)：默认只补空（**已有 `打分` 的行跳过**），`force` 时全量重算。

    跳过判据仅看打分列（DESIGN §26.7）：`理由` 有值但 `打分` 空的行视为未完成，重新打分并
    补齐两列，自愈部分写入失败。跳过行即横向上文来源；F41 完成写入后重复 `--apply` 写入数=0
    的幂等自然成立。
    """
    if force:
        return list(rows), []
    pending: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in rows:
        (skipped if _has_score(row) else pending).append(row)
    log.debug("provider=%s 待打分=%d 跳过（打分列非空）=%d", provider, len(pending), len(skipped))
    return pending, skipped
