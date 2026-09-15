"""LLM 文本 → JSON 候选选择（自 extract_parse 下沉以守住 ≤200 行，DESIGN §3）。

容错口径：剥推理块后，从 ``` 围栏与「最内层平衡括号」配对候选中排序取最优：
① 含**调用方期望键**（extract→`topics`；score→`scores`/`results`；缺省用全部目标键）且其值为
list 优先（非空 > 空 > 无键）；② 围栏候选优先于裸 prose 括号候选；③ 同级取最后出现者。
不再「取最长」——更长示例/说明块或真答案为空时不得压过真答案；空答案须是干净的 `[]`
（#R9-17 → #R10-01 → 验证补遗）。
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


def _tier(value: Any, keys: tuple[str, ...]) -> int:
    """候选分层：0=含期望键且任一为非空 list；1=含期望键但均为空 list；2=非 dict/无期望键。"""
    if not isinstance(value, dict):
        return 2
    lists = [value[k] for k in keys if isinstance(value.get(k), list)]
    if not lists:
        return 2
    return 0 if any(lists) else 1


def load_json_value(raw: str, expected_keys: tuple[str, ...] = ()) -> Any | None:
    """容忍围栏与前后说明：按期望键分层 + 围栏优先 + 同级最后出现排序，返回最优可解析 JSON 值。"""
    keys = expected_keys or _TARGET_KEYS
    text = strip_reasoning((raw or "").strip())
    candidates = [(c, 0) for c in (m.group(1).strip() for m in _FENCE_RE.finditer(text)) if c]
    candidates += [(c, 1) for c in (text, *_brace_candidates(text)) if c]
    best: Any = None
    rank: tuple[int, int, int] | None = None
    for idx, (cand, prov) in enumerate(candidates):
        try:
            value = json.loads(cand)
        except json.JSONDecodeError:
            continue
        cur = (_tier(value, keys), prov, -idx)
        if rank is None or cur < rank:
            rank, best = cur, value
    return best


def load_json_obj(raw: str, expected_keys: tuple[str, ...] = ()) -> dict[str, Any] | None:
    """`load_json_value` 的对象视图：非 dict 值视为无对象（extract/score 共用口径，#R10-01）。"""
    value = load_json_value(raw, expected_keys)
    return value if isinstance(value, dict) else None
