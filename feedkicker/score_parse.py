"""F40 打分契约解析与归一：闸门/否决/缺失归一/总分重算/分布校验（DESIGN §26.4/§26.6/§26.8）。"""

from __future__ import annotations

import logging
from typing import Any

from feedkicker.llm_json import load_json_obj as _load_json_obj
from feedkicker.score_report import WEIGHTS as _WEIGHTS
from feedkicker.score_report import DistCheck as DistCheck
from feedkicker.score_report import check_distribution as check_distribution
from feedkicker.score_report import dim as _dim
from feedkicker.score_report import num as _num
from feedkicker.score_report import truthy
from feedkicker.score_report import weighted_total as weighted_total
from feedkicker.score_source import name_key

log = logging.getLogger(__name__)

_MISSING = "缺失"
_REASON_LIMIT = 100


def _dims_ok(item: dict[str, Any]) -> bool:
    """`gate=pass` 六维严格校验：每维须为 0–5 数值（含 0.5 档）或字面量 `"缺失"`（DESIGN §26.4）。"""
    if _zero_gate(item.get("gate")):
        return True
    scores = item.get("dimensions")
    scores = scores if isinstance(scores, dict) else item.get("scores")
    scores = scores if isinstance(scores, dict) else {}
    for key in _WEIGHTS:
        raw = scores.get(key)
        if isinstance(raw, str) and raw.strip() == _MISSING:
            continue
        val = _num(raw)
        if val is None or not 0.0 <= val <= 5.0:
            return False
    return True


def parse_results(raw: str) -> tuple[list[dict[str, Any]], int]:
    """`parse_results_full` 的 `(合法项, dropped)` 视图（公开契约，#R9-09）。"""
    parsed, dropped, _keys = parse_results_full(raw)
    return parsed, dropped


def parse_results_full(raw: str) -> tuple[list[dict[str, Any]], int, set[str]]:
    """解析并返回 `(合法项, dropped, 被丢弃项的 name_key 集合)`（#R9-09 单点计数）。

    **权威口径 `scores`/`dimensions`/`gate: pass|zero`**（DESIGN §26.4，兼容回退 `results` 主键与
    `scores` 维度键）：非法 JSON / 顶层非对象 / 权威键非列表 → raise ValueError（调用方重试）；单条
    丢弃并计数：非对象 / 缺名 / 缺 `reason` / `gate=pass` 六维非 0–5 数值（含 0.5 档）或非 `"缺失"`。
    `dropped_keys` 供 `normalize_results` 避免把同一行二次计为「缺返回行」。
    """
    obj = _load_json_obj(raw)
    if obj is None:
        raise ValueError("LLM 输出不是合法 JSON 对象")
    items = obj.get("scores")
    if not isinstance(items, list):
        items = obj.get("results")
    if not isinstance(items, list):
        raise ValueError("scores 字段缺失或非列表")
    parsed: list[dict[str, Any]] = []
    dropped = 0
    dropped_keys: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            dropped += 1
            continue
        name = str(item.get("话题名称") or "").strip()
        if not name_key(name) or not str(item.get("reason") or "").strip() or not _dims_ok(item):
            dropped += 1
            if name_key(name):
                dropped_keys.add(name_key(name))
            continue
        parsed.append({**item, "话题名称": name})
    if dropped:
        log.warning("丢弃 %d 条非法 scores 元素（非对象/缺名/缺 reason/六维非法）", dropped)
    return parsed, dropped, dropped_keys


def parse_scores(raw: str) -> list[dict[str, Any]]:
    """`parse_results` 的列表视图（公开契约，dropped 由 `parse_results` 计数）。"""
    return parse_results(raw)[0]


def _missing_list(value: Any) -> list[str]:
    """`missing` 归一为 list：仅 list/tuple/set 采信；标量/字典等不可迭代值忽略并 WARNING（#R10-03）。

    防单条坏元素（`missing: 5`/`true`/`{}`）在 parse try 之外抛 TypeError 中止整轮。
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value]
    log.warning("missing 字段非可迭代（%s），按无缺失处理", type(value).__name__)
    return []


def _zero_gate(gate: Any) -> bool:
    if isinstance(gate, (int, float)) and not isinstance(gate, bool):
        return gate == 0
    return str(gate).strip().lower() in ("zero", "0", "0.0")


def _normalize_item(item: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    scores = item.get("dimensions")
    if not isinstance(scores, dict):
        scores = item.get("scores")
    scores = scores if isinstance(scores, dict) else {}
    total, missing = weighted_total(scores, _missing_list(item.get("missing")))
    pain, pain_missing = _dim(scores, "普适痛点强度")
    layer, layer_missing = _dim(scores, "分层承载力")
    veto = (not pain_missing and pain == 0) or (not layer_missing and layer == 0)
    if _zero_gate(item.get("gate")) or veto:
        total = 0.0
    model_total = _num(item.get("weighted_total"))
    if total is not None and model_total is not None and abs(total - model_total) > 0.1:
        log.warning("话题 %s 总分重算=%s 与模型值=%s 偏差 >0.1", item.get("话题名称"), total, model_total)
    reason = str(item.get("reason") or "").strip()
    if len(reason) > _REASON_LIMIT:
        log.warning("话题 %s 理由超 %d 字，截断", item.get("话题名称"), _REASON_LIMIT)
        reason = reason[: _REASON_LIMIT - 1] + "…"
    return {
        "record_id": row.get("record_id") or "",
        "话题名称": str(item.get("话题名称") or "").strip(),
        "gate": item.get("gate"),
        "scores": scores,
        "weighted_total": total,
        "missing": missing,
        "打分": row.get("打分"),
        "risk_flag": truthy(item.get("risk_flag")),
        "source_flag": truthy(item.get("source_flag")),
        "reason": reason,
        "rescue": str(item.get("rescue") or "").strip(),
    }


def normalize_results(
    items: list[dict[str, Any]], rows: list[dict[str, Any]], dropped_keys: Any = ()
) -> tuple[list[dict[str, Any]], DistCheck, int]:
    """回填 `record_id` 并归类 → `(归一列表, 分布校验, dropped)`：**以名匹配为主**，`record_id` 仅在同一
    `name_key` 命中集合内消歧（防模型乱填 id 命中他行）；同名多行消费式匹配（#374）；未匹配/未返回均计 dropped。
    """
    by_name: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = name_key(row.get("话题名称"))
        if key:
            by_name.setdefault(key, []).append(row)
    normalized: list[dict[str, Any]] = []
    dropped = 0
    used: set[int] = set()
    for item in items:
        rid = str(item.get("record_id") or "")
        bucket = by_name.get(name_key(item.get("话题名称")), [])
        row = next((c for c in bucket if rid and str(c.get("record_id") or "") == rid and id(c) not in used), None)
        if row is None and rid and bucket:
            log.warning("scores record_id=%s 与话题名不匹配，降级按名匹配", rid)
        if row is None:
            row = next((c for c in bucket if id(c) not in used), None)
        if row is None:
            dropped += 1
            log.warning("scores 多余项（表内无匹配行，忽略）：%s", item.get("话题名称"))
            continue
        used.add(id(row))
        normalized.append(_normalize_item(item, row))
    for row in rows:
        if id(row) in used:
            continue
        if name_key(row.get("话题名称")) in set(dropped_keys):
            continue
        dropped += 1
        log.warning("表内话题未被返回（缺返回行）：%s", row.get("话题名称"))
    return normalized, check_distribution(normalized), dropped


def normalize(
    items: list[dict[str, Any]], rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], DistCheck]:
    normalized, dist, _ = normalize_results(items, rows)
    return normalized, dist
