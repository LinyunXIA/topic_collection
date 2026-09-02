from __future__ import annotations

import json
import logging
import os

import httpx

log = logging.getLogger(__name__)

PROMPT_TEMPLATES: dict[str, str] = {
    "tool": (
        "你是PPT大纲生成助手（工具类）。请围绕用户给定的话题，生成一份面向实操的PPT大纲。"
        "要求：5-8页自适应，覆盖背景/痛点、工具选型/流程、分步操作、案例演示、避坑与总结；"
        "每页 heading + 3-4 条 bullets + speaker_note；通过 generate_ppt_outline 工具返回JSON。"
    ),
    "principle": (
        "你是PPT大纲生成助手（原理类）。请围绕用户给定的话题，生成一份面向原理与深度的PPT大纲。"
        "要求：5-8页自适应，覆盖背景/问题定义、核心原理/架构、关键机制对比、推导与验证、趋势与总结；"
        "每页 heading + 3-4 条 bullets + speaker_note；通过 generate_ppt_outline 工具返回JSON。"
    ),
}

_TOOL_GENERATE_PPT_OUTLINE = {
    "type": "function",
    "function": {
        "name": "generate_ppt_outline",
        "description": "生成PPT大纲，返回标题与5-8页幻灯片",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "大纲总标题"},
                "slides": {
                    "type": "array",
                    "description": "幻灯片列表，5-8页",
                    "items": {
                        "type": "object",
                        "properties": {
                            "heading": {"type": "string", "description": "页标题"},
                            "bullets": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "要点，3-4条",
                            },
                            "speaker_note": {"type": "string", "description": "讲者备注"},
                        },
                        "required": ["heading", "bullets"],
                    },
                    "minItems": 5,
                    "maxItems": 8,
                },
            },
            "required": ["title", "slides"],
        },
    },
}

_RETRY_CODES = {1002, 1004, 1039, "1002", "1004", "1039"}


def _resolve_api_key(api_key: str | None) -> str:
    if api_key:
        return api_key
    return os.environ.get("MiniMax_Key") or os.environ.get("MINIMAX_API_KEY") or ""


def _extract_code(data: dict) -> str | int | None:
    if not isinstance(data, dict):
        return None
    br = data.get("base_resp")
    if isinstance(br, dict):
        c = br.get("status_code")
        if c is not None:
            return c
        c = br.get("code")
        if c is not None:
            return c
    for k in ("code", "error_code", "status_code", "resp_code"):
        if k in data and data[k] is not None:
            return data[k]
    err = data.get("error")
    if isinstance(err, dict):
        for k in ("code", "error_code", "status_code"):
            if k in err and err[k] is not None:
                return err[k]
    return None


def call_minimax_chat(
    messages: list[dict],
    model: str = "MiniMax-M3",
    api_key: str | None = None,
    base_url: str = "https://api.minimaxi.com",
    timeout: float = 60,
) -> dict:
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
    last_data: dict | None = None
    for attempt in range(2):
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
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
            if code is not None and code != 0:
                raise RuntimeError(f"MiniMax 错误 {code}: {data}")
            if resp.status_code != 200 and attempt == 0:
                pass
            if resp.status_code >= 400:
                raise RuntimeError(f"MiniMax HTTP {resp.status_code}: {data}")
        if isinstance(data, dict) and data.get("base_resp", {}).get("status_code") not in (None, 0):
            sc = data["base_resp"]["status_code"]
            if sc in _RETRY_CODES and attempt == 0:
                continue
            if sc != 0:
                raise RuntimeError(f"MiniMax base_resp {sc}: {data}")
        return data
    if last_data is not None:
        raise RuntimeError(f"MiniMax 重试后仍失败: {last_data}")
    raise RuntimeError("MiniMax 调用失败")


def _parse_outline_from_response(data: dict) -> dict:
    choices = data.get("choices") or []
    if choices:
        msg = choices[0].get("message") or {}
        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            fn = tool_calls[0].get("function") or {}
            args_raw = fn.get("arguments") or ""
            if isinstance(args_raw, dict):
                return args_raw
            if isinstance(args_raw, str) and args_raw.strip():
                try:
                    return json.loads(args_raw)
                except json.JSONDecodeError as e:
                    log.warning("tool_calls arguments 非合法JSON，回落 content: %s", e)
        content = msg.get("content") or ""
        if isinstance(content, str) and content.strip():
            content = content.strip()
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                if content.startswith("```"):
                    inner = content.strip().strip("`")
                    if inner.startswith("json"):
                        inner = inner[4:].strip()
                    try:
                        return json.loads(inner)
                    except json.JSONDecodeError:
                        pass
                raise RuntimeError(f"无法解析大纲JSON，content: {content[:500]}")
    base = data.get("base_resp")
    if isinstance(base, dict) and base.get("status_code") not in (None, 0):
        raise RuntimeError(f"MiniMax 返回错误: {base}")
    raise RuntimeError(f"MiniMax 响应缺少 tool_calls/content: {str(data)[:500]}")


def gen_outline(
    topic: str,
    kind: str = "tool",
    api_key: str | None = None,
    base_url: str = "https://api.minimaxi.com",
    model: str = "MiniMax-M3",
) -> dict:
    if kind not in PROMPT_TEMPLATES:
        raise ValueError(f"未知 kind: {kind}，可选 {list(PROMPT_TEMPLATES)}")
    system_prompt = PROMPT_TEMPLATES[kind]
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": topic},
    ]
    data = call_minimax_chat(messages, model=model, api_key=api_key, base_url=base_url)
    return _parse_outline_from_response(data)
