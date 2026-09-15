"""URL token 归一：粘连 token 截断与包裹标点剥离（自 fetch 下沉以守住 ≤200 行，DESIGN §3）。

`trim_url` 剥成对包裹与裸尾标点；仅 query 段（`?` 之后、`#` 之前）内的尾标点不剥，
避免 `?q=a!` 与 `?q=a` 去重键碰撞（#R9-19/#R10-20 → 验证补遗）。
"""

from __future__ import annotations

import re

_URL_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~:/?#[]@!$&'()*+,;=%")

_URL_EDGE = "()<>[]{}（）【】「」《》，。；、！？：；“”‘’\"'.,;:!?"

_WRAP = {"(": ")", "[": "]", "{": "}", "<": ">", "（": "）", "【": "】", "「": "」", "《": "》"}

_BARE_HOST_RE = re.compile(r"^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?::[0-9]+)?/")


def _in_query(text: str) -> bool:
    q = text.rfind("?")
    return q >= 0 and text.rfind("#") < q


def trim_url(token: str) -> str:
    """粘连 token 截到最后一个合法 URL 字符，再剥成对包裹与裸尾标点（#R9-19/#R10-20）。

    尾部标点若与开头形成包裹对（`(url)`/`<url>`）则连开头一并剥除；query 段（`?` 之后、`#` 之前）
    内的尾标点不剥，保留 `?q=a!` 不被压成 `?q=a`；平衡括号路径 `…/a_(b_(c))` 原样保留。
    """
    text = (token or "").strip()
    end = max((i for i, ch in enumerate(text) if ch in _URL_CHARS), default=-1)
    if end < 0:
        return ""
    text = text[: end + 1]
    while len(text) >= 2 and _WRAP.get(text[0]) == text[-1]:
        text = text[1:-1]
    text = text.lstrip(_URL_EDGE)
    while text and text[-1] in _URL_EDGE:
        if _in_query(text) or (text[-1] == ")" and text.count("(") >= text.count(")")):
            break
        text = text[:-1]
    return text


def is_url_token(token: str) -> bool:
    """token 是否 URL：含 `://`，或形如带路径的裸域名（`example.com/path`，TLD ≥2 字母）。

    点分但非 URL 的 token（版本号 `v2.0.1`、文件名 `report.pdf`）不得生成去重键，否则跨话题
    同注记会被误判重复而静默漏写（#329/#361）。
    """
    text = (token or "").strip()
    return "://" in text or bool(_BARE_HOST_RE.match(text))
