"""thinking 模型内联推理块剥离（#321/#323/#331）。

`_tag_at` 识别 think/thinking/reasoning 标签（含属性、自闭合），`_find_close` 以开标签栈
扫描配对闭标签（支持嵌套，且跳过 JSON 字符串字面量）；顶层 `strip_reasoning` 只剥离字符串
字面量之外的成对块，避免破坏 JSON 字段值里的标签文本（#331）。
"""

from __future__ import annotations

_NAMES = ("think", "thinking", "reasoning")


def _tag_at(text: str, i: int) -> tuple[str, str, bool, int] | None:
    """匹配 text[i] 起的 think 类标签，返回 (name, attrs, is_close, end)；非标签返回 None。"""
    if i >= len(text) or text[i] != "<":
        return None
    j = i + 1
    closing = j < len(text) and text[j] == "/"
    if closing:
        j += 1
    low = text.lower()
    for name in _NAMES:
        if not low.startswith(name, j):
            continue
        k = j + len(name)
        if k < len(text) and (text[k].isalnum() or text[k] == "_"):
            continue
        gt = text.find(">", k)
        if gt < 0:
            return None
        return name, text[k:gt], closing, gt + 1
    return None


def _find_close(text: str, start: int, name: str) -> int | None:
    """自 start 扫描 start 对应的配对闭标签，返回闭标签后的下标；未配对返回 None。"""
    depth = 1
    i = start
    in_str = False
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            i += 1
            continue
        tag = _tag_at(text, i)
        if tag is not None:
            tname, attrs, closing, end = tag
            if closing:
                if tname == name:
                    depth -= 1
                    if depth == 0:
                        return end
            elif not attrs.endswith("/"):
                depth += 1
            i = end
            continue
        i += 1
    return None


def strip_reasoning(text: str) -> str:
    """剥离字符串字面量之外的成对推理块；裸 `<think>` 未闭合时自该处截断（#321/#323/#331）。

    属性/空白开标签（`<think >`/`<think foo=1>`）无配对时保留原样，不得再被误当未闭合吞掉
    其后 JSON（#323）；自闭合 `<think/>` 直接移除。
    """
    out: list[str] = []
    i = 0
    n = len(text or "")
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        tag = _tag_at(text, i)
        if tag is not None:
            name, attrs, closing, end = tag
            if not closing and attrs.endswith("/"):
                i = end
                continue
            if not closing:
                close = _find_close(text, end, name)
                if close is not None:
                    i = close
                    continue
                if not attrs:
                    return "".join(out)
        out.append(ch)
        i += 1
    return "".join(out)
