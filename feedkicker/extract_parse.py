"""F30 提炼提示词构建与 JSON 解析（自 extract_llm 拆出，DESIGN §25.4/#250）。"""

from __future__ import annotations

import json
import re
from typing import Any

_REQUIRED_KEYS = ("话题名称", "可使用工具", "相关AI原理", "资讯链接", "出处来源")

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def build_batch_prompt(template: str, items: list[dict[str, Any]]) -> str:
    """把本批条目（编号+标题+url+摘要）注入模板尾部；摘要截断 300 字符控 token。"""
    lines = [template.rstrip(), "", "## 本批资讯（先整合去重，再按 schema 输出 JSON）"]
    for i, it in enumerate(items, 1):
        title = " ".join(str(it.get("title") or "").split())
        url = str(it.get("url") or "").strip()
        summary = " ".join(str(it.get("description") or "").split())
        if len(summary) > 300:
            summary = summary[:300] + "…"
        lines.append(f"{i}. [{it.get('feed_id') or ''}] {title}")
        lines.append(f"   链接: {url}")
        if summary:
            lines.append(f"   摘要: {summary}")
    return "\n".join(lines)


def parse_topics(raw: str) -> list[dict[str, Any]]:
    """解析 LLM 原始文本；非法 JSON/顶层非对象/topics 非列表/缺 5 键 → []（调用方 WARNING 跳过）。"""
    obj = _load_json_obj(raw)
    if obj is None:
        return []
    items = obj.get("topics")
    if not isinstance(items, list):
        return []
    parsed: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or any(k not in item for k in _REQUIRED_KEYS):
            return []
        parsed.append(
            {
                "话题名称": str(item.get("话题名称") or "").strip(),
                "可使用工具": str(item.get("可使用工具") or "").strip(),
                "相关AI原理": str(item.get("相关AI原理") or "").strip(),
                "资讯链接": _str_list(item.get("资讯链接")),
                "出处来源": _str_list(item.get("出处来源")),
            }
        )
    return merge_topics(parsed)


def merge_topics(topics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按「话题名称」合并同话题多来源：链接/来源顺序去重，工具/原理首个非空保留。"""
    merged: dict[str, dict[str, Any]] = {}
    for t in topics:
        name = str(t.get("话题名称") or "").strip()
        if not name:
            continue
        cur = merged.get(name)
        if cur is None:
            merged[name] = {
                "话题名称": name,
                "可使用工具": str(t.get("可使用工具") or "").strip(),
                "相关AI原理": str(t.get("相关AI原理") or "").strip(),
                "资讯链接": _str_list(t.get("资讯链接")),
                "出处来源": _str_list(t.get("出处来源")),
            }
            continue
        for field in ("可使用工具", "相关AI原理"):
            if not cur[field]:
                cur[field] = str(t.get(field) or "").strip()
        for field in ("资讯链接", "出处来源"):
            for v in _str_list(t.get(field)):
                if v not in cur[field]:
                    cur[field].append(v)
    return list(merged.values())


def _load_json_obj(raw: str) -> dict[str, Any] | None:
    """容忍 ```json 围栏与前后说明文字：先整体解析，失败再取最外层 {...} 重试。"""
    text = (raw or "").strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    braced = text[text.find("{") : text.rfind("}") + 1] if "{" in text and "}" in text else ""
    for cand in (text, braced):
        if not cand:
            continue
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = [str(v) for v in value]
    else:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for v in items:
        v = v.strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out
