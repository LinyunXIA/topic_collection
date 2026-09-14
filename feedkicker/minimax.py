from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from feedkicker.minimax_schema import (
    _TOOL_GENERATE_PPT_OUTLINE as _TOOL_GENERATE_PPT_OUTLINE,
)
from feedkicker.minimax_schema import (
    PROMPT_TEMPLATES as PROMPT_TEMPLATES,
)

log = logging.getLogger(__name__)

_RETRY_CODES = {1002, 1004, 1039, "1002", "1004", "1039"}


def _resolve_api_key(api_key: str | None) -> str:
    return api_key or os.environ.get("MiniMax_Key") or os.environ.get("MINIMAX_API_KEY") or ""


def _extract_code(data: Any) -> str | int | None:
    """错误码提取；非 str/int（list/dict 等）归一为 None，避免 set 成员判断 TypeError（#227）。"""
    if not isinstance(data, dict):
        return None
    for src, keys in (
        (data.get("base_resp"), ("status_code", "code")),
        (data, ("code", "error_code", "status_code", "resp_code")),
        (data.get("error"), ("code", "error_code", "status_code")),
    ):
        if not isinstance(src, dict):
            continue
        for k in keys:
            c = src.get(k)
            if isinstance(c, (str, int)):
                return c
    return None


def _norm_code(code: Any) -> Any:
    """status_code 归一：数字串（含 "0"）转 int，成功/重试判定与解析路径同口径（#245）。"""
    return int(code.strip()) if isinstance(code, str) and code.strip().isdigit() else code


def call_minimax_chat(
    messages: list[dict[str, Any]],
    model: str = "MiniMax-M3",
    api_key: str | None = None,
    base_url: str = "https://api.minimaxi.com",
    timeout: float = 180,
) -> dict[str, Any]:
    key = _resolve_api_key(api_key)
    if not key:
        raise RuntimeError("MiniMax api_key 缺失，请设置 MiniMax_Key 环境变量或 config.minimax.api_key")
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.7,
        "reasoning_split": True,
        "tools": [_TOOL_GENERATE_PPT_OUTLINE],
        "tool_choice": {"type": "function", "function": {"name": "generate_ppt_outline"}},
    }
    last_data: dict[str, Any] | None = None
    for attempt in range(2):
        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
        except httpx.TimeoutException as e:
            log.warning("MiniMax 读超时（%ss）attempt %d/2", timeout, attempt + 1)
            if attempt == 0:
                continue
            raise RuntimeError(f"MiniMax 读超时（{timeout}s）重试后仍失败: {e}") from e
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            try:
                data = json.loads(resp.text or "{}")
            except Exception:  # noqa: BLE001
                data = {}
        last_data = data
        code = _extract_code(data)
        if code in _RETRY_CODES:
            log.warning("MiniMax 返回可重试错误 %s，attempt %d/2", code, attempt + 1)
            if attempt == 0:
                continue
            raise RuntimeError(f"MiniMax 可重试错误 {code}: {data}")
        if resp.status_code != 200:
            if resp.status_code in (429, 529) and attempt == 0:
                log.warning("MiniMax HTTP %d 限流，重试1次", resp.status_code)
                continue
            if code is not None and str(code).strip() != "0":
                raise RuntimeError(f"MiniMax 错误 {code}: {data}")
            if resp.status_code >= 400:
                raise RuntimeError(f"MiniMax HTTP {resp.status_code}: {data}")
        base = data.get("base_resp") if isinstance(data, dict) else None
        sc = _norm_code(base.get("status_code") if isinstance(base, dict) else None)
        if sc is not None and sc != 0:
            if not isinstance(sc, (str, int)):
                raise RuntimeError(
                    f"MiniMax base_resp.status_code 类型异常: {type(sc).__name__}: {str(sc)[:200]}"
                )
            if sc in _RETRY_CODES and attempt == 0:
                continue
            raise RuntimeError(f"MiniMax base_resp {sc}: {data}")
        return data
    if last_data is not None:
        raise RuntimeError(f"MiniMax 重试后仍失败: {last_data}")
    raise RuntimeError("MiniMax 调用失败")


def _parse_outline_from_response(data: Any) -> dict[str, Any]:
    """从模型响应提取大纲对象。

    类型异常与解析失败统一抛 RuntimeError（salon_flow 逐题捕获跳过，#227）。
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
                    args = json.loads(args_raw)
                except json.JSONDecodeError as e:
                    log.warning("tool_calls arguments 非合法JSON，回落 content: %s", e)
                else:
                    if not isinstance(args, dict):
                        raise RuntimeError(
                            f"tool_calls arguments 非对象: {type(args).__name__}: {str(args)[:200]}"
                        )
                    return args
        content = msg.get("content") or ""
        if isinstance(content, str) and content.strip():
            content = content.strip()
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


def gen_outline(
    topic: str,
    kind: str = "tool",
    api_key: str | None = None,
    base_url: str = "https://api.minimaxi.com",
    model: str = "MiniMax-M3",
) -> dict[str, Any]:
    if kind not in PROMPT_TEMPLATES:
        raise ValueError(f"未知 kind: {kind}，可选 {list(PROMPT_TEMPLATES)}")
    system_prompt = PROMPT_TEMPLATES[kind]
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": topic},
    ]
    data = call_minimax_chat(messages, model=model, api_key=api_key, base_url=base_url)
    return _parse_outline_from_response(data)
