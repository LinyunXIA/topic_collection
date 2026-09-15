"""LLM 文本 → JSON 候选选择（自 extract_parse 下沉以守住 ≤200 行，DESIGN §3）。

容错口径：剥推理块后，从 ``` 围栏与「最内层平衡括号」配对候选中，优先取「含目标键
（`topics`/`scores`/`results`）且其值为非空 list」者，同质量取更长、更靠后者，避免模型
先回显 schema/示例（`{"topics": []}`）遮蔽其后的真结果（#R9-17 → #R10-01）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from feedkicker.reasoning import strip_reasoning

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

_TARGET_KEYS = ("topics", "scores", "results")


def _brace_candidates(text: str) -> list[str]:
    """最内层平衡括号配对扫描：栈配对 `{`…`}`（跳过字符串字面量），逐个配对出候选。

    不再用「首个 `{` 到末个 `}`」制造跨说明的非法串（#R9-17 → #R10-01）；单次扫描 O(n)，
    未配对的开括号不产出候选，避免大量 `{` 时的重复全串扫描。
    """
    out: list[str] = []
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
            out.append(text[stack.pop() : i + 1])
        i += 1
    return out


def _has_target_list(obj: dict[str, Any]) -> bool:
    return any(isinstance(obj.get(k), list) and bool(obj[k]) for k in _TARGET_KEYS)


def load_json_value(raw: str) -> Any | None:
    """容忍围栏与前后说明：候选按「目标键值为非空 list」优先、同级取最长排序，返回首个可解析 JSON 值。"""
    text = strip_reasoning((raw or "").strip())
    candidates = [m.group(1).strip() for m in _FENCE_RE.finditer(text)]
    candidates += [text, *_brace_candidates(text)]
    best: Any = None
    rank: tuple[int, int, int] | None = None
    for idx, cand in enumerate(candidates):
        if not cand:
            continue
        try:
            value = json.loads(cand)
        except json.JSONDecodeError:
            continue
        cur = (0 if isinstance(value, dict) and _has_target_list(value) else 1, -len(cand), -idx)
        if rank is None or cur < rank:
            rank, best = cur, value
    return best


def load_json_obj(raw: str) -> dict[str, Any] | None:
    """`load_json_value` 的对象视图：非 dict 值视为无对象（extract/score 共用口径，#R10-01）。"""
    value = load_json_value(raw)
    return value if isinstance(value, dict) else None
