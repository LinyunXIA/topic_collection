"""F40 打分契约解析与归一：闸门/否决/缺失归一/总分重算/分布校验（DESIGN §26.4/§26.6/§26.8）。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from feedkicker.extract_parse import _load_json_obj, topic_key

log = logging.getLogger(__name__)

_WEIGHTS: dict[str, int] = {
    "普适痛点强度": 28, "分层承载力": 22, "可演示性": 20,
    "时效与稀缺": 12, "内容复用价值": 10, "讲解成本": 8,
}
_MISSING = "缺失"
_REASON_LIMIT = 100
_GE4_MAX = 0.20
_LT2_MIN = 0.15


@dataclass
class DistCheck:
    total: int
    ge4_ratio: float
    lt2_ratio: float
    violations: list[str] = field(default_factory=list)


def parse_results(raw: str) -> tuple[list[dict[str, Any]], int]:
    """解析 LLM 原始文本 → `(scores, dropped)`，**权威口径 `scores` 数组 / `dimensions` 六维 /
    `gate: "pass"|"zero"`（DESIGN §26.4）**；兼容回退 `results` 主键与 `scores` 维度键。

    先经 `_load_json_obj` 剥离 thinking 推理块并容忍 JSON 围栏/前后说明；非法 JSON / 顶层非对象 /
    权威键非列表 → raise ValueError（调用方重试后计失败批）；单条非对象/缺名/空名丢弃计数，不整批弃。
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
    for item in items:
        if not isinstance(item, dict):
            dropped += 1
            continue
        name = str(item.get("话题名称") or "").strip()
        if not topic_key(name):
            dropped += 1
            continue
        parsed.append({**item, "话题名称": name})
    if dropped:
        log.warning("丢弃 %d 条非法 scores 元素（非对象/缺名/空名）", dropped)
    return parsed, dropped


def parse_scores(raw: str) -> list[dict[str, Any]]:
    """`parse_results` 的列表视图（公开契约，dropped 由 `parse_results` 计数）。"""
    return parse_results(raw)[0]


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _dim(scores: dict[str, Any], key: str) -> tuple[float | None, bool]:
    """返回 `(值, 是否缺失)`；`"缺失"`/缺键/非数字一律按缺失（不填 0，PRD §22.6）。"""
    raw = scores.get(key)
    if raw is None or (isinstance(raw, str) and raw.strip() == _MISSING):
        return None, True
    val = _num(raw)
    return (val, False) if val is not None else (None, True)


def weighted_total(
    scores: dict[str, Any], missing_extra: Any = ()
) -> tuple[float | None, list[str]]:
    """缺失维剔除权重、其余按剩余权重归一后加权，round 到 1 位小数；全维缺失 → `(None, 全维)`；
    `missing_extra`（模型 `missing` 列表）与「值为 `"缺失"`」两种缺失表达一并归一。"""
    extra = {str(k) for k in missing_extra}
    present: list[tuple[str, float]] = []
    missing: list[str] = []
    for key in _WEIGHTS:
        val, is_missing = _dim(scores, key)
        if is_missing or val is None or key in extra:
            missing.append(key)
        else:
            present.append((key, val))
    if not present:
        return None, missing
    total_w = sum(_WEIGHTS[k] for k, _ in present)
    acc = sum(_WEIGHTS[k] * v for k, v in present)
    return round(acc / total_w, 1), missing


def _zero_gate(gate: Any) -> bool:
    if isinstance(gate, (int, float)) and not isinstance(gate, bool):
        return gate == 0
    return str(gate).strip().lower() in ("zero", "0", "0.0")


def _normalize_item(item: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    scores = item.get("dimensions")
    if not isinstance(scores, dict):
        scores = item.get("scores")
    scores = scores if isinstance(scores, dict) else {}
    total, missing = weighted_total(scores, item.get("missing") or ())
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
        reason = reason[:_REASON_LIMIT] + "…"
    if item.get("risk_flag") and "risk" not in reason:
        reason += "｜risk"
    if item.get("source_flag") and "source" not in reason:
        reason += "｜source"
    return {
        "record_id": row.get("record_id") or "",
        "话题名称": str(item.get("话题名称") or "").strip(),
        "gate": item.get("gate"),
        "scores": scores,
        "weighted_total": total,
        "missing": missing,
        "打分": row.get("打分"),
        "risk_flag": bool(item.get("risk_flag")),
        "source_flag": bool(item.get("source_flag")),
        "reason": reason,
        "rescue": str(item.get("rescue") or "").strip(),
    }


def normalize_results(
    items: list[dict[str, Any]], rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], DistCheck, int]:
    """回填 `record_id` 并归类 → `(归一列表, 分布校验, dropped)`；按 `topic_key` NFKC 归一匹配输入行，
    匹配不到的返回行（多余）与未被返回的输入行（缺返回）均计 dropped 并 WARNING（§26.8）。"""
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = topic_key(row.get("话题名称"))
        if key and key not in by_key:
            by_key[key] = row
    normalized: list[dict[str, Any]] = []
    dropped = 0
    matched: set[str] = set()
    for item in items:
        key = topic_key(item.get("话题名称"))
        row = by_key.get(key)
        if row is None:
            dropped += 1
            log.warning("scores 多余项（表内无此话题，忽略）：%s", item.get("话题名称"))
            continue
        matched.add(key)
        normalized.append(_normalize_item(item, row))
    for key in set(by_key) - matched:
        dropped += 1
        log.warning("表内话题未被返回（缺返回行）：%s", by_key[key].get("话题名称"))
    return normalized, check_distribution(normalized), dropped


def normalize(
    items: list[dict[str, Any]], rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], DistCheck]:
    """`normalize_results` 的二元视图（公开契约，dropped 由 `normalize_results` 计数）。"""
    normalized, dist, _ = normalize_results(items, rows)
    return normalized, dist


def check_distribution(items: list[dict[str, Any]]) -> DistCheck:
    """按 PRD §22.7 校验 `≥4.0 ≤20%` 与 `<2.0 ≥15%`；仅收集 violation，不自动调分。"""
    scored = [i for i in items if i.get("weighted_total") is not None]
    if not scored:
        return DistCheck(0, 0.0, 0.0, [])
    total = len(scored)
    ge4 = sum(1 for i in scored if i["weighted_total"] >= 4.0) / total
    lt2 = sum(1 for i in scored if i["weighted_total"] < 2.0) / total
    violations: list[str] = []
    if ge4 > _GE4_MAX:
        violations.append(f"≥4.0 占比 {ge4:.0%} 超过 20%")
    if lt2 < _LT2_MIN:
        violations.append(f"<2.0 占比 {lt2:.0%} 少于 15%")
    return DistCheck(total=total, ge4_ratio=ge4, lt2_ratio=lt2, violations=violations)
