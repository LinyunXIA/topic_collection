"""MiniMax 大纲响应解析（自 minimax 拆出，#346）：工具调用/内容回退 + 内联 think 剥离。"""

from __future__ import annotations

import json
import logging
from typing import Any

from feedkicker.minimax_transport import _norm_code, content_blocks_text
from feedkicker.reasoning import strip_reasoning

log = logging.getLogger(__name__)


def parse_outline_from_response(data: Any) -> dict[str, Any]:
    """从模型响应提取大纲对象。

    类型异常与解析失败统一抛 RuntimeError（salon_flow 逐题捕获跳过，#227）。
    `tool_calls.arguments` 与 `content` 解析前剥离内联 `<think>` 推理块，`content` 为空时回退
    `reasoning_content`（thinking 模型仅输出推理字段，#346）。
    """
    if not isinstance(data, dict):
        raise RuntimeError(f"MiniMax 响应非 dict: {type(data).__name__}: {str(data)[:200]}")
    choices = data.get("choices") or []
    if not isinstance(choices, list):
        raise RuntimeError(f"choices 非 list: {type(choices).__name__}: {str(choices)[:200]}")
    if choices:
        first = choices[0]
        if not isinstance(first, dict):
            raise RuntimeError(f"choices[0] 非 dict: {type(first).__name__}: {str(first)[:200]}")
        msg = first.get("message") or {}
        if not isinstance(msg, dict):
            raise RuntimeError(f"choices[0].message 非 dict: {type(msg).__name__}: {str(msg)[:200]}")
        tool_calls = msg.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            raise RuntimeError(f"tool_calls 非 list: {type(tool_calls).__name__}: {str(tool_calls)[:200]}")
        if tool_calls:
            tc0 = tool_calls[0]
            if not isinstance(tc0, dict):
                raise RuntimeError(f"tool_calls[0] 非 dict: {type(tc0).__name__}: {str(tc0)[:200]}")
            fn = tc0.get("function") or {}
            if not isinstance(fn, dict):
                raise RuntimeError(f"tool_calls[0].function 非 dict: {type(fn).__name__}: {str(fn)[:200]}")
            args_raw = fn.get("arguments") or ""
            if isinstance(args_raw, dict):
                return args_raw
            if isinstance(args_raw, str) and args_raw.strip():
                try:
                    args = json.loads(strip_reasoning(args_raw))
                except json.JSONDecodeError as e:
                    log.warning("tool_calls arguments 非合法JSON，回落 content: %s", e)
                else:
                    if not isinstance(args, dict):
                        raise RuntimeError(
                            f"tool_calls arguments 非对象: {type(args).__name__}: {str(args)[:200]}"
                        )
                    return args
        content = msg.get("content")
        if not isinstance(content, str):
            content = content_blocks_text(content)
        if not content.strip():
            fallback = msg.get("reasoning_content")
            content = fallback if isinstance(fallback, str) else ""
        if content.strip():
            content = strip_reasoning(content).strip()
            candidates = [content]
            if content.startswith("```"):
                inner = content.strip().strip("`")
                if inner.startswith("json"):
                    inner = inner[4:].strip()
                candidates.append(inner)
            for cand in candidates:
                try:
                    parsed = json.loads(cand)
                except json.JSONDecodeError:
                    continue
                if not isinstance(parsed, dict):
                    raise RuntimeError(
                        f"content 非对象: {type(parsed).__name__}: {str(parsed)[:200]}"
                    )
                return parsed
            raise RuntimeError(f"无法解析大纲JSON，content: {content[:500]}")
    base = data.get("base_resp")
    sc = _norm_code(base.get("status_code")) if isinstance(base, dict) else None
    if sc is not None and sc != 0:
        raise RuntimeError(f"MiniMax 返回错误: {base}")
    raise RuntimeError(f"MiniMax 响应缺少 tool_calls/content: {str(data)[:500]}")
