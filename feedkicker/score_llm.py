"""F39 打分提示词构建与 LLM 调用：模板加载 / 横向上文注入 / provider 解析（DESIGN §26.4）。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from feedkicker import extract_llm, score_parse
from feedkicker.config import PROJECT_ROOT
from feedkicker.config_models import ProviderConf, ScoreConf
from feedkicker.score_source import SCORE_FIELDS, name_text

log = logging.getLogger(__name__)

_FIELD_LIMITS: dict[str, int] = {
    "可使用工具": 300, "相关AI原理": 300, "资讯链接": 500, "出处来源": 200,
}

_PRIOR_LIMIT = 200


def load_template(prompt_file: str) -> str:
    """读仓库根相对路径的提示词模板；缺失/为空 → RuntimeError（score_flow 映射 rc2）。"""
    path = Path(prompt_file)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.is_file():
        raise RuntimeError(f"提示词文件不存在: {path}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise RuntimeError(f"提示词文件为空: {path}")
    return text


def _cell(value: Any, limit: int) -> str:
    if isinstance(value, list):
        text = " / ".join(str(v) for v in value if str(v).strip())
    else:
        text = str(value or "")
    text = " ".join(text.split())
    if not text:
        return "（空）"
    return text[:limit] + "…" if len(text) > limit else text


def build_prompt(
    template: str, batch: list[dict[str, Any]], prior_scores: list[tuple[str, str]]
) -> str:
    """在模板末尾追加「## 本批话题（含横向上文）」：逐条 5 字段（空值占位）+ 已打分参考（≤200 条）。

    摘要类字段做长度上限（`相关AI原理` ≤300 字符）；`prior_scores` 超出 200 条按行序取最近，
    以控 prompt 体积（DESIGN §26.4 / PRD §22.7 横向上文）。话题名与匹配键同源
    （`name_text`/`name_key`：折叠空白 + 截断 ≤200，#377）。
    """
    lines = [template.rstrip(), "", "## 本批话题（含横向上文）"]
    if prior_scores:
        lines.append("### 已打分参考（供横向对比，不要重复打分）")
        for name, score in prior_scores[-_PRIOR_LIMIT:]:
            lines.append(f"- {name_text(name)}：{score}")
    lines.append("### 本批话题")
    for i, row in enumerate(batch, 1):
        lines.append(f"{i}. 话题名称：{name_text(row.get('话题名称')) or '（空）'}")
        for name in SCORE_FIELDS[1:]:
            lines.append(f"   {name}：{_cell(row.get(name), _FIELD_LIMITS[name])}")
    return "\n".join(lines)


def call_llm(conf: ProviderConf, prompt: str, timeout: float = 600.0) -> str:
    """委托 `extract_llm.post_chat`（单一 HTTP 出口 + 错误码归一，不另起一套）；`timeout` 透传。"""
    return extract_llm.post_chat(conf, prompt, timeout)


def resolve_for_score(score_conf: ScoreConf, provider: str | None) -> ProviderConf:
    """按 score 段解析 provider；缺 key/占位 → RuntimeError（score_flow 映射 rc2）。"""
    return extract_llm.resolve_provider_conf(score_conf.providers, provider, score_conf.provider)


@dataclass
class BatchResult:
    scored: list[dict[str, Any]] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    calls: int = 0
    failed: int = 0
    empty: int = 0
    dropped: int = 0


def _score_text(total: float | None) -> str:
    return "缺失" if total is None else str(total)


def refine_batches(
    conf: ProviderConf,
    template: str,
    batches: list[list[dict[str, Any]]],
    prior_scores: list[tuple[str, str]],
    max_calls: int,
    timeout: float = 600.0,
) -> BatchResult:
    """逐批「调用+解析」共享重试预算（第 1 次失败重试 1 次，总 HTTP ≤2/批），返回 `BatchResult`。

    两次都失败计 `failed`；`max_calls` 达限时当前批（已尝试未解析）计入失败并停止剩余批
    （对齐 `extract_llm.refine_batches` 的 #334 教训）；空 scores 计 `empty`；归一后 dropped
    累计（解析侧 + 缺返回/多余行）；命中行回灌 `prior_scores` 作为下一批横向上文。
    `timeout` 透传每次 LLM 调用（`score.timeout_seconds`，大批量需调大）。
    """
    scored: list[dict[str, Any]] = []
    violations: list[str] = []
    prior = list(prior_scores)
    calls = failed = empty = dropped = 0
    for no, batch in enumerate(batches, 1):
        prompt = build_prompt(template, batch, prior)
        items: list[dict[str, Any]] | None = None
        parsed_dropped = 0
        dropped_keys: set[str] = set()
        limit_reached = False
        tried = False
        for attempt in (1, 2):
            if max_calls and calls >= max_calls:
                log.warning("达到 max_calls=%d 上限，停止剩余批", max_calls)
                limit_reached = True
                break
            calls += 1
            tried = True
            try:
                raw = call_llm(conf, prompt, timeout)
            except Exception as e:  # noqa: BLE001
                log.warning("第 %d/%d 批第 %d/2 次尝试失败（调用异常）: %s", no, len(batches), attempt, e)
                continue
            try:
                items, parsed_dropped, dropped_keys = score_parse.parse_results_full(raw)
                break
            except ValueError as e:
                log.warning("第 %d/%d 批第 %d/2 次尝试失败（契约解析失败）: %s", no, len(batches), attempt, e)
        if limit_reached:
            if tried and items is None:
                failed += 1
                log.warning("第 %d/%d 批因达到 max_calls 上限中止重试，计失败批", no, len(batches))
            break
        if items is None:
            failed += 1
            log.warning("第 %d/%d 批两次尝试后仍失败，跳过", no, len(batches))
            continue
        if not items:
            dropped += parsed_dropped
            if parsed_dropped:
                failed += 1
                log.warning("第 %d/%d 批 %d 条 scores 全部非法，计失败批", no, len(batches), parsed_dropped)
            else:
                empty += 1
            continue
        normalized, dist, norm_dropped = score_parse.normalize_results(items, batch, dropped_keys)
        dropped += parsed_dropped + norm_dropped
        if dist.violations:
            log.warning("第 %d/%d 批分布校验违规：%s", no, len(batches), "；".join(dist.violations))
        violations.extend(dist.violations)
        scored.extend(normalized)
        prior.extend((name_text(n["话题名称"]), _score_text(n["weighted_total"])) for n in normalized)
    return BatchResult(
        scored=scored, violations=violations, calls=calls, failed=failed, empty=empty, dropped=dropped
    )
