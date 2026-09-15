"""F40 打分契约解析与归一：闸门/否决/缺失归一/总分重算/分布校验（DESIGN §26.4/§26.6/§26.8）。"""

from __future__ import annotations

import logging
from typing import Any

from feedkicker.extract_parse import _load_json_obj
from feedkicker.score_report import DistCheck as DistCheck
from feedkicker.score_report import check_distribution as check_distribution
from feedkicker.score_source import name_key

log = logging.getLogger(__name__)

_WEIGHTS: dict[str, int] = {
    "普适痛点强度": 28, "分层承载力": 22, "可演示性": 20,
    "时效与稀缺": 12, "内容复用价值": 10, "讲解成本": 8,
}
_MISSING = "缺失"
_REASON_LIMIT = 100


def parse_results(raw: str) -> tuple[list[dict[str, Any]], int]:
    """解析 LLM 原始文本 → `(scores, dropped)`，**权威口径 `scores`/`dimensions`/`gate: pass|zero`**
    （DESIGN §26.4，兼容回退 `results` 主键与 `scores` 维度键）：非法 JSON / 顶层非对象 / 权威键非列表
    → raise ValueError（调用方重试）；单条丢弃并计数：非对象 / 缺名 / 缺 `reason` / `gate=pass` 六维非
    0–5 数值（含 0.5 档）或非 `"缺失"`（含越界/非数字/bool/乱字符串）。
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
        if not name_key(name) or not str(item.get("reason") or "").strip() or not _dims_ok(item):
            dropped += 1
            continue
        parsed.append({**item, "话题名称": name})
    if dropped:
        log.warning("丢弃 %d 条非法 scores 元素（非对象/缺名/缺 reason/六维非法）", dropped)
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
    raw = scores.get(key)
    if raw is None or (isinstance(raw, str) and raw.strip() == _MISSING):
        return None, True
    val = _num(raw)
    return (val, False) if val is not None else (None, True)


def weighted_total(
    scores: dict[str, Any], missing_extra: Any = ()
) -> tuple[float | None, list[str]]:
    """缺失维剔除权重、其余按剩余权重归一后加权 round 到 1 位小数；全维缺失 → `(None, 全维)`；
    `missing_extra` 与「值为 `"缺失"`」两种缺失表达一并归一（PRD §22.6）。"""
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
    """回填 `record_id` 并归类 → `(归一列表, 分布校验, dropped)`：优先用返回值 `record_id`，否则按
    `name_key` 消费式匹配所有同名未匹配行（使表内多行同名都能写到，#374）；未匹配/未返回均计 dropped。
    """
    by_id: dict[str, dict[str, Any]] = {}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        rid = str(row.get("record_id") or "")
        if rid and rid not in by_id:
            by_id[rid] = row
        key = name_key(row.get("话题名称"))
        if key:
            by_name.setdefault(key, []).append(row)
    normalized: list[dict[str, Any]] = []
    dropped = 0
    used: set[int] = set()
    for item in items:
        rid = str(item.get("record_id") or "")
        row = by_id.get(rid) if rid else None
        if row is None or id(row) in used:
            bucket = by_name.get(name_key(item.get("话题名称")), [])
            row = next((c for c in bucket if id(c) not in used), None)
        if row is None:
            dropped += 1
            log.warning("scores 多余项（表内无匹配行，忽略）：%s", item.get("话题名称"))
            continue
        used.add(id(row))
        normalized.append(_normalize_item(item, row))
    for row in rows:
        if id(row) not in used:
            dropped += 1
            log.warning("表内话题未被返回（缺返回行）：%s", row.get("话题名称"))
    return normalized, check_distribution(normalized), dropped


def normalize(
    items: list[dict[str, Any]], rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], DistCheck]:
    normalized, dist, _ = normalize_results(items, rows)
    return normalized, dist
