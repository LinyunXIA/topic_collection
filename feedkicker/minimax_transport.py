"""MiniMax chat 调用与错误码归一（自 minimax 拆出，#346）。"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from feedkicker.minimax_schema import _TOOL_GENERATE_PPT_OUTLINE

log = logging.getLogger(__name__)

_RETRY_CODES = {1002, 1004, 1039, "1002", "1004", "1039"}


def content_blocks_text(content: Any) -> str:
    """content-blocks 数组 → 拼接各 block 的 `text`/`content`（extract/minimax 两处同口径，#R9-20）。"""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            content = block.get("content")
            seg = text if isinstance(text, str) and text else (content if isinstance(content, str) else "")
            if seg:
                parts.append(seg)
    return "".join(parts)


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
