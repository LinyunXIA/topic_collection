"""F39 打分提示词构建与 LLM 调用：模板加载 / 横向上文注入 / provider 解析（DESIGN §26.4）。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from feedkicker import extract_llm
from feedkicker.config import PROJECT_ROOT
from feedkicker.config_models import ProviderConf, ScoreConf
from feedkicker.score_source import SCORE_FIELDS

log = logging.getLogger(__name__)

_FIELD_LIMITS: dict[str, int] = {
    "话题名称": 200, "可使用工具": 300, "相关AI原理": 300, "资讯链接": 500, "出处来源": 200,
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
    以控 prompt 体积（DESIGN §26.4 / PRD §22.7 横向上文）。
    """
    lines = [template.rstrip(), "", "## 本批话题（含横向上文）"]
    if prior_scores:
        lines.append("### 已打分参考（供横向对比，不要重复打分）")
        for name, score in prior_scores[-_PRIOR_LIMIT:]:
            lines.append(f"- {name}：{score}")
    lines.append("### 本批话题")
    for i, row in enumerate(batch, 1):
        lines.append(f"{i}. 话题名称：{_cell(row.get('话题名称'), _FIELD_LIMITS['话题名称'])}")
        for name in SCORE_FIELDS[1:]:
            lines.append(f"   {name}：{_cell(row.get(name), _FIELD_LIMITS[name])}")
    return "\n".join(lines)


def call_llm(conf: ProviderConf, prompt: str) -> str:
    """委托 `extract_llm.post_chat`（单一 HTTP 出口 + 错误码归一，不另起一套）。"""
    return extract_llm.post_chat(conf, prompt)


def resolve_for_score(score_conf: ScoreConf, provider: str | None) -> ProviderConf:
    """按 score 段解析 provider；缺 key/占位 → RuntimeError（score_flow 映射 rc2）。"""
    return extract_llm.resolve_provider_conf(score_conf.providers, provider, score_conf.provider)
