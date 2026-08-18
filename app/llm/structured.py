"""结构化输出解析 + 修复 — DESIGN §4.5 / §6

parse_with_repair：尝试解析 LLM 输出的 JSON，失败时尝试修复。
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)


def parse_json(text: str) -> dict | list | None:
    """尝试直接解析 JSON。"""
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def extract_json_from_text(text: str) -> str:
    """从 LLM 输出中提取 JSON 块（支持 markdown 代码块包裹）。"""
    text = text.strip()

    # 尝试提取 ```json ... ``` 代码块
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # 尝试提取第一个 { ... } 或 [ ... ]
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        start = text.find(start_char)
        end = text.rfind(end_char)
        if start != -1 and end > start:
            return text[start : end + 1]

    return text


def repair_json(text: str) -> dict | list | None:
    """尝试修复常见 JSON 问题后解析。"""
    extracted = extract_json_from_text(text)

    # 直接解析
    result = parse_json(extracted)
    if result is not None:
        return result

    # 常见修复：单引号换双引号
    repaired = extracted.replace("'", '"')
    result = parse_json(repaired)
    if result is not None:
        logger.debug("JSON 修复成功：单引号→双引号")
        return result

    # 修复：移除尾部逗号
    repaired = re.sub(r",\s*([}\]])", r"\1", extracted)
    result = parse_json(repaired)
    if result is not None:
        logger.debug("JSON 修复成功：移除尾部逗号")
        return result

    # 修复：换行符在字符串内未转义（常见于 LLM 输出）
    # 尝试将未转义的换行替换为 \\n（仅在非字符串上下文中）
    repaired = re.sub(r'(?<!\\)\n', r"\\n", extracted)
    result = parse_json(repaired)
    if result is not None:
        logger.debug("JSON 修复成功：转义换行符")
        return result

    return None


def parse_with_repair(text: str, expected_keys: list[str] | None = None) -> dict | None:
    """解析 LLM 输出的 JSON，带修复和校验。

    Args:
        text: LLM 输出文本
        expected_keys: 期望的顶层 key 列表（可选，缺失则降级）

    Returns:
        解析后的 dict，失败返回 None
    """
    result = repair_json(text)
    if result is None:
        logger.warning("JSON 解析完全失败，前 200 字: %s", text[:200])
        return None

    if not isinstance(result, dict):
        logger.warning("JSON 解析结果不是 dict: %s", type(result).__name__)
        return None

    if expected_keys:
        missing = [k for k in expected_keys if k not in result]
        if missing:
            logger.warning("JSON 缺少期望 key: %s", missing)

    return result
