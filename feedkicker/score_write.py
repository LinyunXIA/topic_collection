"""F41 打分写入与幂等：MMax/DS 列映射 + 只补空/--force + 批量写（DESIGN §26.5/§26.7）。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from feedkicker import bitable_lark
from feedkicker.score_report import truthy

log = logging.getLogger(__name__)

WRITE_CHUNK = 100

PROVIDER_LABELS: dict[str, str] = {"minimax": "MMax", "deepseek": "DS"}

_MISSING = "缺失"

_SHORT_DIM: dict[str, str] = {
    "普适痛点强度": "普适痛点", "分层承载力": "分层承载", "可演示性": "可演示",
    "时效与稀缺": "时效稀缺", "内容复用价值": "复用价值", "讲解成本": "讲解成本",
}


@dataclass
class WriteConf:
    app_token: str
    table_id: str
    provider: str


@dataclass
class ScoreStats:
    rows: int = 0
    scored: int = 0
    skipped: int = 0
    written: int = 0
    failed_writes: int = 0
    failed_batches: int = 0
    dropped: int = 0
    empty_batches: int = 0


def _non_empty(value: Any) -> bool:
    if isinstance(value, list):
        return any(str(v).strip() for v in value)
    return value is not None and bool(str(value).strip())


def plan_writes(
    scored: list[dict[str, Any]], *, force: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(to_write, skipped)：默认只补空（`打分` 已有非空值 → skipped），`force=True` 全量重算（§26.7）。"""
    if force:
        return list(scored), []
    to_write: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in scored:
        (skipped if _non_empty(item.get("打分")) else to_write).append(item)
    return to_write, skipped


def _dim_text(value: Any) -> str:
    if value is None or (isinstance(value, str) and value.strip() == _MISSING):
        return _MISSING
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return _MISSING


def _dedupe(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """按 `record_id` 去重，返回 `(去重后行, 空 id 行数)`；重复 id 去重避免 `written` 虚高（#374）。

    空 `record_id` 行不再静默剔除，而是返回计数由调用方计入 `failed_writes`（#R10-04）：
    读层 schema 漂移致全表空 id 时必须非零退出，不得假成功。
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    empty = 0
    for row in rows:
        rid = str(row.get("record_id") or "")
        if not rid:
            empty += 1
        elif rid not in seen:
            seen.add(rid)
            out.append(row)
    return out, empty


def _cell(item: dict[str, Any], label: str) -> dict[str, str]:
    """单条记录的目标 2 列：`{label}打分` 纯数字（全维缺失写「缺失」）+ 单行 `{label}理由`。

    六维渲染以**归一后的 `missing` 集合**为准：列入 missing 的维一律输出「缺失」，即使模型给了
    数值，保证与 `weighted_total`（缺失维剔除权重）口径一致（#375）。
    """
    scores = item.get("scores")
    scores = scores if isinstance(scores, dict) else {}
    missing_raw = item.get("missing")
    missing = {str(k) for k in missing_raw} if isinstance(missing_raw, (list, tuple, set)) else set()
    dims = "/".join(
        f"{_SHORT_DIM[k]}{_MISSING if k in missing else _dim_text(scores.get(k))}" for k in _SHORT_DIM
    )
    total = item.get("weighted_total")
    score = _MISSING if total is None else f"{float(total):.1f}"
    risk = "true" if truthy(item.get("risk_flag")) else "false"
    source = "true" if truthy(item.get("source_flag")) else "false"
    reason = str(item.get("reason") or "").strip()
    detail = f"{reason} ｜ risk={risk} ｜ source={source} ｜ 六维：{dims}"
    return {f"{label}打分": score, f"{label}理由": detail}


def _batch_write(conf: WriteConf, chunk: list[dict[str, Any]], label: str) -> int:
    """`+record-batch-update`（`--json` 体 = help 的 `{"update_records": {record_id: fields}}`）。

    返回**实际 payload 键数**（成功）或 0（失败），供 `written` 如实计数（#374）。
    """
    payload = {"update_records": {str(r.get("record_id") or ""): _cell(r, label) for r in chunk}}
    with bitable_lark._json_arg(payload) as (jflag, jval):
        proc = bitable_lark._run(
            ["base", "+record-batch-update", "--base-token", conf.app_token,
             "--table-id", conf.table_id, jflag, jval],
            timeout=300,
        )
    return len(payload["update_records"]) if bitable_lark._ok(proc) else 0


def write_scores(conf: WriteConf, rows: list[dict[str, Any]], *, dry_run: bool) -> ScoreStats:
    """分块（≤100/批）只更新目标 2 列；失败批计入 `failed_writes`，**绝不触碰其它列**（§26.5）。

    `dry_run=True` 零写调用；写动词只用真实存在的 `+record-batch-update`（单条亦走此批接口，1..100
    通用），缺少该动词即 raise（旧 `+record-update` 不存在，死分支已删除，#R9-15）。
    """
    stats = ScoreStats()
    rows, empty_ids = _dedupe(rows)
    stats.scored = len(rows)
    if empty_ids:
        stats.failed_writes += empty_ids
        log.warning("打分写入：%d 行缺 record_id，无法定位行，计入写入失败", empty_ids)
    if dry_run or not rows:
        return stats
    if bitable_lark._has_batch_verb() is None:
        raise RuntimeError("lark-cli 无 +record-batch-update，无法写表")
    label = PROVIDER_LABELS[conf.provider]
    for i in range(0, len(rows), WRITE_CHUNK):
        chunk = rows[i : i + WRITE_CHUNK]
        written = _batch_write(conf, chunk, label)
        stats.written += written
        if written < len(chunk):
            log.warning("打分批量写入失败（第 %d 批 %d 条）", i // WRITE_CHUNK + 1, len(chunk))
            stats.failed_writes += len(chunk) - written
    return stats
