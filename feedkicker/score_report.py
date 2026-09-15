"""F40 dry-run 清单、分布校验与运行摘要打印（自 score_flow 拆出，DESIGN §26.2/§26.6）。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

_REASON_CLIP = 60
_GE4_MAX = 0.20
_LT2_MIN = 0.15

WEIGHTS: dict[str, int] = {
    "普适痛点强度": 28, "分层承载力": 22, "可演示性": 20,
    "时效与稀缺": 12, "内容复用价值": 10, "讲解成本": 8,
}
MISSING = "缺失"


def num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def dim(scores: dict[str, Any], key: str) -> tuple[float | None, bool]:
    """返回 `(值, 是否缺失)`；`"缺失"`/缺键/非数字一律按缺失（不填 0，PRD §22.6）。"""
    raw = scores.get(key)
    if raw is None or (isinstance(raw, str) and raw.strip() == MISSING):
        return None, True
    val = num(raw)
    return (val, False) if val is not None else (None, True)


def weighted_total(
    scores: dict[str, Any], missing_extra: Any = ()
) -> tuple[float | None, list[str]]:
    """缺失维剔除权重、其余按剩余权重归一后加权 round 到 1 位小数；全维缺失 → `(None, 全维)`；
    `missing_extra` 与「值为 `"缺失"`」两种缺失表达一并归一（PRD §22.6）。"""
    extra = {str(k) for k in missing_extra} if isinstance(missing_extra, (list, tuple, set)) else set()
    present: list[tuple[str, float]] = []
    missing: list[str] = []
    for key in WEIGHTS:
        val, is_missing = dim(scores, key)
        if is_missing or val is None or key in extra:
            missing.append(key)
        else:
            present.append((key, val))
    if not present:
        return None, missing
    total_w = sum(WEIGHTS[k] for k, _ in present)
    acc = sum(WEIGHTS[k] * v for k, v in present)
    return round(acc / total_w, 1), missing


@dataclass
class DistCheck:
    total: int
    ge4_ratio: float
    lt2_ratio: float
    violations: list[str] = field(default_factory=list)


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


def _display(total: Any) -> str:
    return "缺失" if total is None else str(total)


def _clip(text: str, limit: int) -> str:
    return text[:limit] + "…" if len(text) > limit else text


def print_dry_run(
    planned: list[dict[str, Any]], skipped: list[dict[str, Any]], dist: DistCheck | None = None
) -> None:
    """逐行打印待写清单（理由截断 60 字）与汇总；`dist` 缺省时按 planned 现算分布校验。"""
    for i, item in enumerate(planned, 1):
        reason = _clip(str(item.get("reason") or ""), _REASON_CLIP)
        print(f"[将写入] {i}. {item.get('话题名称')} → {_display(item.get('weighted_total'))} ｜ {reason}")
    for i, item in enumerate(skipped, 1):
        print(f"[已存在跳过] {i}. {item.get('话题名称')}")
    check = dist if dist is not None else check_distribution(planned)
    verdict = "ok" if not check.violations else "；".join(check.violations)
    print(f"待写 {len(planned)} / 跳过 {len(skipped)} / 分布校验 {verdict}")


def print_summary(stats: dict[str, Any]) -> None:
    """输出可统计 JSON 摘要行（与 `tc-extract` 同风格）；`written`/`failed_writes` 由 F41 写入统计提供。"""
    print(json.dumps(stats, ensure_ascii=False))
    log.info(
        "打分完成：模式=%s 批=%d 调用=%d 行=%d 打分=%d 跳过=%d 写入=%d 写入失败=%d 失败批=%d 丢弃=%d 空批=%d",
        stats["mode"], stats["batches"], stats["llm_calls"], stats["rows"], stats["scored"],
        stats["skipped"], stats.get("written", 0), stats["failed_writes"],
        stats["failed_batches"], stats["dropped"], stats["empty_batches"],
    )
