"""F41 打分写入与幂等：MMax/DS 列映射 + 只补空/--force + 批量写（DESIGN §26.5/§26.7）。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from feedkicker import bitable_lark

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


def _cell(item: dict[str, Any], label: str) -> dict[str, str]:
    """单条记录的目标 2 列：`{label}打分` 纯数字（全维缺失写「缺失」）+ 单行 `{label}理由`。"""
    scores = item.get("scores")
    scores = scores if isinstance(scores, dict) else {}
    dims = "/".join(f"{_SHORT_DIM[k]}{_dim_text(scores.get(k))}" for k in _SHORT_DIM)
    total = item.get("weighted_total")
    score = _MISSING if total is None else f"{float(total):.1f}"
    risk = "true" if item.get("risk_flag") else "false"
    source = "true" if item.get("source_flag") else "false"
    reason = str(item.get("reason") or "").strip()
    detail = f"{reason} ｜ risk={risk} ｜ source={source} ｜ 六维：{dims}"
    return {f"{label}打分": score, f"{label}理由": detail}


def _batch_write(conf: WriteConf, chunk: list[dict[str, Any]], label: str) -> bool:
    payload = {"update_records": {str(r.get("record_id") or ""): _cell(r, label) for r in chunk}}
    with bitable_lark._json_arg(payload) as (jflag, jval):
        proc = bitable_lark._run(
            ["base", "+record-batch-update", "--base-token", conf.app_token,
             "--table-id", conf.table_id, jflag, jval],
            timeout=300,
        )
    return bitable_lark._ok(proc)


def _single_write(conf: WriteConf, row: dict[str, Any], label: str) -> bool:
    with bitable_lark._json_arg(_cell(row, label)) as (jflag, jval):
        proc = bitable_lark._run(
            ["base", "+record-update", "--base-token", conf.app_token, "--table-id", conf.table_id,
             "--record-id", str(row.get("record_id") or ""), jflag, jval],
            timeout=120,
        )
    return bitable_lark._ok(proc)


def write_scores(conf: WriteConf, rows: list[dict[str, Any]], *, dry_run: bool) -> ScoreStats:
    """分块（≤100/批）只更新目标 2 列；失败批计入 `failed_writes`，**绝不触碰其它列**（§26.5）。

    `dry_run=True` 零写调用；`+record-batch-update` 缺失时回退逐条 `+record-update`（§26.6 写失败只计数）。
    """
    stats = ScoreStats(scored=len(rows))
    if dry_run or not rows:
        return stats
    verb = bitable_lark._has_batch_verb()
    if verb is None:
        raise RuntimeError("lark-cli 无 +record-batch-update/+record-update，无法写表")
    label = PROVIDER_LABELS[conf.provider]
    for i in range(0, len(rows), WRITE_CHUNK):
        chunk = rows[i : i + WRITE_CHUNK]
        if verb == "+record-batch-update":
            if _batch_write(conf, chunk, label):
                stats.written += len(chunk)
            else:
                log.warning("打分批量写入失败（第 %d 批 %d 条）", i // WRITE_CHUNK + 1, len(chunk))
                stats.failed_writes += len(chunk)
            continue
        failed = 0
        for row in chunk:
            if _single_write(conf, row, label):
                stats.written += 1
            else:
                failed += 1
        if failed:
            log.warning("打分逐条写入失败 %d 条", failed)
            stats.failed_writes += failed
    return stats
