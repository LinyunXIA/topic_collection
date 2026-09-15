"""LLM 文本 → JSON 候选选择（自 extract_parse 下沉以守住 ≤200 行，DESIGN §3）。

容错口径：剥推理块后收集两类候选——`fence`（```json 围栏内）与 `bare`（裸 prose 花括号，最内层
平衡括号配对、按文档序）。命中「含调用方期望键（extract→`topics`；score→`scores`/`results`；未传则
不按键过滤）且其值为 list」的候选参与选择：

- 有 `fence` 命中 → 取**最后一个**（围栏里答案通常在最后给出，之前的围栏多为 schema/示例）；
- 否则取**第一个** `bare` 命中（裸 prose 里答案通常最先给出，其后才是示例/说明）。

不用任何长度启发——更长示例/说明块或空答案不得压过真答案
（#R9-17 → #R10-01 → P1-1 残留）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from feedkicker.reasoning import strip_reasoning

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _brace_candidates(text: str) -> list[str]:
    """最内层平衡括号配对扫描：栈配对 `{`…`}`（跳过字符串字面量），按起点升序产出候选。

    不再用「首个 `{` 到末个 `}`」制造跨说明的非法串（#R9-17 → #R10-01）；单次扫描 O(n)，
    未配对的开括号不产出候选，避免大量 `{` 时的重复全串扫描。
    """
    spans: list[tuple[int, int]] = []
    stack: list[int] = []
    in_str = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 1
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            spans.append((stack.pop(), i + 1))
        i += 1
    return [text[start:end] for start, end in sorted(spans)]


def _qualifies(value: Any, keys: tuple[str, ...]) -> bool:
    """未传期望键时任意可解析值均命中；传了则须为 dict 且任一期望键映射为 list（空 list 也算）。"""
    if not keys:
        return True
    return isinstance(value, dict) and any(isinstance(value.get(k), list) for k in keys)


def _parsed(cand: str) -> tuple[bool, Any]:
    try:
        return True, json.loads(cand)
    except json.JSONDecodeError:
        return False, None


def load_json_value(raw: str, expected_keys: tuple[str, ...] = ()) -> Any | None:
    """见模块 docstring：围栏取最后一个命中、裸 prose 取第一个命中；无命中返回 None。"""
    text = strip_reasoning((raw or "").strip())
    for cand in reversed([m.group(1).strip() for m in _FENCE_RE.finditer(text)]):
        ok, value = _parsed(cand)
        if ok and _qualifies(value, expected_keys):
            return value
    for cand in (text, *_brace_candidates(text)):
        if not cand:
            continue
        ok, value = _parsed(cand)
        if ok and _qualifies(value, expected_keys):
            return value
    return None


def load_json_obj(raw: str, expected_keys: tuple[str, ...] = ()) -> dict[str, Any] | None:
    """`load_json_value` 的对象视图：非 dict 值视为无对象（extract/score 共用口径，#R10-01）。"""
    value = load_json_value(raw, expected_keys)
    return value if isinstance(value, dict) else None
