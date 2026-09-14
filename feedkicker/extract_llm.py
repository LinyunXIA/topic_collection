"""F29/F30 LLM provider 抽象 + 批量提炼：单层重试编排 / 调用 / 提示词构建 / JSON 解析（DESIGN §25.2–§25.4/§25.7）。"""

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
        tool_label="MMax",
    ),
    "deepseek": ProviderConf(
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        tool_label="DS",
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


def refine_batches(
    ex: ExtractConf, template: str, batches: list[list[dict[str, Any]]], max_calls: int
) -> tuple[list[dict[str, Any]], int, int, int]:
    """逐批 LLM 提炼，返回 (topics, calls, failed, empty)。

    单批「调用 + 解析」共享同一重试预算：第 1 次尝试失败（调用异常或 JSON/契约解析失败）
    重试 1 次，两次都失败才计 failed（总 HTTP ≤2/批）；模型合法返回空列表计 empty（PRV-2）；
    单条非法 topic 丢弃并计数（PRV-6）。
    """
    collected: list[dict[str, Any]] = []
    calls = 0
    failed = 0
    empty = 0
    for no, batch in enumerate(batches, 1):
        prompt = build_batch_prompt(template, batch)
        parsed: list[dict[str, Any]] | None = None
        dropped = 0
        limit_reached = False
        for attempt in (1, 2):
            if max_calls and calls >= max_calls:
                log.warning("达到 max_calls=%d 上限，停止剩余批", max_calls)
                limit_reached = True
                break
            calls += 1
            try:
                raw = call_llm(ex, prompt)
            except Exception as e:  # noqa: BLE001
                log.warning("第 %d/%d 批第 %d/2 次尝试失败（调用异常）: %s", no, len(batches), attempt, e)
                continue
            try:
                parsed, dropped = parse_topics(raw)
                break
            except ValueError as e:
                log.warning("第 %d/%d 批第 %d/2 次尝试失败（JSON/契约解析失败）: %s", no, len(batches), attempt, e)
        if limit_reached:
            break
        if parsed is None:
            failed += 1
            log.warning("第 %d/%d 批两次尝试后仍失败，跳过", no, len(batches))
            continue
        if dropped:
            log.warning("第 %d/%d 批丢弃 %d 条非法 topic", no, len(batches), dropped)
        if not parsed:
            if dropped:
                failed += 1
                log.warning("第 %d/%d 批 %d 条 topic 全部非法，计失败批", no, len(batches), dropped)
            else:
                empty += 1
                log.info("第 %d/%d 批模型合法返回空话题列表", no, len(batches))
            continue
        collected.extend(parsed)
        log.info("第 %d/%d 批提炼 %d 个话题", no, len(batches), len(parsed))
    return collected, calls, failed, empty


def _post_chat(conf: ProviderConf, prompt: str, timeout: float = 180.0) -> str:
    """POST {base_url}/chat/completions，单次尝试（PRV-8）。

    超时/429/529/业务可重试码一律抛 RuntimeError 可重试错误；重试仅由编排层
    `refine_batches` 做 1 次，总 HTTP ≤2/批，避免双层重试放大到 4 次。
    """
    url = f"{conf.base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {conf.api_key}", "Content-Type": "application/json"}
    payload = {
        "model": conf.model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
    }
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    except httpx.TimeoutException as e:
        raise RuntimeError(f"LLM 读超时（{timeout}s）: {e}") from e
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        try:
            data = json.loads(resp.text or "{}")
        except Exception:  # noqa: BLE001
            data = {}
    code = minimax._extract_code(data)
    if code in minimax._RETRY_CODES:
        raise RuntimeError(f"LLM 可重试错误 {code}: {data}")
    if resp.status_code != 200:
        raise RuntimeError(f"LLM HTTP {resp.status_code}（code={code}）: {str(data)[:300]}")
    text = _content_of(data)
    if text.strip():
        return text
    raise RuntimeError(f"LLM 响应缺少 content: {str(data)[:300]}")


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

