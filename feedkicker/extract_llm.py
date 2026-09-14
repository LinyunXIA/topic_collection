"""F29/F30 LLM provider 抽象 + 批量提炼：调用 / 提示词构建 / JSON 解析（DESIGN §25.2–§25.4）。"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from feedkicker import minimax
from feedkicker.config_models import ExtractConf, ProviderConf, env_key_for
from feedkicker.extract_parse import (
    build_batch_prompt as build_batch_prompt,
    merge_topics as merge_topics,
    parse_topics as parse_topics,
)

log = logging.getLogger(__name__)

PROVIDERS: dict[str, ProviderConf] = {
    "minimax": ProviderConf(
        base_url="https://api.minimaxi.com/v1",
        model="MiniMax-M3",
        tool_label="MMX（MiniMax）",
    ),
    "deepseek": ProviderConf(
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        tool_label="DS（DeepSeek）",
    ),
}


def resolve_provider(cfg: ExtractConf, name: str | None = None) -> ProviderConf:
    """按 provider 名合并注册表默认与 yaml 覆盖；缺 key/占位 key 抛 RuntimeError（不发起调用）。"""
    provider = (name or cfg.provider or "minimax").strip()
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise RuntimeError(f"未知 extract provider: {provider}（可用 {sorted(PROVIDERS)}）")
    got = cfg.providers.get(provider) or ProviderConf()
    api_key = got.api_key or env_key_for(provider)
    if not api_key or api_key.strip().startswith("<"):
        raise RuntimeError(
            f"extract provider {provider} 缺少 api_key（配置 providers.{provider}.api_key 或设置环境变量）"
        )
    return ProviderConf(
        base_url=got.base_url or spec.base_url,
        model=got.model or spec.model,
        api_key=api_key,
        tool_label=got.tool_label or spec.tool_label,
    )


def call_llm(cfg: ExtractConf, prompt: str) -> str:
    """按 cfg.provider 分派 OpenAI 兼容 chat/completions，返回 assistant 原始文本。"""
    if not prompt.strip():
        raise ValueError("prompt 不能为空")
    return _post_chat(resolve_provider(cfg), prompt)


def _post_chat(conf: ProviderConf, prompt: str, timeout: float = 180.0) -> str:
    """POST {base_url}/chat/completions；超时/429/529/可重试业务码沿用 minimax 惯例重试 1 次。"""
    url = f"{conf.base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {conf.api_key}", "Content-Type": "application/json"}
    payload = {
        "model": conf.model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
    }
    last: Any = None
    for attempt in range(2):
        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
        except httpx.TimeoutException as e:
            log.warning("LLM 读超时（%ss）attempt %d/2", timeout, attempt + 1)
            if attempt == 0:
                continue
            raise RuntimeError(f"LLM 读超时（{timeout}s）重试后仍失败: {e}") from e
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            try:
                data = json.loads(resp.text or "{}")
            except Exception:  # noqa: BLE001
                data = {}
        last = data
        code = minimax._extract_code(data)
        if code in minimax._RETRY_CODES:
            log.warning("LLM 返回可重试错误 %s，attempt %d/2", code, attempt + 1)
            if attempt == 0:
                continue
            raise RuntimeError(f"LLM 可重试错误 {code}: {data}")
        if resp.status_code != 200:
            if resp.status_code in (429, 529) and attempt == 0:
                log.warning("LLM HTTP %d 限流，重试1次", resp.status_code)
                continue
            raise RuntimeError(f"LLM HTTP {resp.status_code}（code={code}）: {str(data)[:300]}")
        text = _content_of(data)
        if text.strip():
            return text
        raise RuntimeError(f"LLM 响应缺少 content: {str(data)[:300]}")
    raise RuntimeError(f"LLM 重试后仍失败: {str(last)[:300]}")


def _content_of(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    msg = choices[0].get("message")
    if isinstance(msg, dict) and isinstance(msg.get("content"), str):
        return msg["content"]
    tail = choices[0].get("text")
    return tail if isinstance(tail, str) else ""

