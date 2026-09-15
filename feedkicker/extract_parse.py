"""F30 提炼提示词构建与 JSON 解析（自 extract_llm 拆出，DESIGN §25.4/#250）。"""

from __future__ import annotations

import logging
import unicodedata
from typing import Any

from feedkicker.fetch import is_url_token, trim_url
from feedkicker.llm_json import load_json_obj as _load_json_obj
from feedkicker.reasoning import strip_reasoning as strip_reasoning

log = logging.getLogger(__name__)

_REQUIRED_KEYS = ("话题名称", "可使用工具", "相关AI原理", "资讯链接", "出处来源")


def topic_key(name: Any) -> str:
    """去重/合并比较键：NFKC 归一 + strip + casefold（写入仍用原值，PRV-3）。"""
    return unicodedata.normalize("NFKC", str(name or "")).strip().casefold()


def _clip(text: str, limit: int) -> str:
    return text[:limit] + "…" if len(text) > limit else text


def build_batch_prompt(template: str, items: list[dict[str, Any]]) -> str:
    """把本批条目（编号+标题+url+摘要）注入模板尾部；摘要/标题/链接截断控 prompt 体积（#335）。"""
    lines = [template.rstrip(), "", "## 本批资讯（先整合去重，再按 schema 输出 JSON）"]
    for i, it in enumerate(items, 1):
        title = _clip(" ".join(str(it.get("title") or "").split()), 200)
        url = _clip(str(it.get("url") or "").strip(), 500)
        summary = _clip(" ".join(str(it.get("description") or "").split()), 300)
        lines.append(f"{i}. [{it.get('feed_id') or ''}] {title}")
        lines.append(f"   链接: {url}")
        if summary:
            lines.append(f"   摘要: {summary}")
    return "\n".join(lines)


def parse_topics(raw: str) -> tuple[list[dict[str, Any]], int]:
    """解析 LLM 原始文本，返回 `(topics, dropped)`。

    JSON 非法 / 顶层非对象 / `topics` 非列表 → raise ValueError（调用方计失败批）；
    单个 topic 非对象 / 缺 5 键 / 名称 NFKC 归一后为空 → 丢弃该条、dropped 计数并 WARNING，
    不整批弃（PRV-6）；空白名同样计 dropped，不得静默丢弃（#294）。
    """
    obj = _load_json_obj(raw)
    if obj is None:
        raise ValueError("LLM 输出不是合法 JSON 对象")
    items = obj.get("topics")
    if not isinstance(items, list):
        raise ValueError("topics 字段缺失或非列表")
    parsed: list[dict[str, Any]] = []
    dropped = 0
    for item in items:
        if not isinstance(item, dict) or any(k not in item for k in _REQUIRED_KEYS):
            dropped += 1
            continue
        name = str(item.get("话题名称") or "").strip()
        if not topic_key(name):
            dropped += 1
            continue
        parsed.append(
            {
                "话题名称": name,
                "可使用工具": str(item.get("可使用工具") or "").strip(),
                "相关AI原理": str(item.get("相关AI原理") or "").strip(),
                "资讯链接": _str_list(item.get("资讯链接")),
                "出处来源": _str_list(item.get("出处来源")),
            }
        )
    if dropped:
        log.warning("丢弃 %d 条非法 topic（非对象/缺 5 键/空白名）", dropped)
    return merge_topics(parsed), dropped


def merge_topics(topics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按「话题名称」NFKC 归一比较键合并同话题多来源：链接/来源顺序去重，工具/原理首个非空保留。"""
    merged: dict[str, dict[str, Any]] = {}
    for t in topics:
        name = str(t.get("话题名称") or "").strip()
        key = topic_key(name)
        if not key:
            continue
        cur = merged.get(key)
        if cur is None:
            merged[key] = {
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


def md_link_tokens(text: str) -> list[str]:
    """把 markdown 链接目标与余文裸链拆成 token 列表（`_link_key` 归一前，供 `link_keys`）。

    目标自 `](` 起按**括号平衡**扫描，支持任意嵌套深度（`…/a_(b_(c))`，#N4）；标签文本
    `[..]` 不参与（`[标签](url)` 不得把标签当 URL，#270）；相邻/混排链接各取各、裸链不丢
    （#270/#288）；非 URL token（正文注记、`(url "标题")` 标题）一律过滤（#329）。
    """
    tokens: list[str] = []
    rest: list[str] = []
    i = 0
    while True:
        at = text.find("](", i)
        if at < 0:
            rest.append(text[i:])
            break
        start = text.rfind("[", i, at)
        if start < 0:
            rest.append(text[i : at + 2])
            i = at + 2
            continue
        depth, j = 1, at + 2
        while j < len(text) and depth:
            depth += (text[j] == "(") - (text[j] == ")")
            j += 1
        if depth:
            rest.append(text[i:])
            break
        tokens.extend(text[at + 2 : j - 1].split())
        rest.append(text[i:start])
        i = j
    tokens.extend("".join(rest).split())
    return [t for t in (trim_url(x) for x in tokens) if is_url_token(t)]
